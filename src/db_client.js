// db_client.js — Zero-download hybrid query engine (AGENTS.md §4)
//
// Three data-access methods, none of which ever downloads the full corpus:
//   1. Keyword search   — Datasets-Server BM25 (when HF repo configured) or a selective
//                         DuckDB-Wasm ILIKE query projecting metadata only (no vector bytes).
//   2. Concept search   — In-browser query vector -> centroid routing -> DuckDB-Wasm
//                         fetches ONLY the two matching Parquet row groups (HTTP 206 ranges).
//   3. Hybrid retrieval — union of 1 + 2 for "Ask the Dossier".

export const CONFIG = {
  // Streams everything from the Hugging Face dataset repo (PDFs, Parquet, centroids);
  // keyword search uses the Datasets-Server BM25 endpoint. Set '' to stream locally.
  HF_DATASET_REPO: 'kolenyo/MS_Datacenter',
  // Local base URL of dist_data when no HF repo is configured.
  LOCAL_DATA_BASE: '/dist_data',
  // Score at or above which a concept match shows as a strong match.
  STRONG_MATCH_THRESHOLD: 0.72,
  MAX_KEYWORD_RESULTS: 25,
  MAX_CONCEPT_RESULTS: 15,
  DOSSIER_PASSAGES: 6,
  // Cross-encoder re-ranking pool for Ask the Dossier (retrieval quality pass).
  RERANKER_MODEL: 'Xenova/ms-marco-MiniLM-L-6-v2',
  RERANKER_CANDIDATES: 40,
  RERANKER_KEYWORD_SHARE: 12,   // of the candidate pool; the rest comes from concept search
};

// Pinned CDN bundles (no build step required)
const DUCKDB_WASM_URL = 'https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.32.0/+esm';
const TRANSFORMERS_VERSION = '4.3.2';
const EMBED_MODEL_ID = 'Xenova/bge-small-en-v1.5';
// bge model-card prefix recommended for queries (passages were embedded without it)
const QUERY_PREFIX = 'Represent this sentence for searching relevant passages: ';

export function dataUrl(path) {
  if (CONFIG.HF_DATASET_REPO) {
    return `https://huggingface.co/datasets/${CONFIG.HF_DATASET_REPO}/resolve/main/${path}`;
  }
  // DuckDB-Wasm requires a fully-qualified URL for its HTTP range reader;
  // plain absolute paths would resolve against its virtual FS instead of the network.
  return new URL(`${CONFIG.LOCAL_DATA_BASE}/${path}`, window.location.href).href;
}

export function pdfUrl(docId) {
  return dataUrl(`pdfs/${docId}.pdf`);
}

const METADATA_COLUMNS =
  'passage_id, doc_id, doc_title, doc_date, page_number, content_type, passage_text, ' +
  'bbox_x_pct, bbox_y_pct, bbox_w_pct, bbox_h_pct';

// ---------- internal state ----------
let _duckdb = null;   // module namespace
let _db = null;       // AsyncDuckDB
let _conn = null;     // AsyncDuckDBConnection
let _centroids = null; // { model_id, dimensions, clusters: [{cluster_id, centroid}] }
let _embedWorker = null;
let _embedSeq = 0;
const _embedPending = new Map();

function setStatus(msg, busy = false) {
  const el = document.getElementById('status-bar');
  if (!el) return;
  el.textContent = msg || '';
  el.classList.toggle('busy', busy);
}

