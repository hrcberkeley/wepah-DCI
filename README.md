# Investigations Lab's WeHPAH!

> **(We have pinpoint at home!)** — a zero-backend legal dossier explorer:
> search 261 docket PDFs, get cited passages, and see the exact spot on the original page highlighted in gold.

Built to the contract in [`AGENTS.md`](./AGENTS.md) over the CEC San Jose City Data Center docket (19-SPPE-04).

## Project layout

```text
project-root/
├── raw_pdfs/                  # 261 source PDFs (moved here from the folder root)
├── ingestion/
│   ├── pyproject.toml         # uv-managed Python 3.12 environment
│   ├── build_corpus.py        # OCR → bbox normalization → embeddings → KMeans → Parquet
│   └── devserver.py           # range-capable static server for local use
├── dist_data/                 # deployable artifacts (upload this to Hugging Face)
│   ├── corpus.parquet         # 85,590 rows, sorted by cluster, ROW_GROUP_SIZE 500, ZSTD
│   ├── centroids.json         # 64 K-means centroids for browser-side routing
│   └── pdfs/                  # web-served copies of every PDF
└── web_app/                   # static vanilla-JS frontend (no build step required)
    ├── index.html
    ├── style.css
    └── src/
        ├── main.js            # UI controller & progressive disclosure state machine
        ├── db_client.js       # DuckDB-Wasm + HF Datasets Server hybrid query engine
        ├── verifier.js        # deterministic quote-grounding verification gate
        └── pdf_stage.js       # PDF.js renderer with percentage bbox overlays
```

## Quick start (local)

```bash
# 1. Build the corpus (incremental: extraction is cached per document in ingestion/.cache/)
cd ingestion
uv sync
uv run python build_corpus.py            # full 261-doc run (~30 min on an M4 Max; resumable)
uv run python build_corpus.py --limit 5  # quick smoke build
uv run python build_corpus.py --verify-only

# 2. Serve and open
uv run python devserver.py 8410
# → http://127.0.0.1:8410/web_app/
```

`devserver.py` is a plain static server with the extras the zero-download design needs:
HTTP **206 Partial Content** support, `Accept-Ranges: bytes`, and CORS expose-headers.
(Python's built-in `http.server` silently ignores Range headers — don't swap it in.)

## Point the frontend at Hugging Face (zero-local-data mode) — ACTIVE

`web_app/src/db_client.js` ships configured for the live dataset:

```js
export const CONFIG = {
  HF_DATASET_REPO: 'kolenyo/MS_Datacenter',  // '' = stream from local /dist_data
  ...
};
```

With this set, keyword search uses the HF Datasets-Server BM25 endpoint (once indexing
finishes for the repo) and Parquet/PDF/centroid URLs resolve to
`https://huggingface.co/datasets/kolenyo/MS_Datacenter/resolve/main/…`.

### Refreshing the remote copy after re-ingestion (§3.4)

```bash
hf auth login --token hf_...   # write-scoped token
hf upload kolenyo/MS_Datacenter dist_data . --repo-type dataset
```

Last upload: 285 files (283 PDFs + corpus.parquet + centroids.json), 1.03 GB,
verified with a `206 Partial Content` ranged GET against the HF CDN.

## Search modes (§5.2)

| Mode | What it does |
|---|---|
| **1. Find Exact Words** | `ILIKE` substring search (or Datasets-Server BM25 when an HF repo is configured), with gold `<mark>` highlighting |
| **2. Explore Concepts** | Query is embedded in a Web Worker, routed to the 2 nearest of 64 centroids, scored against the matching rows |
| **3. Ask the Dossier** | Hybrid retrieval (union of both) → **on-device written answer** (see below), every bullet carrying a gold `verify: …` pill that opens the cited page |

