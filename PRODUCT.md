# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Non-technical lawyers and legal reviewers working through the CEC San Jose City Data
Center docket (19-SPPE-04). They experience high cognitive fatigue with complex
filters, chatbots that hide sources, and raw database tables; they verify evidence by
reading the original page. Secondary audience: the Investigations Lab team that built
and maintains the explorer.

## Product Purpose

A zero-backend legal dossier explorer: search 283 docket PDFs (91,130 passages), get
cited passages, and see the exact spot on the original page highlighted. Success =
the lawyer finds a passage and verifies it against the original page in one click,
without ever leaving the browser or trusting an unsourced answer.

## Positioning

Every answer is traceable to a highlighted bounding box on the original PDF page —
claims that cannot be verified verbatim against their cited passage are silently
dropped and never rendered. All querying happens client-side (DuckDB-Wasm +
Transformers.js over remote Parquet/PDF range requests); there is no backend.

## Operating Context

- Static vanilla-JS app (`web_app/`), no build step; CDN modules (DuckDB-Wasm,
  Transformers.js, PDF.js, WebLLM) and a Hugging Face dataset repo
  (`kolenyo/MS_Datacenter`) holding `corpus.parquet`, `centroids.json`, and `pdfs/`.
- Local development via `ingestion/devserver.py` (range-capable static server).
- Data contract (Parquet schema, centroid routing, bbox top-left percentages) is
  defined in `AGENTS.md` §2–§4 and must not drift between Python and JS.

## Capabilities and Constraints

- Three search modes: Find Exact Words (BM25/ILIKE), Explore Concepts (in-browser
  embedding → centroid-routed Parquet scan), Ask the Dossier (hybrid retrieval →
  cross-encoder re-rank → optional on-device WebLLM synthesis, always gated).
- Mandatory deterministic verification gate (`verifier.js`): rendered claims must
  quote their source passage verbatim.
- UI copy rule: never render AI/ML jargon ("Vector", "Embedding", "Chunk", "Top-K",
  "LLM", "Cosine Distance") anywhere in the interface.
- PDF overlay boxes come from the data as top-left percentages; the frontend never
  computes raw PDF points.
- Body copy minimum 15px; interface must stay legible for long reading sessions.
- Mode 3 is labeled experimental in the UI.

## Brand Commitments

- Existing name: **Investigations Lab's WeHPAH! (We have pinpoint at home!)**.
- User-confirmed (2026-11-06): the redesign has full visual freedom — the behavioral
  contract (three modes, no-jargon rule, verification gate, highlighted bbox on the
  PDF) is binding; palette, typography, layout, badges and chrome were replaced, and
  `AGENTS.md` §5 updated to match what shipped.
- Pinned visual direction: The Pudding (pudding.cool) editorial data-essay style —
  warm paper ground, ink typography, one saturated highlighter accent, mono kickers,
  footnote culture. Confirmed: warm paper light ground; collapsing-header mechanism
  for the receding UI.

## Evidence on Hand

- `dist_data/corpus.parquet` (91,130 rows, 64 clusters, ZSTD) and 64 cluster files,
  `dist_data/centroids.json`, `dist_data/pdfs/` (283 web-served PDFs); mirrored to
  the HF dataset repo (1.03 GB, 285 files, 206-range verified).
- Docket source PDFs in `raw_pdfs/` (283 files; `GetDocument.aspx` excluded — it is
  a PowerPoint, not a PDF).

## Product Principles

1. Evidence first: results and original documents are the interface; chrome recedes
   unless it is needed for the next action.
2. Nothing on screen that cannot be verified by clicking through to the page.
3. Speak lawyer, not machine: provenance is translated into badge/sentence language.
4. One accent does the pointing: a single highlighter color marks matches, citations,
   and the spot on the page — everything else is ink on paper.
5. Calm and authoritative over clever: the tool is trusted with legal conclusions.

## Accessibility & Inclusion

Long-session reading tool for professionals: body text ≥15px, high-contrast ink on
paper, visible focus states, keyboard-operable search and pager (Enter/arrows),
`role=tablist` semantics for the mode switcher.
