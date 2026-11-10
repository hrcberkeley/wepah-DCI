// db_client.js — browser retrieval engine.
// Remote Parquet files are registered through DuckDB-Wasm's JavaScript HTTP file
// system. The older direct-URL read path can fetch an entire remote file.

export const CONFIG = {
  // Streams everything from the Hugging Face dataset repo (PDFs, Parquet, centroids);
  // keyword search uses the Datasets-Server BM25 endpoint. Set '' to stream locally.
  // Add ?local to the page URL to preview a freshly built dist_data/ before uploading it,
  // or ?repo=owner/name to open another published dossier (what `wehpah publish` prints).
  HF_DATASET_REPO: (() => {
    const params = new URLSearchParams(location.search);
    if (params.has('local')) return '';
    const repo = params.get('repo') || '';
    return /^[A-Za-z0-9][\w.-]*\/[A-Za-z0-9][\w.-]*$/.test(repo) ? repo : 'kolenyo/MS_Datacenter';
  })(),
  // Local base URL of dist_data when no HF repo is configured.
  LOCAL_DATA_BASE: '/dist_data',
  MAX_KEYWORD_RESULTS: 25,
  MAX_CONCEPT_RESULTS: 15,
  // Cross-encoder re-ranking pool for Ask the Dossier (retrieval quality pass).
  RERANKER_MODEL: 'Xenova/ms-marco-MiniLM-L-6-v2',
  RERANKER_KEYWORD_SHARE: 0.3,  // of the candidate pool; the rest comes from concept search
};
let activeDossier = null;
let _parquetSeq = 0;
const _registeredFiles = new Map();

async function registerParquet(url) {
  if (_registeredFiles.has(url)) return _registeredFiles.get(url);
  const alias = `dossier_corpus_${++_parquetSeq}.parquet`;
  const registration = _db.registerFileURL(alias, url, _duckdb.DuckDBDataProtocol.HTTP, false)
    .then(() => alias);
  _registeredFiles.set(url, registration);
  try { return await registration; }
  catch (error) { _registeredFiles.delete(url); throw error; }
}