**The answering engine.** Ask the Dossier is powered by **Qwen3.5-4B (WebLLM, WebGPU)** — chosen
after a Nov 2026 leaderboard pass (tiny-model quality per IFEval/BFCL; the same-size Qwen3.5-2B
gains only ~3 points over the older 1.7B with a "thinking loops" caveat, while the 4B scores
89.8% IFEval) — running entirely in a Web Worker on the user's machine
(`web_app/src/answer_engine.js`). First use requires an explicit opt-in (a gold chip on the
summary box — the ~2.5 GB q4f16 download is
one-time and browser-cached). Claims are grammar-constrained to valid JSON via XGrammar
(`response_format: json_object` + schema) and then pass the §5.4.4 verification gate in
`verifier.js` — `verbatim_quote` must appear character-for-character in the cited passage,
and `passage_id` must resolve, or the claim is silently dropped. When WebGPU is unavailable,
setup fails, or zero claims survive the gate, the deterministic extractive summary
(best sentence per document, scored with the bge-small encoder) takes over — so the
feature always produces cited output. Mode 3 also shows an "experimental feature" warning.

**Cross-encoder re-ranking.** Dossier retrieval fetches a ~40-passage pool — token-keyword
hits (term-frequency scored **and length-normalized**, with dossier-generic words like
"data/center/project" pruned so they don't crowd out meaningful question words;
`web_app/src/db_client.js` `candidatePassages`), phrase hits, and centroid-routed concept
hits — then re-ranks every (question, passage) pair with
**`Xenova/ms-marco-MiniLM-L-6-v2`** (~25 MB one-time, `web_app/src/reranker.js`) inside a
Web Worker, keeping the top 6 for the answer. WebGPU fp16 is attempted first with a canary
that checks a known relevance **ordering** (relevant text must outscore irrelevant text —
finite-but-degraded fp16 logits fail it and fall back to WASM q8), table-cell fragments
(<200 chars) are filtered, and DuckDB queries run serialized (one AsyncDuckDBConnection
deadlocks under concurrent queries). If the reranker can't load or score, the pool's TF
order is used directly.

## Verification status (§6 checklist)

| Check | Status |
|---|---|
| 1. `corpus.parquet` + `centroids.json` exist, non-empty | ✅ 93,167 rows / 64 clusters / 283 docs, 0 extraction failures |
| 2. `0.0 ≤ bbox_*_pct ≤ 1.0` for 100% of rows | ✅ 0 violations; all embeddings 384-d with norm 1.0 |
| 3. Verify HTTP `206` range streaming | ⚠️ **PDFs: verified locally AND on the HF CDN** (`206` + `Content-Range` on ranged GETs). **Parquet: not achievable as specified** — see below |
| 4. Card click → right pane renders page with gold box over cited text | ✅ Verified in-browser in local mode and HF mode (e.g. Draft EIR p. 398 of 471 streamed from HF, gold box over the cited "Traffic Impacts" passage) |

### Known spec deviation: DuckDB-Wasm range reads

AGENTS.md §4.3 assumes DuckDB-Wasm fetches only the byte ranges of the matching row
groups. In the current DuckDB-Wasm build (`@duckdb/duckdb-wasm@1.32.0`), the browser
HTTP layer (`lib/src/http_wasm.cc`, `file_page_buffer.cc`) contains **no range-request
logic** — remote files are fetched with a single full GET and buffered for the session.
The same query against native DuckDB *does* produce 206s, and this repo's `devserver.py`
serves correct 206s (verified with curl and native DuckDB), so nothing on the serving
side is missing. Practical impact: the first concept search downloads `corpus.parquet`
(~67 MB with embeddings) once; subsequent queries are instant. Keyword search with an
HF repo configured bypasses the Parquet file entirely via Datasets-Server. If DuckDB-Wasm
ships range-capable HTTP reads, the existing row-group sort + `ROW_GROUP_SIZE 500` layout
will pick the benefit up with no further changes.

## Notes

- `GetDocument.aspx` in the project root is **not a PDF** (it's a PowerPoint file); it was left in place and excluded from the corpus.
- Re-runs are incremental: per-document extraction JSON is cached under `ingestion/.cache/` — delete it (or pass `--force`) to re-OCR from scratch.
- Coordinates follow §3.2: docling `BOTTOMLEFT` bboxes are converted to top-left fractions of page width/height and clamped to `[0,1]`; the browser never touches raw PDF points.