// ---------- initialization (§4.1) ----------
export async function initEngine() {
  setStatus('Preparing the record index…', true);

  // 1. Centroid routing file (~90 KB)
  const cResp = await fetch(dataUrl('centroids.json'));
  if (!cResp.ok) throw new Error(`centroids.json not found (${cResp.status})`);
  _centroids = await cResp.json();

  // 2. DuckDB-Wasm from jsDelivr bundles
  _duckdb = await import(DUCKDB_WASM_URL);
  const bundles = _duckdb.getJsDelivrBundles();
  const bundle = await _duckdb.selectBundle(bundles);
  const workerUrl = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker}");`], { type: 'text/javascript' })
  );
  const dbWorker = new Worker(workerUrl);
  const logger = new _duckdb.ConsoleLogger(_duckdb.LogLevel.WARNING);
  _db = new _duckdb.AsyncDuckDB(logger, dbWorker);
  await _db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  _conn = await _db.connect();
  await ensureSimilarityFunction();

  // 3. In-browser semantic encoder inside a Web Worker (UI thread never freezes)
  initEmbedWorker();

  setStatus(
    `Record index ready — ${_centroids.clusters.length} subject areas across your dossier.`
  );
}

async function ensureSimilarityFunction() {
  // Prefer the engine's native fixed-array cosine function; on older DuckDB builds,
  // register an equivalent SQL macro so the §4.3 query text works unchanged.
  try {
    await _conn.query(
      'SELECT array_cosine_similarity(CAST([1.0,2.0] AS FLOAT[2]), CAST([2.0,1.0] AS FLOAT[2])) AS s'
    );
    return;
  } catch {
    const macroBody = (lambdaSyntax) => `
      CREATE OR REPLACE MACRO array_cosine_similarity(a, b) AS
        list_sum(list_transform(a, (${lambdaSyntax}x, i) x * b[i]))
        / (sqrt(list_sum(list_transform(a, (${lambdaSyntax}x) x * x)))
           * sqrt(list_sum(list_transform(b, (${lambdaSyntax}x) x * x))) + 1e-10);
    `;
    try {
      // Newer engines: `lambda x, i: expr`
      await _conn.query(macroBody('lambda '));
    } catch {
      // Older engines: arrow syntax `x -> expr`
      await _conn.query(macroBody(''));
    }
  }
}

function initEmbedWorker() {
  const src = `
    import { pipeline, env } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@${TRANSFORMERS_VERSION}";
    env.allowLocalModels = false;
    let extractor = null;
    async function ensure() {
      if (!extractor) {
        extractor = await pipeline("feature-extraction", "${EMBED_MODEL_ID}", { dtype: "q8" });
      }
    }
    onmessage = async (e) => {
      const { id, text, prefix } = e.data;
      try {
        await ensure();
        const out = await extractor((prefix || "") + text, { pooling: "cls", normalize: true });
        postMessage({ id, ok: true, embedding: Array.from(out.data) });
      } catch (err) {
        postMessage({ id, ok: false, error: String(err && err.message || err) });
      }
    };
  `;
  _embedWorker = new Worker(
    URL.createObjectURL(new Blob([src], { type: 'text/javascript' })),
    { type: 'module' }
  );
  _embedWorker.onmessage = (e) => {
    const { id, ok, embedding, error } = e.data;
    const pending = _embedPending.get(id);
    if (!pending) return;
    _embedPending.delete(id);
    ok ? pending.resolve(embedding) : pending.reject(new Error(error));
  };
}

export function embedQuery(text) {
  return new Promise((resolve, reject) => {
    const id = ++_embedSeq;
    _embedPending.set(id, { resolve, reject });
    _embedWorker.postMessage({ id, text, prefix: QUERY_PREFIX });
  });
}

function embedQueryWorker(text, prefix) {
  return new Promise((resolve, reject) => {
    const id = ++_embedSeq;
    _embedPending.set(id, { resolve, reject });
    _embedWorker.postMessage({ id, text, prefix });
  });
}

// Sentence embeddings for extractive synthesis arrive WITHOUT the query prefix.
export function embedRaw(text) {
  return embedQueryWorker(text, '');
}

