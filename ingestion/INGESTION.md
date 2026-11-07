# Build or refresh a WeHPAH! dossier

This guide starts with a folder of PDFs and ends with a checked `dist_data/` folder. You can repeat the build after adding, replacing, or removing PDFs. The builder reuses work that is still valid.

## 1. Prepare the inputs

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if it is not already available. From the project root, put every source PDF in `raw_pdfs/`. Keep the original files there; the builder makes copies for the website. Give files distinct, stable names. Two names that simplify to the same document ID (for example, `A B.pdf` and `A-B.pdf`) will stop the build with a clear error.

Check that you have enough free space for a second copy of the PDF set, the search file, and temporary processing files. The first run may download OCR and language-model dependencies. It can take substantially longer than later runs.

## 2. Build the dossier

Run these commands from the project root:

```sh
uv sync --project ingestion
uv run --project ingestion python ingestion/build_corpus.py \
  --title "My dossier" \
  --description "Short description of the document collection"
```

`--title` is the name shown in WeHPAH!. You can omit `--description`. The builder prints progress for extraction, passage representations, and export. It stops if any PDF fails; it will not publish an incomplete release.

For a quick trial, add `--limit 5`. This creates a **five-document release**, so run the command again without `--limit` before publishing the full dossier. Use `--workers 2` if the machine is short on memory.

## 3. Check the result

The finished `dist_data/` should contain `dossier.json`, `centroids.json`, `corpus.parquet`, `clusters/`, and `pdfs/`. Each file in `clusters/` holds one subject cluster so concept search can fetch small files on browsers that otherwise download a whole Parquet file. Run:

```sh
uv run --project ingestion python ingestion/build_corpus.py --verify-only
```

This checks the manifest, counts, search dimensions, and every saved highlight box. A valid box must have positive width and height and fit on its page. The checks apply to the **current** `dist_data/`; an older release may fail the stricter checks even though the new builder has not run yet.

To preview locally, run:

```sh
uv run --project ingestion python ingestion/devserver.py 8410
```

Open `http://127.0.0.1:8410/`. The range-capable server is needed for PDF and Parquet streaming. If the site is configured to use a Hugging Face repository, the preview will search that repository rather than the local `dist_data/`.

## 4. Publish, if you want others to open it

First check that the PDFs may be shared publicly. A public Hugging Face dataset makes both the PDFs and their extracted text available to anyone. Sign in with the Hugging Face CLI using a write-scoped token stored **outside this project**, create a public dataset repository if needed, then upload `dist_data/`:

```sh
hf auth login
hf repos create YOUR_USERNAME/YOUR_DATASET_NAME --repo-type dataset
hf upload YOUR_USERNAME/YOUR_DATASET_NAME dist_data/ . --repo-type dataset
```

Replace the example names with your own. The site can open a public dossier by its `owner/name` or dataset URL. Check one search result and its original PDF page after uploading. Do not commit tokens or put them in `dossier.json`.

## Later runs and recovery

- Add, replace, or remove PDFs in `raw_pdfs/`, then run the same build command. The next release contains exactly the selected inputs; removed PDFs do not linger.
- Successful extraction is reused only when the PDF bytes, document metadata, OCR settings, platform, and relevant tool versions still match. Passage representations are reused by text and model revision. Clusters and the final release are rebuilt for the selected inputs.
- A failed PDF is retried on the next run. `--force` re-extracts every PDF. Delete `ingestion/.cache/` only if you want to discard all checkpoints.
- The existing `dist_data/` stays in place until a new release passes validation. If a build stops during the final directory swap, inspect `.dist-data-previous/` and restore it to `dist_data/` before retrying. The builder refuses to overwrite that recovery copy.

The lightweight ingestion checks run with `python3 -m unittest discover -s ingestion/tests -v`.

On macOS the builder checks Apple Vision OCR in the main process and in each extraction worker before use. If this check reports no recognized text, run the builder in an environment where macOS Vision is permitted; restricted sandboxes can otherwise return no OCR text without an error. The release records the OCR backend and full-page setting in its build metadata.