export function currentRepository() { return CONFIG.HF_DATASET_REPO; }
export async function openDossier(repo, manifest) {
  const url = `https://huggingface.co/datasets/${repo}/resolve/main/${manifest.centroids_path}`;
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Could not open the search index (${response.status}).`);
  const centroids = await response.json();
  if (centroids.model_id !== manifest.model_id || centroids.dimensions !== manifest.dimensions || !Array.isArray(centroids.clusters) || !centroids.clusters.length || centroids.clusters.some((c) => !Number.isInteger(c.cluster_id) || !Array.isArray(c.centroid) || c.centroid.length !== 384)) throw new Error('The search index does not match this dossier.');
  CONFIG.HF_DATASET_REPO = repo;
  activeDossier = manifest;
  _centroids = centroids;
}

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
  return dataUrl(`${activeDossier?.pdfs_path || 'pdfs'}/${docId}.pdf`);
}
function corpusFile() { return registerParquet(dataUrl(activeDossier?.corpus_path || 'corpus.parquet')); }

async function conceptFiles(clusterIds) {
  const folder = activeDossier?.clusters_path;
  if (!folder) return [await corpusFile()];
  return Promise.all(clusterIds.map((id) => registerParquet(dataUrl(`${folder}/cluster_${String(id).padStart(2, '0')}.parquet`))));
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
export async function initEngine(manifest = null) {
  activeDossier = manifest;
  setStatus('Preparing the record index…', true);

  // 1. Centroid routing file (~90 KB)
  const cResp = await fetch(dataUrl(activeDossier?.centroids_path || 'centroids.json'));
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
  if (!CONFIG.HF_DATASET_REPO) {
    try {
      const manifest = await fetch(dataUrl('dossier.json'));
      if (manifest.ok) activeDossier = await manifest.json();
    } catch { /* Older local datasets have no manifest. */ }
  }
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

// ---------- Mode 1: literal case-insensitive substring search ----------
export async function keywordSearch(query, limit = CONFIG.MAX_KEYWORD_RESULTS) {
  const requested = query.trim();
  const safeLimit = Math.max(1, Math.min(100, Math.trunc(limit)));
  if (CONFIG.HF_DATASET_REPO) {
    try {
      // The server searches all string fields with stemming. Treat it only as
      // candidate generation and verify every result against passage_text.
      const matches = new Map();
      for (let offset = 0; offset < 1000 && matches.size < safeLimit; offset += 100) {
        const u = new URL('https://datasets-server.huggingface.co/search');
        for (const [key, value] of Object.entries({
          dataset: CONFIG.HF_DATASET_REPO, config: activeDossier?.config || 'default',
          split: activeDossier?.split || 'train', query: requested, offset, length: 100,
        })) u.searchParams.set(key, String(value));
        const response = await fetch(u);
        if (!response.ok) throw new Error(`Search service unavailable (${response.status}).`);
        const data = await response.json();
        for (const entry of data.rows || []) {
          const row = entry.row;
          if (String(row?.passage_text || '').toLocaleLowerCase().includes(requested.toLocaleLowerCase())) {
            matches.set(row.passage_id, {
              passage_id: row.passage_id, doc_id: row.doc_id, doc_title: row.doc_title,
              doc_date: row.doc_date, page_number: row.page_number,
              content_type: row.content_type, passage_text: row.passage_text,
              bbox_x_pct: row.bbox_x_pct, bbox_y_pct: row.bbox_y_pct,
              bbox_w_pct: row.bbox_w_pct, bbox_h_pct: row.bbox_h_pct,
              score: null, match: 'exact',
            });
          }
        }
        if (!data.rows?.length || offset + 100 >= (data.num_rows_total ?? 0)) break;
      }
      return [...matches.values()]
        .sort((a, b) => a.doc_id.localeCompare(b.doc_id) || a.page_number - b.page_number || a.passage_id.localeCompare(b.passage_id))
        .slice(0, safeLimit);
    } catch (error) {
      console.warn('The public search service is unavailable; searching the dossier file.', error);
    }
  }

  const lit = sqlString(query.trim());
  const file = await corpusFile();
  const sql = `
    SELECT ${METADATA_COLUMNS}
    FROM read_parquet('${file}')
    WHERE strpos(lower(passage_text), lower(${lit})) > 0
    ORDER BY doc_id ASC, page_number ASC, passage_id ASC
    LIMIT ${safeLimit};
  `;
  const result = await runQuery(sql);
  return rowsToObjects(result).map((row) => ({ ...row, score: null, match: 'exact' }));
}

// ---------- Mode 2: Explore Concepts (§4.3, centroid-routed) ----------
export async function conceptSearch(query, limit = CONFIG.MAX_CONCEPT_RESULTS, minChars = 0) {
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

  // 3. Predicate pushdown skips row groups outside clusters c1 and c2.
  //    Parquet reads fixed arrays back as lists, so cast embedding to FLOAT[384].
  //    The query vector and cluster ids are inlined as literals (no binding quirks).
  const vecLiteral = `CAST([${Array.from(qv).join(',')} ] AS FLOAT[384])`;
  const c1Safe = Number.parseInt(c1, 10);
  const c2Safe = Number.parseInt(c2, 10);
  let files = await conceptFiles([...new Set([c1Safe, c2Safe])]);
  const parquetSource = () => files.length === 1
    ? `'${files[0]}'`
    : `[${files.map((file) => `'${file}'`).join(', ')}]`;
  const sql = `
    SELECT ${METADATA_COLUMNS},
           array_cosine_similarity(CAST(embedding AS FLOAT[384]), ${vecLiteral}) AS score
    FROM read_parquet(${parquetSource()})
    WHERE cluster_id IN (${c1Safe}, ${c2Safe}) AND length(passage_text) >= ${Number(minChars) || 0}
    ORDER BY score DESC, passage_id ASC
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
  // Dossier-generic words: they appear in virtually every passage of this
  // record ("San Jose Data Center project"), so they carry zero retrieval
  // signal and crowd meaningful question words out of the token budget.
  'data', 'center', 'centers', 'project', 'san', 'jose', 'city',
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
    .slice(0, 8);
  if (!tokens.length) return [];
  if (CONFIG.HF_DATASET_REPO) {
    const u = new URL('https://datasets-server.huggingface.co/search');
    for (const [key, value] of Object.entries({
      dataset: CONFIG.HF_DATASET_REPO, config: activeDossier?.config || 'default',
      split: activeDossier?.split || 'train', query: tokens.join(' '), length: 100,
    })) u.searchParams.set(key, String(value));
    const response = await fetch(u);
    if (!response.ok) throw new Error(`Search service unavailable (${response.status}).`);
    const data = await response.json();
    const candidates = (data.rows || []).map((entry) => entry.row)
      .filter((row) => row?.passage_id && row?.passage_text);
    const score = (text) => {
      const upper = text.toUpperCase();
      return tokens.reduce((total, token) => total + upper.split(token.toUpperCase()).length - 1, 0)
        * 1000 / Math.max(200, text.length);
    };
    return candidates
      .map((row) => ({ ...row, _tf: score(row.passage_text) }))
      .sort((a, b) => b._tf - a._tf || a.passage_id.localeCompare(b.passage_id))
      .slice(0, limit);
  }
  const esc = (t) => t.replace(/\0/g, '').replace(/'/g, "''");
  const where = tokens.map((t) => `passage_text ILIKE '%${esc(t)}%'`).join(' OR ');
  // Term-frequency scoring, normalized by passage length: a dedicated noise
  // section says "noise" fifty times, a boilerplate letter says it once —
  // presence-counts alone let boilerplate win, and raw counts let 13k-char
  // policy tables that mention every token incidentally win over both.
  const tf = tokens
    .map((t) => `((LENGTH(passage_text) - LENGTH(REPLACE(UPPER(passage_text), '${esc(t.toUpperCase())}', ''))) / ${t.length})`)
    .join(' + ');
  const file = await corpusFile();
  const sql = `
    SELECT ${METADATA_COLUMNS}, (${tf}) * 1000.0 / GREATEST(200, LENGTH(passage_text)) AS _tf
    FROM read_parquet('${file}')
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
export async function candidatePassages(query, k = 40) {
  const kwN = Math.min(Math.round(k * CONFIG.RERANKER_KEYWORD_SHARE), k);
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
  try { pushAll(await conceptSearch(query, csN, 120)); } catch (err) { console.warn('concept search failed:', err); }
  if (!pool.length) throw new Error('All search methods failed');
  return pool;
}

// ---------- dated events & cited authorities (built by ingestion/entities.py) ----------
const PLACE_COLUMNS = 'passage_id, doc_id, doc_title, page_number, bbox_x_pct, bbox_y_pct, bbox_w_pct, bbox_h_pct, sentence';

export function hasEntities() { return !!(activeDossier?.events_path && activeDossier?.authorities_path); }

function idList(ids) { return ids.map(sqlString).join(', '); }

/** Dated mentions, one per (date, document), oldest first. Optional: only these passages. */
export async function timeline(passageIds = null, limit = 5000) {
  if (!hasEntities() || (passageIds && !passageIds.length)) return [];
  const file = await registerParquet(dataUrl(activeDossier.events_path));
  const where = passageIds ? `WHERE passage_id IN (${idList(passageIds)})` : '';
  return rowsToObjects(await runQuery(`
    SELECT date, precision, date_text, ${PLACE_COLUMNS}, mentions FROM (
      SELECT *, row_number() OVER (PARTITION BY date, doc_id ORDER BY page_number, passage_id) AS rn,
             count(*) OVER (PARTITION BY date, doc_id) AS mentions
      FROM read_parquet('${file}') ${where}
    ) WHERE rn = 1 ORDER BY date, doc_id LIMIT ${Math.trunc(limit)};`));
}

/** Laws, regulations and cases with how often and in how many documents each is cited. */
export async function authorityIndex(passageIds = null, limit = 300) {
  if (!hasEntities() || (passageIds && !passageIds.length)) return [];
  const file = await registerParquet(dataUrl(activeDossier.authorities_path));
  const where = passageIds ? `WHERE passage_id IN (${idList(passageIds)})` : '';
  return rowsToObjects(await runQuery(`
    SELECT instrument, count(*) AS mentions, count(DISTINCT doc_id) AS documents,
           list(DISTINCT provision ORDER BY provision) FILTER (WHERE provision <> '') AS provisions
    FROM read_parquet('${file}') ${where}
    GROUP BY instrument ORDER BY mentions DESC, instrument LIMIT ${Math.trunc(limit)};`));
}

/** Every citation of one law (optionally one section), one per passage. */
export async function authorityCitations(instrument, provision = '', passageIds = null, limit = 200) {
  if (!hasEntities()) return [];
  const file = await registerParquet(dataUrl(activeDossier.authorities_path));
  const where = [`instrument = ${sqlString(instrument)}`];
  if (provision) where.push(`provision = ${sqlString(provision)}`);
  if (passageIds) where.push(passageIds.length ? `passage_id IN (${idList(passageIds)})` : 'false');
  return rowsToObjects(await runQuery(`
    SELECT instrument, provision, cite_text, ${PLACE_COLUMNS} FROM (
      SELECT *, row_number() OVER (PARTITION BY passage_id ORDER BY provision) AS rn
      FROM read_parquet('${file}') WHERE ${where.join(' AND ')}
    ) WHERE rn = 1 ORDER BY doc_id, page_number, passage_id LIMIT ${Math.trunc(limit)};`));
}

// ---------- people & organizations (built by ingestion/names.py) ----------
export function hasNames() { return !!activeDossier?.names_path; }

function kindFilter(group) { return group === 'people' ? "kind = 'person'" : "kind <> 'person'"; }

/** People or organizations with mention and document counts, and the roles they appear in. */
export async function nameIndex(group, passageIds = null, limit = 5000) {
  if (!hasNames() || (passageIds && !passageIds.length)) return [];
  const file = await registerParquet(dataUrl(activeDossier.names_path));
  const where = [kindFilter(group)];
  if (passageIds) where.push(`passage_id IN (${idList(passageIds)})`);
  return rowsToObjects(await runQuery(`
    SELECT entity, any_value(kind) AS kind, count(*) AS mentions, count(DISTINCT doc_id) AS documents,
           list(DISTINCT role ORDER BY role) FILTER (WHERE role <> '') AS roles,
           max(defined_as) AS defined_as
    FROM read_parquet('${file}') WHERE ${where.join(' AND ')}
    GROUP BY entity ORDER BY documents DESC, mentions DESC, entity LIMIT ${Math.trunc(limit)};`));
}

/** Every mention of one person or organization, one per passage. */
export async function nameMentions(entity, passageIds = null, limit = 300) {
  if (!hasNames()) return [];
  const file = await registerParquet(dataUrl(activeDossier.names_path));
  const where = [`entity = ${sqlString(entity)}`];
  if (passageIds) where.push(passageIds.length ? `passage_id IN (${idList(passageIds)})` : 'false');
  return rowsToObjects(await runQuery(`
    SELECT entity, kind, role, mention_text, ${PLACE_COLUMNS} FROM (
      SELECT *, row_number() OVER (PARTITION BY passage_id ORDER BY role DESC) AS rn
      FROM read_parquet('${file}') WHERE ${where.join(' AND ')}
    ) WHERE rn = 1 ORDER BY role = '' , doc_id, page_number, passage_id LIMIT ${Math.trunc(limit)};`));
}

// ---------- links between names, laws and dates ----------
// A subject is { t: 'name' | 'law' | 'date', key, sub? }: a person or organization (key = entity),
// a law (key = instrument, sub = section) or a date (key = "2019-11-15" or "2019-11").

async function entityFiles() {
  const reg = (k) => (activeDossier?.[k] ? registerParquet(dataUrl(activeDossier[k])) : null);
  const [names, laws, dates] = await Promise.all([reg('names_path'), reg('authorities_path'), reg('events_path')]);
  return { names, laws, dates };
}

function subjectSource(files, s) {
  if (s.t === 'name') return [files.names, `entity = ${sqlString(s.key)}`];
  if (s.t === 'law') return [files.laws, `instrument = ${sqlString(s.key)}${s.sub ? ` AND provision = ${sqlString(s.sub)}` : ''}`];
  return [files.dates, `date = ${sqlString(s.key)}`];
}

/** Every name, law and date found in these passages: passage_id -> [{ t, text, key }]. */
export async function mentionsIn(passageIds) {
  const out = new Map();
  if (!passageIds.length) return out;
  const f = await entityFiles();
  const ids = `passage_id IN (${idList(passageIds)})`;
  const parts = [
    f.names && `SELECT passage_id, 'name' AS t, mention_text AS text, entity AS key FROM read_parquet('${f.names}') WHERE ${ids}`,
    f.laws && `SELECT passage_id, 'law', cite_text, instrument FROM read_parquet('${f.laws}') WHERE ${ids}`,
    f.dates && `SELECT passage_id, 'date', date_text, date FROM read_parquet('${f.dates}') WHERE ${ids}`,
  ].filter(Boolean);
  if (!parts.length) return out;
  for (const r of rowsToObjects(await runQuery(parts.join(' UNION ALL ')))) {
    if (!out.has(r.passage_id)) out.set(r.passage_id, []);
    out.get(r.passage_id).push(r);
  }
  return out;
}

/** How often a subject is mentioned, the span of its documents' dates, and the people,
 *  organizations, laws and dates named in the same passages (most shared passages first). */
export async function related(subject, limit = 12) {
  const f = await entityFiles();
  const [file, where] = subjectSource(f, subject);
  const ids = `(SELECT DISTINCT passage_id FROM read_parquet('${file}') WHERE ${where})`;
  const not = (col, t) => (subject.t === t ? `AND ${col} <> ${sqlString(subject.key)}` : '');
  const base = `count(DISTINCT passage_id) AS passages, count(DISTINCT doc_id) AS documents`;
  let facts;
  try { // doc_date is in releases built from November 2026 on
    facts = rowsToObjects(await runQuery(`SELECT ${base},
      min(doc_date) FILTER (WHERE doc_date <> 'Undated') AS first, max(doc_date) FILTER (WHERE doc_date <> 'Undated') AS last
      FROM read_parquet('${file}') WHERE ${where}`))[0];
  } catch { facts = rowsToObjects(await runQuery(`SELECT ${base} FROM read_parquet('${file}') WHERE ${where}`))[0]; }
  const out = { facts, people: [], orgs: [], laws: [], dates: [] };
  if (f.names) {
    const rows = rowsToObjects(await runQuery(`
      SELECT entity AS key, any_value(kind) AS kind, count(DISTINCT passage_id) AS n
      FROM read_parquet('${f.names}') WHERE passage_id IN ${ids} ${not('entity', 'name')}
      GROUP BY entity QUALIFY row_number() OVER (PARTITION BY any_value(kind) = 'person' ORDER BY n DESC, entity) <= ${limit}
      ORDER BY n DESC, entity`));
    out.people = rows.filter((r) => r.kind === 'person');
    out.orgs = rows.filter((r) => r.kind !== 'person');
  }
  if (f.laws) out.laws = rowsToObjects(await runQuery(`
      SELECT instrument AS key, count(DISTINCT passage_id) AS n FROM read_parquet('${f.laws}')
      WHERE passage_id IN ${ids} ${not('instrument', 'law')} GROUP BY instrument ORDER BY n DESC, key LIMIT ${limit}`));
  if (f.dates) out.dates = rowsToObjects(await runQuery(`
      SELECT * FROM (SELECT date AS key, any_value(precision) AS precision, count(DISTINCT passage_id) AS n
        FROM read_parquet('${f.dates}') WHERE passage_id IN ${ids} ${not('date', 'date')}
        GROUP BY date ORDER BY n DESC, key LIMIT ${limit}) ORDER BY key`));
  return out;
}

/** Every passage that mentions one date, one per passage. */
export async function dateMentions(date, limit = 300) {
  if (!hasEntities()) return [];
  const file = await registerParquet(dataUrl(activeDossier.events_path));
  return rowsToObjects(await runQuery(`
    SELECT date, precision, date_text, ${PLACE_COLUMNS} FROM (
      SELECT *, row_number() OVER (PARTITION BY passage_id) AS rn
      FROM read_parquet('${file}') WHERE date = ${sqlString(date)}
    ) WHERE rn = 1 ORDER BY doc_id, page_number, passage_id LIMIT ${Math.trunc(limit)};`));
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
