# WeHPAH!

A browser-based legal dossier explorer. Search passages, open the cited PDF page, and inspect the highlighted source region.

The current Microsoft Datacenter Dossier contains 283 PDFs and 91,130 passages. The browser uses the public [Hugging Face dataset](https://huggingface.co/datasets/kolenyo/MS_Datacenter) by default.

## Build or update a dossier

Put the PDFs in `raw_pdfs/` and run:

```sh
./wehpah
```

This opens the **WeHPAH! Studio**, a local page that sets the dossier's title, shows which PDFs are new, builds and checks the release, previews it, and publishes it to Hugging Face. The same steps are available as `./wehpah build`, `preview`, `login`, `publish` and `check`. Settings live in [dossier.toml](dossier.toml); only `title` is required. See the [ingestion guide](ingestion/INGESTION.md).

The builder keeps the existing release until a replacement passes its checks, and reuses earlier work for unchanged PDFs and passages. The output is `dist_data/` with `dossier.json`, `corpus.parquet`, per-cluster files in `clusters/`, `centroids.json`, `events.parquet` (dates), `authorities.parquet` (laws and cases), `names.parquet` (people and organizations), and `pdfs/`. How each stage works and why is in [docs/PIPELINE.md](docs/PIPELINE.md).

## Run the site locally

```sh
./wehpah preview
```

This opens `http://127.0.0.1:8410/?local`, which searches the local `dist_data/` through a server that supports HTTP byte ranges. Without `?local`, the site opens the public `kolenyo/MS_Datacenter` dataset; `?repo=owner/name` opens another published dossier.

## How searches work

- **Find Exact Words** finds a case-insensitive, literal phrase in `passage_text`. Results have stable document, page, and passage-ID ordering. For public datasets, the Hugging Face search endpoint supplies candidates; each displayed result is then checked for the literal phrase because the endpoint stems words and searches all string fields.
- **Explore Concepts** computes a query representation in a Web Worker, selects the two closest subject clusters, and ranks their passages. It is a discovery aid: its fixed cluster selection can miss relevant passages.
- **Ask the Dossier** combines keyword and concept candidates, reranks a candidate pool, and displays source sentences with citations. An optional on-device model selects source sentences; its output must pass the quote and statement checks. If that model is unavailable, the app displays extracted key passages.

- **Timeline, Laws & cases cited, People and Organizations** list what the build found across the whole dossier (links on the cover) or within the current results (the switch above the results). "When…?", "Which law…?" and "Who…?" questions in Ask the Dossier are answered from the same tables. Every entry is quoted exactly as written and opens its page.

The PDF viewer uses normalized, top-left page coordinates prepared during ingestion. A citation opens the original page and highlights the source item's region. The region can be broader than the exact quoted sentence.

## Transfer behavior and limits

The current DuckDB-Wasm version fetched the complete ~69 MB `corpus.parquet` in a local browser test, even after using its URL-registration API. This release includes one small Parquet file per subject cluster. Concept search loads only the two selected cluster files; a local browser test fetched `cluster_29.parquet` and `cluster_17.parquet` without requesting `corpus.parquet`. A public cluster file also returned HTTP 206 for a 1,024-byte range request. Exact-word search asks the Hugging Face service for candidates and checks the literal phrase in each passage; it may miss matches outside the service's candidate window. If that service fails, the app falls back to a Parquet scan, which can download the full file on this DuckDB-Wasm version.

The full release was rebuilt and uploaded on 7 November 2026. `--verify-only` passes with no invalid highlight boxes. An OCR environment check was added after an initial sandboxed run omitted text from plan pages; the corrected run recovered the affected documents. Some Docling table-cell and reading-order warnings remain, so dense tables still warrant inspection against the original PDF.

For reproducible retrieval evaluation, add a lawyer-reviewed set of questions with known relevant passage IDs and measure candidate recall, final recall, and citation location before adjusting cluster breadth or ranking thresholds.
