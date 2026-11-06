// reranker.js — cross-encoder re-ranking for "Ask the Dossier".
//
// Scores (question, passage) pairs with Xenova/bge-reranker-base (Transformers.js,
// q8, ~280 MB one-time download, browser-cached) inside a Web Worker so the UI
// never freezes. Raw relevance logits are returned in document order — higher is
// more relevant to the question.

import { CONFIG } from './db_client.js';

const TRANSFORMERS_URL =
  `https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.3.2`;

let _worker = null;
let _seq = 0;
const _pending = new Map();
let _ready = false;
let _initPromise = null;

export function isReady() {
  return _ready;
}

function workerSource() {
  return `
    import { AutoTokenizer, AutoModelForSequenceClassification } from "${TRANSFORMERS_URL}";
    let tok = null, model = null;
    self.onmessage = async ({ data }) => {
      const { id, op, payload } = data;
      try {
        if (op === 'init') {
          tok = await AutoTokenizer.from_pretrained(payload.model);
          // WebGPU fp16 scores pairs ~10x faster than WASM but can emit NaN logits
          // for this classifier on some driver stacks — validate with a canary pair
          // and fall back to WASM q8 when the output is not finite.
          let device = 'wasm', dtype = 'q8';
          const load = async (dev, dt) => AutoModelForSequenceClassification.from_pretrained(payload.model, {
            dtype: dt, device: dev,
            progress_callback: (e) => {
              if (e.status === 'progress' && e.total) {
                self.postMessage({ id, kind: 'progress', text: e.file ?? '', loaded: e.loaded, total: e.total });
              }
            },
          });
          const canaryOK = async (m) => {
            try {
              const t = tok(['Will construction create noise impacts?'],
                { text_pair: ['The project includes 45 backup generators.'], truncation: true, max_length: 128 });
              const out = await m(t);
              return Number.isFinite(Number(out.logits.data[0]));
            } catch { return false; }
          };
          try {
            const adapter = navigator.gpu ? await navigator.gpu.requestAdapter() : null;
            if (adapter) {
              const gpuModel = await load('webgpu', 'fp16');
              if (await canaryOK(gpuModel)) { model = gpuModel; device = 'webgpu'; dtype = 'fp16'; }
            }
          } catch { model = null; }
          if (!model) model = await load('wasm', 'q8');
          self.postMessage({ id, kind: 'done', device, dtype });
        } else if (op === 'rank') {
          const { query, docs, chunkSize, maxLength } = payload;
          const scores = [];
          for (let i = 0; i < docs.length; i += chunkSize) {
            const chunk = docs.slice(i, i + chunkSize);
            const inputs = tok(
              Array(chunk.length).fill(query),
              { text_pair: chunk, padding: true, truncation: true, max_length: maxLength || 384 }
            );
            const out = await model(inputs);
            const lg = out.logits;
            const stride = lg.dims[lg.dims.length - 1] || 1;
            for (let j = 0; j < chunk.length; j++) {
              const v = Number(lg.data[j * stride]);
              if (!Number.isFinite(v)) throw new Error('reranker produced non-finite scores');
              scores.push(v);
            }
            self.postMessage({ id, kind: 'progress', done: Math.min(i + chunk.length, docs.length), total: docs.length });
          }
          self.postMessage({ id, kind: 'done', scores });
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
      await call('init', { model: CONFIG.RERANKER_MODEL }, onProgress);
      _ready = true;
    })();
  }
  await _initPromise;
}

/**
 * Score each doc against the question. Returns an array of raw relevance logits
 * aligned 1:1 with `docs` (higher = more relevant).
 */
export async function rank(query, docs, onProgress) {
  if (!docs.length) return [];
  const res = await call(
    'rank',
    { query, docs, chunkSize: 8 },
    onProgress
  );
  return res.scores;
}