// ---------- Mode 1: Find Exact Words (§4.2) ----------
export async function keywordSearch(query, limit = CONFIG.MAX_KEYWORD_RESULTS) {
  if (CONFIG.HF_DATASET_REPO) {
    try {
      const api = 'https://datasets-server.huggingface.co/search';
      const u = `${api}?dataset=${encodeURIComponent(CONFIG.HF_DATASET_REPO)}`
        + `&config=default&split=train&query=${encodeURIComponent(query)}&limit=${limit}`;
      const resp = await fetch(u);
      if (!resp.ok) throw new Error(`datasets-server ${resp.status}`);
      const data = await resp.json();
      return (data.rows || []).map((r) => {
        const row = r.row;
        return {
          passage_id: row.passage_id, doc_id: row.doc_id, doc_title: row.doc_title,
          doc_date: row.doc_date, page_number: row.page_number,
          content_type: row.content_type, passage_text: row.passage_text,
          bbox_x_pct: row.bbox_x_pct, bbox_y_pct: row.bbox_y_pct,
          bbox_w_pct: row.bbox_w_pct, bbox_h_pct: row.bbox_h_pct,
          score: null, match: 'exact',
        };
      });
    } catch (err) {
      // fall through to the local Parquet mode
      console.warn('Datasets Server unavailable, using local Parquet mode:', err);
    }
  }

  // Keyword inlined as an escaped SQL literal — avoids prepared-statement binding
  // quirks across duckdb-wasm builds. Single quotes are doubled; NULs stripped.
  const lit = sqlString(query);
  const sql = `
    SELECT ${METADATA_COLUMNS}
    FROM read_parquet('${dataUrl('corpus.parquet')}')
    WHERE passage_text ILIKE '%' || ${lit} || '%'
    LIMIT ${limit};
  `;
  const result = await runQuery(sql);
  return rowsToObjects(result);
}

// ---------- Mode 2: Explore Concepts (§4.3, centroid-routed) ----------
export async function conceptSearch(query, limit = CONFIG.MAX_CONCEPT_RESULTS) {
  if (!_centroids || !_centroids.clusters.length) throw new Error('Centroid index not loaded');

  // 1. Query vector, computed in the browser worker
  const qv = await embedQuery(query);

  // 2. Plain-JS dot product against centroids -> top 2 subject areas (clusters c1, c2)
  const scored = _centroids.clusters
    .map((c) => {
      let dot = 0;
      for (let i = 0; i < c.centroid.length; i++) dot += qv[i] * c.centroid[i];
      return { cluster_id: c.cluster_id, sim: dot };
    })
    .sort((a, b) => b.sim - a.sim);
  const c1 = scored[0].cluster_id;
  const c2 = scored.length > 1 ? scored[1].cluster_id : c1;

  // 3. Predicate pushdown fetches only the byte ranges of clusters c1 and c2.
  //    Parquet reads fixed arrays back as lists, so cast embedding to FLOAT[384].
  //    The query vector and cluster ids are inlined as literals (no binding quirks).
  const vecLiteral = `CAST([${Array.from(qv).join(',')} ] AS FLOAT[384])`;
  const c1Safe = Number.parseInt(c1, 10);
  const c2Safe = Number.parseInt(c2, 10);
  const sql = `
    SELECT ${METADATA_COLUMNS},
           array_cosine_similarity(CAST(embedding AS FLOAT[384]), ${vecLiteral}) AS score
    FROM read_parquet('${dataUrl('corpus.parquet')}')
    WHERE cluster_id IN (${c1Safe}, ${c2Safe})
    ORDER BY score DESC
    LIMIT ${limit};
  `;
  const result = await runQuery(sql);
  const rows = rowsToObjects(result);
  rows.forEach((r) => { r.match = 'concepts'; });
  return rows;
}

const STOP_WORDS = new Set([
  'will', 'this', 'that', 'with', 'from', 'have', 'been', 'their', 'about',
  'which', 'what', 'when', 'where', 'does', 'under', 'after', 'before',
  'while', 'those', 'these', 'they', 'them', 'than', 'then', 'into', 'over',
  'more', 'most', 'some', 'such', 'only', 'also', 'very', 'just', 'like',
  'make', 'made', 'were', 'each', 'much', 'many', 'upon', 'within', 'shall',
]);

