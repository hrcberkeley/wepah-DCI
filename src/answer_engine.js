// answer_engine.js — the on-device answering helper behind "Ask the Dossier".
//
// Runs Qwen3.5 (WebLLM, WebGPU) entirely in a Web Worker so the UI thread
// stays free. Claims are grammar-constrained to valid JSON and still pass the
// deterministic verifier gate in verifier.js before anything reaches the screen.
// The model downloads once and is cached by the browser.
//
// Model choice (Nov 2026 leaderboard pass): at tiny sizes Qwen3.5-2B only gains
// ~3 points over Qwen3-1.7B and the model card warns about "thinking loops";
// Qwen3.5-4B scores 89.8% IFEval (vs 78.6% for the 2B) — instruction-following
// precision is exactly what the JSON claim + verbatim-quote contract needs.
//
// Adaptive tiering: capable machines get the 4B, constrained GPUs / low-RAM
// machines start one or two tiers down, and any failed init automatically
// retries with the next smaller tier — so the written answer (or the
// zero-download extractive fallback) works on everything from an M4 Max
// to an 8 GB laptop.

const WEBLLM_URL = 'https://esm.run/@mlc-ai/web-llm@0.2.87';
const STORAGE_KEY = 'wehpah-answer-engine-enabled';
const WORKING_MODEL_KEY = 'wehpah-answer-engine-working-model';

// Prebuilt WebLLM models, best-first. approxGB is the one-time download size
// shown in the UI; MLC publishes VRAM lower bounds of ~3.9 / ~2.2 / ~1.6 GB.
const MODEL_TIERS = [
  { id: 'Qwen3.5-4B-q4f16_1-MLC', approxGB: '2.5' },
  { id: 'Qwen3.5-2B-q4f16_1-MLC', approxGB: '1.3' },
  { id: 'Qwen3.5-0.8B-q4f16_1-MLC', approxGB: '0.9' },
];

/** Pick the tier to TRY FIRST on this machine (a failed init still retries smaller). */
async function pickStartTier() {
  // A successful smaller tier should load directly on subsequent visits.
  try {
    const prior = localStorage.getItem(WORKING_MODEL_KEY);
    const priorIndex = MODEL_TIERS.findIndex((tier) => tier.id === prior);
    if (priorIndex >= 0) return priorIndex;
  } catch { /* storage can be unavailable in private browsing */ }
  // navigator.deviceMemory is capped at 8 (meaning "8 GB or more"), so any
  // reported value below 8 identifies a genuinely small machine.
  const mem = Number(navigator.deviceMemory) || null;
  if (mem && mem < 8) return MODEL_TIERS.length - 1;

  let gpu = null;
  try {
    const adapter = navigator.gpu ? await navigator.gpu.requestAdapter() : null;
    if (adapter) {
      let info = adapter.info || null;
      if (!info && typeof adapter.requestAdapterInfo === 'function') {
        try { info = await adapter.requestAdapterInfo(); } catch { /* older API */ }
      }
      gpu = {
        apple: /apple/i.test(`${info?.vendor || ''} ${info?.architecture || ''} ${info?.description || ''}`),
        maxBufferMB: adapter.limits ? adapter.limits.maxBufferSize / (1024 * 1024) : null,
      };
    }
  } catch { /* no WebGPU: callers already gate on webGPUSupported() */ }

  if (gpu && gpu.maxBufferMB !== null && gpu.maxBufferMB < 1536) return MODEL_TIERS.length - 1;
  if (gpu && gpu.apple) return 0;   // Apple Silicon: unified memory, 16 GB+ in practice
  if (gpu && gpu.maxBufferMB !== null && gpu.maxBufferMB >= 3072) return 0;
  return 1;
}

/** Approximate one-time download size for this machine, e.g. "2.5" (GB), or null. */
export async function estimateSetupGB() {
  try {
    const tier = MODEL_TIERS[await pickStartTier()];
    return tier ? tier.approxGB : null;
  } catch {
    return null;
  }
}

function clip(text, max = 900) {
  const t = String(text ?? '');
  return t.length > max ? `${t.slice(0, max)}…` : t;
}

let _worker = null;
let _seq = 0;
const _pending = new Map();
let _ready = false;
let _initPromise = null;
let _modelId = null;

/** Model id the engine actually initialized with (for diagnostics). */
export function activeModelId() {
  return _modelId;
}

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
    let engine = null;
    self.onmessage = async ({ data }) => {
      const { id, op, payload } = data;
      try {
        if (op === 'init') {
          // Keep import failures inside the request/response protocol. A failed
          // top-level import otherwise kills the worker and leaves the UI waiting.
          const { CreateMLCEngine } = await import("${WEBLLM_URL}");
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
            extra_body: { enable_thinking: false },
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
  _worker.onerror = (event) => {
    const error = new Error(event.message || 'The answering helper stopped unexpectedly.');
    for (const pending of _pending.values()) pending.reject(error);
    _pending.clear();
    _worker.terminate();
    _worker = null;
  };
  _worker.onmessageerror = () => {
    const error = new Error('The answering helper could not return its result.');
    for (const pending of _pending.values()) pending.reject(error);
    _pending.clear();
    _worker.terminate();
    _worker = null;
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
      let idx = await pickStartTier();
      for (;;) {
        try {
          await call('init', { modelId: MODEL_TIERS[idx].id }, onProgress);
          _modelId = MODEL_TIERS[idx].id;
          try { localStorage.setItem(WORKING_MODEL_KEY, _modelId); } catch { /* private mode */ }
          _ready = true;
          return;
        } catch (err) {
          if (idx + 1 >= MODEL_TIERS.length) throw err;
          idx += 1;
          // The GPU couldn't hold this tier — step down and let the caller's
          // progress UI show the restart.
          onProgress?.({ progress: 0, text: `The larger model didn't fit this machine — preparing a smaller one…` });
        }
      }
    })();
  }
  try {
    await _initPromise;
  } catch (error) {
    // A later search or explicit retry must be able to start a fresh attempt.
    _initPromise = null;
    _ready = false;
    throw error;
  }
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
        '- Produce a direct answer in 2 to 4 complementary claims. Include important qualifications or contrary evidence.\n' +
        '- Each claim must be supported by ONE excerpt. Never combine facts from different excerpts in one claim.\n' +
        '- claim.statement: copy ONE complete sentence from the cited excerpt, character-for-character. Do not paraphrase it.\n' +
        '- claim.passage_id: copy the passage_id of the excerpt you used, character-for-character.\n' +
        '- claim.verbatim_quote: copy a contiguous supporting span from that excerpt, character-for-character, containing the complete statement and its immediate qualification where present.\n' +
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
    // WebLLM's XGrammar expects the schema as a JSON-encoded string.
    responseFormat: { type: 'json_object', schema: JSON.stringify(CLAIMS_SCHEMA) },
    maxTokens: 700,
  });
  const text = res.content || '';
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start < 0 || end <= start) return [];
  const parsed = JSON.parse(text.slice(start, end + 1));
  return Array.isArray(parsed.claims) ? parsed.claims : [];
}
