# Build or refresh a WeHPAH! dossier

One command, `./wehpah`, turns a folder of PDFs into a checked dossier, previews it, and publishes it. Run it from the project root. It needs [uv](https://docs.astral.sh/uv/getting-started/installation/); the first run installs everything else.

## The quick way: the Studio

```sh
./wehpah
```

This opens the **WeHPAH! Studio** in your browser (`http://127.0.0.1:8410/studio`). It walks through four steps on one page:

1. **The dossier**: its title, an optional description, the folder that holds the PDFs (*Choose folder…* opens your computer's folder picker, or paste a path; any folder works except the output folder itself), and the Hugging Face dataset to publish to.
2. **Documents**: every PDF in the folder, marked *Read* (already processed), *New*, or *Could not read*, with the reason. Add PDFs with the button or by dropping them on the list (a file with the same name replaces the old one). *Remove* moves a PDF into a `Removed PDFs` subfolder, so nothing is deleted. *Show folder* opens the folder on your computer.
3. **Build**: one button, with progress per PDF and a short report at the end (including pages with no readable text, which are worth checking against the originals).
4. **Preview & publish**: open the explorer on the new build, then publish it after confirming the documents may be made public.

The Studio only listens on your own computer. Keep the terminal open while it runs; press Ctrl+C there to stop it.

## The same steps from the terminal

```sh
./wehpah build          # read the PDFs, build the release, check it
./wehpah preview        # open the explorer on the local build
./wehpah login          # once: sign in to Hugging Face
./wehpah publish        # upload the checked release
./wehpah check          # re-check the current release at any time
```

`build --limit 5` makes a quick **trial** from the first five PDFs; `publish` refuses a trial build. `build --force` reads every PDF again. `./wehpah --dir path/to/folder …` works on a dossier kept somewhere else.

On Windows, replace `./wehpah` with `uv run --project ingestion python ingestion/wehpah.py`.

## Settings: `dossier.toml`

Each dossier has one small settings file next to its PDFs. Only `title` is required; the Studio or the first `build` creates the file for you.

```toml
title = "My dossier"
description = "Short description of the document collection"
# pdfs = "raw_pdfs"            # folder of source PDFs (default: raw_pdfs/ if present, else this folder)
# output = "dist_data"         # where the finished release is written
# cache = ".wehpah-cache"      # saved work, so rebuilds only read new or changed PDFs
# publish_to = "owner/name"    # Hugging Face dataset

[filenames]                    # optional: pull titles and dates out of file names
title = '^TN\d+_\d{8}T\d{6}_(.+)$'
date = '^TN\d+_(\d{8})T'       # must capture YYYYMMDD

[advanced]                     # optional
workers = "auto"               # PDFs read at once; lower it if memory is short
full_page_ocr = false          # read every page as an image (slower; for poor scans)
```

Paths are relative to the file. Unknown keys and malformed patterns stop with a clear message instead of being silently ignored. Without filename patterns, the title is the file name and the date comes from the PDF's own metadata, or "Undated".

## Preparing the PDFs

Put every source PDF in the PDF folder and keep the originals there; the builder copies them into the release. Give files distinct, stable names: two names that simplify to the same document ID (for example `A B.pdf` and `A-B.pdf`) stop the build with a clear error. Make sure there is room for a second copy of the PDFs. The first run downloads text-recognition and search models and takes much longer than later runs.

## What the build guarantees

- **Nothing half-finished is ever live.** The new release is built and checked in a temporary folder next to the output, and only then swapped in. A failed PDF stops the build and leaves the existing release untouched; the Studio and the terminal name the file and the reason.
- **Only new work is done.** Each PDF's extracted text is saved under the cache folder, keyed by the PDF's exact bytes, its title and date, the text-recognition settings and tool versions. Passage search data is saved in a single `vectors.duckdb` file keyed by the passage text and model revision. Removed PDFs disappear from the next release.
- **Every release is checked**: counts, the manifest, the search files, that every original PDF is present, and that every highlight box has positive size and fits on its page.
- **Interrupted swaps recover themselves.** If a build is stopped exactly while swapping folders, the next run restores the previous release (or, if the new one checks out, removes the leftover backup).
- On macOS the builder confirms that Apple Vision text recognition works before reading any new PDF; restricted sandboxes can otherwise return no text without an error.

## Publishing

First check that the PDFs may be shared publicly: a public Hugging Face dataset makes the PDFs and all their extracted text available to anyone. `./wehpah login` stores your write-scoped token in your user folder, never in this project. `publish` checks the release, creates the dataset if needed (public, since the explorer can only open public datasets), uploads the release, removes files from earlier releases that are no longer part of the dossier, and prints the link. Files already on Hugging Face are not uploaded again.

Open a published dossier with the explorer's dossier switcher or by adding `?repo=owner/name` to its address.

## Tests

```sh
uv run --project ingestion python -m unittest discover -s ingestion/tests -v
```

The older entry point `uv run --project ingestion python ingestion/build_corpus.py [--verify-only]` still works and forwards to `wehpah build` / `wehpah check`.