/**
 * Keyword search over the question's content words. Sentence-style questions never
 * match ILIKE verbatim, so the dossier pool needs per-token matching (scored by
 * number of term hits) to pull in topically decisive passages.
 */
async function tokenSearch(query, limit) {
  const tokens = query
    .split(/[^A-Za-z0-9']+/)
    .filter((w) => w.length >= 4 && !STOP_WORDS.has(w.toLowerCase()))
    .slice(0, 6);
  if (!tokens.length) return [];
  const esc = (t) => t.replace(/\0/g, '').replace(/'/g, "''");
  const where = tokens.map((t) => `passage_text ILIKE '%${esc(t)}%'`).join(' OR ');
  // Term-frequency scoring: a dedicated noise section says "noise" fifty times,
  // a boilerplate letter says it once — presence-counts alone let boilerplate win.
  const tf = tokens
    .map((t) => `((LENGTH(passage_text) - LENGTH(REPLACE(UPPER(passage_text), '${esc(t.toUpperCase())}', ''))) / ${t.length})`)
    .join(' + ');
  const sql = `
    SELECT ${METADATA_COLUMNS}, (${tf}) AS _tf
    FROM read_parquet('${dataUrl('corpus.parquet')}')
    WHERE ${where}
    ORDER BY _tf DESC, doc_id ASC, page_number ASC
    LIMIT ${limit};
  `;
  const result = await runQuery(sql);
  return rowsToObjects(result);
}

/** Wide candidate pool for Ask the Dossier: token-keyword + phrase-keyword + concept hits, deduped.
 *  Callers slice/select from this pool (e.g., after cross-encoder re-ranking).
 *  Queries run sequentially — one AsyncDuckDBConnection serializes its messages and
 *  concurrent queries can deadlock the worker. */
export async function candidatePassages(query, k = CONFIG.RERANKER_CANDIDATES) {
  const kwN = Math.min(CONFIG.RERANKER_KEYWORD_SHARE, k);
  const csN = Math.max(0, k - kwN);
  const seen = new Set();
  const pool = [];
  const pushAll = (rows) => {
    if (!Array.isArray(rows)) return;
    for (const r of rows) {
      if (!seen.has(r.passage_id)) {
        seen.add(r.passage_id);
        pool.push(r);
      }
    }
  };
  try { pushAll(await tokenSearch(query, kwN)); } catch (err) { console.warn('token search failed:', err); }
  try { pushAll(await keywordSearch(query, 4)); } catch (err) { console.warn('phrase search failed:', err); }
  try { pushAll(await conceptSearch(query, csN)); } catch (err) { console.warn('concept search failed:', err); }
  if (!pool.length) throw new Error('All search methods failed');
  return pool;
}

// ---------- helpers ----------
/** Escape a JS string as a DuckDB string literal. */
function sqlString(s) {
  return `'${String(s ?? '').replace(/\0/g, '').replace(/'/g, "''")}'`;
}

async function runQuery(sql) {
  return _conn.query(sql);
}

function rowsToObjects(result) {
  // duckdb-wasm returns an arrow table; read rows as plain JS objects.
  const rows = [];
  const child = result.getChild ? result : null;
  const n = result.numRows;
  const cols = result.schema ? result.schema.fields.map((f) => f.name) : [];
  for (let i = 0; i < n; i++) {
    const obj = {};
    for (const c of cols) obj[c] = child ? result.getChildAt(result.schema.fields.findIndex((f) => f.name === c)).get(i) : result[c][i];
    rows.push(obj);
  }
  return rows;
}

export function isReady() {
  return !!(_conn && _centroids && _embedWorker);
}
