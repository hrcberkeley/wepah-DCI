# Investigations Lab's WeHPAH! (We have pinpoint at home!)

GitHub Pages deployment of the WeHPAH! dossier explorer — a static, zero-backend site.
Live at **https://hrcberkeley.github.io/wepah-DCI/** once Pages is enabled for `main`.

All heavyweight artifacts (283 source PDFs, the 93k-row Parquet index, and cluster
centroids) stream from the Hugging Face dataset repo
[`kolenyo/MS_Datacenter`](https://huggingface.co/datasets/kolenyo/MS_Datacenter) —
nothing here but the vanilla-JS frontend.

- Search modes: exact words · concepts · ask the dossier (extractive, cited)
- Sources: DuckDB-Wasm + Datasets-Server, PDF.js range streaming, in-browser embeddings
- Project source & ingestion pipeline: see the local project root (`AGENTS.md`, `README.md`, `ingestion/`, `web_app/`)
