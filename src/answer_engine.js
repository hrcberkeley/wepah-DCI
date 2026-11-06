// answer_engine.js — the on-device answering helper behind "Ask the Dossier".
//
// Runs Qwen3-1.7B (WebLLM, WebGPU) entirely in a Web Worker so the UI thread
// stays free. Claims are grammar-constrained to valid JSON and still pass the
// deterministic verifier gate in verifier.js before anything reaches the screen.
// The model (~1.1 GB, q4f16) downloads once and is cached by the browser.

const WEBLLM_URL = 'https://esm.run/@mlc-ai/web-llm@0.2.87';
const MODEL_ID = 'Qwen3-1.7B-q4f16_1-MLC';
const STORAGE_KEY = 'wehpah-answer-engine-enabled';

function clip(text, max = 900) {
  const t = String(text ?? '');
  return t.length > max ? `${t.slice(0, max)}…` : t;
}

let _worker = null;
let _seq = 0;
const _pending = new Map();
let _ready = false;
let _initPromise = null;

// ---------- opt-in state (plain-language consent for the one-time download) ----------
export function optedIn() {
  try { return localStorage.getItem(STORAGE_KEY) === 'on'; } catch { return false; }
}
export function optIn() {
  try { localStorage.setItem(STORAGE_KEY, 'on'); } catch { /* private mode */ }
}

export async function webGPUSupported() {
  try {
    return !!(navigator.gpu && await navigator.gpu.requestAdapter());
  } catch {
    return false;
  }
}

export function isReady() {
  return _ready;
}

// ---------- worker plumbing ----------
function workerSource() {
  return `
    import { CreateMLCEngine } from "${WEBLLM_URL}";
    let engine = null;
    self.onmessage = async ({ data }) => {
      const { id, op, payload } = data;
      try {
        if (op === 'init') {
          engine = await CreateMLCEngine(payload.modelId, {
            initProgressCallback: (r) =>
              self.postMessage({ id, kind: 'progress', text: r.text ?? '', progress: r.progress ?? 0 }),
          });
          self.postMessage({ id, kind: 'done' });
        } else if (op === 'complete') {
          const res = await engine.chat.completions.create({
            messages: payload.messages,
            temperature: 0.2,
            max_tokens: payload.maxTokens ?? 700,
            response_format: payload.responseFormat,
          });
          self.postMessage({ id, kind: 'done', content: res.choices?.[0]?.message?.content ?? '' });
        }
      } catch (err) {
        self.postMessage({ id, kind: 'error', error: String(err?.message || err) });
      }
    };
  `;
}

function ensureWorker() {
  if (_worker) return _worker;
  _worker = new Worker(
    URL.createObjectURL(new Blob([workerSource()], { type: 'text/javascript' })),
    { type: 'module' }
  );
  _worker.onmessage = ({ data }) => {
    const { id, kind } = data;
    const p = _pending.get(id);
    if (!p) return;
    if (kind === 'progress') {
      p.onProgress?.(data);
      return;
    }
    _pending.delete(id);
    if (kind === 'done') p.resolve(data);
    else p.reject(new Error(data.error));
  };
  return _worker;
}

function call(op, payload, onProgress) {
  return new Promise((resolve, reject) => {
    const id = ++_seq;
    _pending.set(id, { resolve, reject, onProgress });
    ensureWorker().postMessage({ id, op, payload });
  });
}

// ---------- lifecycle ----------
export async function ensureReady(onProgress) {
  if (_ready) return;
  if (!_initPromise) {
    _initPromise = (async () => {
      await call('init', { modelId: MODEL_ID }, onProgress);
      _ready = true;
    })();
  }
  await _initPromise;
}

// ---------- claim generation ----------
const CLAIMS_SCHEMA = {
  type: 'object',
  properties: {
    claims: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          statement: { type: 'string' },
          passage_id: { type: 'string' },
          verbatim_quote: { type: 'string' },
        },
        required: ['statement', 'passage_id', 'verbatim_quote'],
      },
    },
  },
  required: ['claims'],
};

function buildMessages(question, rows) {
  const excerpts = rows
    .map((r, i) =>
      `[${i + 1}] passage_id=${r.passage_id} (from "${r.doc_title}", page ${r.page_number})\n"""${clip(r.passage_text)}"""`
    )
    .join('\n\n');

  return [
    { role: 'system', content: 'You are a meticulous legal research assistant. /no_think' },
    {
      role: 'user',
      content:
        'Answer the question using ONLY the numbered excerpts below.\n' +
        'Rules:\n' +
        '- Produce 2 to 4 claims that together answer the question.\n' +
        '- claim.statement: ONE plain sentence, grounded in a single excerpt.\n' +
        '- claim.passage_id: copy the passage_id of the excerpt you used, character-for-character.\n' +
        '- claim.verbatim_quote: copy ONE contiguous sentence from that excerpt, character-for-character. Never paraphrase it.\n' +
        '- If the excerpts do not answer the question, return {"claims":[]}.\n\n' +
        `Question: ${question}\n\nExcerpts:\n${excerpts}`,
    },
  ];
}

/**
 * Generate `{statement, passage_id, verbatim_quote}` claims for the question.
 * Returns the raw parsed claims — the caller MUST run them through verifier.js
 * before rendering anything on screen.
 */
export async function generateClaims(question, rows) {
  const res = await call('complete', {
    messages: buildMessages(question, rows),
    responseFormat: { type: 'json_object', schema: CLAIMS_SCHEMA },
    maxTokens: 700,
  });
  const text = res.content || '';
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start < 0 || end <= start) return [];
  const parsed = JSON.parse(text.slice(start, end + 1));
  return Array.isArray(parsed.claims) ? parsed.claims : [];
}
