# WeHPAH!

A browser-based legal dossier explorer. Search passages, open the cited PDF page, and inspect the highlighted source region.

The current Microsoft Datacenter Dossier contains 283 PDFs and 91,130 passages. The browser uses the public [Hugging Face dataset](https://huggingface.co/datasets/kolenyo/MS_Datacenter) by default.

## Build or update a dossier

Follow the step-by-step [ingestion guide](ingestion/INGESTION.md). It covers adding PDFs, running the builder, checking a release, previewing it, publishing it, and reusing previous processing.

```sh
uv sync --project ingestion
uv run --project ingestion python ingestion/build_corpus.py --title "My dossier"
uv run --project ingestion python ingestion/build_corpus.py --verify-only
```

The builder keeps the existing release until a replacement passes validation. It caches successful extraction by PDF content and settings, and passage representations by text and model revision. The output is `dist_data/` with `dossier.json`, `corpus.parquet`, per-cluster files in `clusters/`, `centroids.json`, and `pdfs/`.

## Run the site locally

```sh
uv run --project ingestion python ingestion/devserver.py 8410
```

Open `http://127.0.0.1:8410/`. The site currently points to the public `kolenyo/MS_Datacenter` dataset in [src/db_client.js](src/db_client.js). Clear `HF_DATASET_REPO` there to search the local `dist_data/`. The local server supports HTTP byte ranges.

## How searches work

- **Find Exact Words** finds a case-insensitive, literal phrase in `passage_text`. Results have stable document, page, and passage-ID ordering. For public datasets, the Hugging Face search endpoint supplies candidates; each displayed result is then checked for the literal phrase because the endpoint stems words and searches all string fields.
- **Explore Concepts** computes a query representation in a Web Worker, selects the two closest subject clusters, and ranks their passages. It is a discovery aid: its fixed cluster selection can miss relevant passages.
- **Ask the Dossier** combines keyword and concept candidates, reranks a candidate pool, and displays source sentences with citations. An optional on-device model selects source sentences; its output must pass the quote and statement checks. If that model is unavailable, the app displays extracted key passages.

The PDF viewer uses normalized, top-left page coordinates prepared during ingestion. A citation opens the original page and highlights the source item's region. The region can be broader than the exact quoted sentence.

## Transfer behavior and limits

The current DuckDB-Wasm version fetched the complete ~69 MB `corpus.parquet` in a local browser test, even after using its URL-registration API. This release includes one small Parquet file per subject cluster. Concept search loads only the two selected cluster files; a local browser test fetched `cluster_29.parquet` and `cluster_17.parquet` without requesting `corpus.parquet`. A public cluster file also returned HTTP 206 for a 1,024-byte range request. Exact-word search asks the Hugging Face service for candidates and checks the literal phrase in each passage; it may miss matches outside the service's candidate window. If that service fails, the app falls back to a Parquet scan, which can download the full file on this DuckDB-Wasm version.

The full release was rebuilt and uploaded on 7 November 2026. `--verify-only` passes with no invalid highlight boxes. An OCR environment check was added after an initial sandboxed run omitted text from plan pages; the corrected run recovered the affected documents. Some Docling table-cell and reading-order warnings remain, so dense tables still warrant inspection against the original PDF.

For reproducible retrieval evaluation, add a lawyer-reviewed set of questions with known relevant passage IDs and measure candidate recall, final recall, and citation location before adjusting cluster breadth or ranking thresholds.
