# AGENTS.md — Master Build Specification: Zero-Download Legal Dossier Explorer

## 0. ROLE & CRITICAL EXECUTION RULES
You are a deterministic software builder executing a strict specification. Follow these rules without exception:
1. **No Hallucinated Schemas:** Use the exact column names, data types, and JSON keys defined in Section 2. Do not rename fields between Python and JavaScript.
2. **Coordinate Normalization Rule:** PDF coordinates and HTML canvas coordinates use different origins. You MUST normalize all bounding boxes in Python to top-left relative percentages (`0.0` to `1.0` of page width and height) before saving to Parquet. The frontend must NEVER calculate raw PDF points.
3. **Zero AI Jargon in the UI:** The target users are non-technical lawyers. You are strictly forbidden from rendering words like "Vector", "Embedding", "Cosine Distance", "Chunk", "Top-K", or "LLM" anywhere in the user interface.
4. **DuckDB Everywhere:** Use Python `duckdb` to build the sorted, partitioned Parquet file during ingestion, and `@duckdb/duckdb-wasm` in the browser to query remote Parquet row groups via HTTP Range Requests.

---

## 1. DIRECTORY STRUCTURE TO GENERATE
Create and maintain this exact project layout:
```text
project-root/
├── raw_pdfs/                  # Input folder containing raw PDF files (scanned & digital)
├── ingestion/
│   ├── pyproject.toml         # Managed via uv
│   └── build_corpus.py        # End-to-end OCR, VLM captioning, clustering & DuckDB Parquet export
├── dist_data/                 # Output folder to upload to Hugging Face Dataset repo
│   ├── corpus.parquet         # Sorted by cluster_id with fixed ROW_GROUP_SIZE
│   ├── centroids.json         # K-means cluster centers for zero-download browser routing
│   └── pdfs/                  # Web-optimized copies of raw_pdfs for PDF.js range streaming
├── index.html                 # Static Vanilla JS frontend at repo root (GitHub Pages)
├── style.css
├── src/
│   ├── main.js                # UI controller & progressive disclosure state machine
│   ├── db_client.js           # DuckDB-Wasm + HF Datasets Server hybrid query engine
│   ├── verifier.js            # Deterministic quote-grounding verification gate
│   └── pdf_stage.js           # PDF.js renderer with normalized SVG bounding-box overlays
```

---

## 2. THE DATA CONTRACT (PYTHON TO JAVASCRIPT)

### 2.1 DuckDB Table Schema (`corpus.parquet`)
The Python script MUST create a DuckDB table and export it to `dist_data/corpus.parquet` with this exact DDL:
```sql
CREATE TABLE corpus (
    passage_id VARCHAR PRIMARY KEY,   -- Format: "{doc_slug}_p{page_num}_i{item_idx}"
    doc_id VARCHAR,                   -- Clean filename slug
    doc_title VARCHAR,                -- Human-readable document title
    doc_date VARCHAR,                 -- Extracted or file-modified date (YYYY-MM-DD or "Undated")
    page_number INTEGER,              -- 1-indexed page number
    content_type VARCHAR,             -- ENUM: 'text', 'table', 'visual_evidence'
    passage_text VARCHAR,             -- Clean text, Markdown table, or objective image description
    cluster_id INTEGER,               -- K-Means cluster ID (0 to 63) for HTTP Range Request routing
    embedding FLOAT[384],             -- Fixed-length normalized vector (Snowflake/arctic-embed-xs or bge-small-en-v1.5)
    bbox_x_pct FLOAT,                 -- Left edge: 0.0 to 1.0 (fraction of page width from left)
    bbox_y_pct FLOAT,                 -- Top edge: 0.0 to 1.0 (fraction of page height from TOP)
    bbox_w_pct FLOAT,                 -- Box width: 0.0 to 1.0 (fraction of page width)
    bbox_h_pct FLOAT                  -- Box height: 0.0 to 1.0 (fraction of page height)
);
```

### 2.2 Centroid Routing File (`centroids.json`)
To prevent the browser from downloading the entire `embedding` column over the network, `build_corpus.py` must export `dist_data/centroids.json`:
```json
{
  "model_id": "Xenova/bge-small-en-v1.5",
  "dimensions": 384,
  "clusters": [
    { "cluster_id": 0, "centroid": [0.012, -0.043, ...] },
    { "cluster_id": 1, "centroid": [-0.021, 0.088, ...] }
  ]
}
```

---

## 3. PHASE 1: PYTHON INGESTION PIPELINE (`ingestion/build_corpus.py`)

### Step 3.1: Environment Setup
Initialize `ingestion/` using `uv`:
```bash
uv init ingestion
cd ingestion
uv add docling duckdb sentence-transformers scikit-learn pillow pypdfium2 huggingface_hub
```

### Step 3.2: Layout-Aware Extraction & Top-Left Coordinate Normalization
1. Iterate over every `.pdf` file in `../raw_pdfs/`. Copy each file into `../dist_data/pdfs/{doc_id}.pdf`.
2. Configure `docling.document_converter.DocumentConverter` with OCR enabled (`do_ocr=True`, `do_table_structure=True`).
3. For every extracted document item (paragraph, heading, table, or picture):
   - Extract `page_no` and the bounding box `bbox` along with the page's `width` and `height`.
   - **CRITICAL COORDINATE CONVERSION:** Check `bbox.coord_origin`. If `coord_origin` is `BOTTOMLEFT`, convert to top-left percentages using:
     - `bbox_x_pct = bbox.l / page_width`
     - `bbox_y_pct = (page_height - bbox.t) / page_height`
     - `bbox_w_pct = (bbox.r - bbox.l) / page_width`
     - `bbox_h_pct = (bbox.t - bbox.b) / page_height`
     If `coord_origin` is `TOPLEFT`, use:
     - `bbox_x_pct = bbox.l / page_width`
     - `bbox_y_pct = bbox.t / page_height`
     - `bbox_w_pct = (bbox.r - bbox.l) / page_width`
     - `bbox_h_pct = (bbox.b - bbox.t) / page_height`
   - Clamp all four percentage values between `0.0` and `1.0`.
4. **Handling Visual Evidence (Images/Stamps/Signatures):** When an item is a `PictureItem`, prefix `passage_text` with `"[Visual Evidence on Page {page_no}]: "` followed by any OCR text or caption inside that figure region. Set `content_type = 'visual_evidence'`.

### Step 3.3: Embedding, K-Means Clustering & Sorted Parquet Export
1. Embed all `passage_text` rows using `sentence-transformers` with `BAAI/bge-small-en-v1.5` (`normalize_embeddings=True`, 384 dimensions).
2. Run `sklearn.cluster.MiniBatchKMeans(n_clusters=min(64, max(1, len(rows) // 50)), random_state=42)` on the embeddings. Assign each row its integer `cluster_id` and write `centroids.json`.
3. Insert all rows into DuckDB, **sort by `cluster_id`**, and export with small row groups so `DuckDB-Wasm` HTTP Range Requests only download the exact byte slices needed:
```sql
COPY (
    SELECT * FROM corpus ORDER BY cluster_id ASC, doc_id ASC, page_number ASC
) TO '../dist_data/corpus.parquet' (FORMAT PARQUET, ROW_GROUP_SIZE 500, COMPRESSION ZSTD);
```

### Step 3.4: Authenticated Upload to Hugging Face Hub via CLI
Once the ingestion script finishes generating `dist_data/`, push the dataset and PDF files to your Hugging Face dataset repository so the browser can stream them via range requests and the Datasets Server API.

1. **Authenticate via CLI:**
   Open your terminal and run the Hugging Face login command. Paste your User Access Token (with `write` permissions generated from your HF settings):
   ```bash
   huggingface-cli login
   ```
   *(Alternatively, set the environment variable: `export HF_TOKEN="hf_..."`)*

2. **Create the Dataset Repository (if it doesn't exist):**
   ```bash
   huggingface-cli repo create YOUR_DATASET_NAME --type dataset
   ```

3. **Upload the Ingested Artifacts:**
   Use the Hugging Face CLI to upload the entire `dist_data/` folder to your remote repository:
   ```bash
   huggingface-cli upload YOUR_USERNAME/YOUR_DATASET_NAME dist_data/ . --repo-type dataset
   ```
   *Note:* Ensure your repository is public (or if private, ensure your frontend application handles authentication via proxy as outlined in architectural best practices). Once uploaded, Hugging Face will automatically generate Parquet endpoints and enable the `/search` and `/filter` datasets-server REST APIs.

---

## 4. PHASE 2: ZERO-DOWNLOAD HYBRID QUERY ENGINE (`src/db_client.js`)

The browser must NEVER download `corpus.parquet` in full. Implement three specific data access methods in `db_client.js`:

### 4.1 Initialization
1. Load `centroids.json` (~90 KB) on startup and keep it in memory.
2. Initialize `@duckdb/duckdb-wasm` using the jsDelivr CDN bundles.
3. Initialize `@huggingface/transformers` (`pipeline('feature-extraction', 'Xenova/bge-small-en-v1.5', { dtype: 'q8' })`) inside a Web Worker so the UI thread never freezes.

### 4.2 Mode 1: "Find Exact Words" (Keyword Search)
- If `CONFIG.HF_DATASET_REPO` is configured and public, query Hugging Face's Datasets Server BM25 endpoint first (`https://datasets-server.huggingface.co/search?dataset={repo}&config=default&split=train&query={encodeURIComponent(query)}`).
- Fallback / Local Parquet mode: Run a selective DuckDB-Wasm query that projects only metadata columns (skipping the `embedding` column so zero vector bytes are transferred over HTTP):
```sql
SELECT passage_id, doc_id, doc_title, doc_date, page_number, content_type, passage_text,
       bbox_x_pct, bbox_y_pct, bbox_w_pct, bbox_h_pct
FROM read_parquet('${PARQUET_URL}')
WHERE passage_text ILIKE '%' || ? || '%'
LIMIT 25;
```

### 4.3 Mode 2: "Explore Concepts" (Centroid-Routed Semantic Search)
1. Compute the 384-d query embedding in the browser using `Transformers.js`.
2. In plain JavaScript, compute dot-product similarity between the query vector and the centroids in `centroids.json`. Select the top 2 closest `cluster_id` integers (`c1`, `c2`).
3. Execute this exact DuckDB-Wasm query, which uses Parquet `ROW_GROUP_SIZE` predicate pushdown to fetch ONLY the byte ranges for clusters `c1` and `c2`:
```sql
SELECT passage_id, doc_id, doc_title, doc_date, page_number, content_type, passage_text,
       bbox_x_pct, bbox_y_pct, bbox_w_pct, bbox_h_pct,
       array_cosine_similarity(embedding, CAST(? AS FLOAT[384])) AS score
FROM read_parquet('${PARQUET_URL}')
WHERE cluster_id IN (?, ?)
ORDER BY score DESC
LIMIT 15;
```

---

## 5. PHASE 3: PEDAGOGICAL, LOW-COGNITIVE-LOAD UI SPECIFICATION

Non-technical lawyers experience high cognitive fatigue when confronted with complex filters, chat bots that hide sources, or raw database tables. Build a calm, authoritative **Editorial Essay Workspace** (visual direction: The Pudding, pudding.cool) in which the documents and the search results ARE the page and every other element recedes until it is needed. The UI teaches verification through visual affordances.

### 5.1 Visual Design Tokens (`style.css`)
The world is a warm-paper editorial data-essay. Ground and ink carry the structure; ONE gold highlighter accent does all the pointing (marks, selection, active states, the PDF bounding box).
- **Background Canvas:** `#F7F1E6` (warm cream paper) with a recessed well `#EEE6D4` behind the PDF plate.
- **Ink:** `#211B12` (warm near-black; secondary `#5F574A`, faint meta `#6E6455` — all ≥4.5:1 on paper).
- **The One Accent:** `#FDB515` California gold — highlighter marks at `rgba(253,181,21,0.34)`, PDF bbox fill `rgba(253,181,21,0.32)` with `#D99A00` border, small gold-ink text `#7A5200`. No second accent color anywhere.
- **Plates:** `#FFFFFF` surfaces (answer plate, PDF page) with a 1px ink-tinted ring + soft offset shadow (`--shadow-plate`); near-square 2–3px radii.
- **Rules:** hairline `rgba(33,27,18,0.16)` separates entries; a heavier 2.5px ink rule closes the results header (the thick-over-thin rule pair).
- **Typography (Google Fonts, no build step):** `Bricolage Grotesque` 800 display masthead (tracking −0.03em); `Source Serif 4` for passages/quotes (16px/1.62) and italic helper copy; `Spline Sans Mono` for data — page numbers, counts, status, tags, captions (uppercase, tracked); `Archivo` for UI controls. Minimum body font size: `15px`; all line-heights ≥1.3.
- **Browser surfaces are part of the design:** gold text selection, gold caret, thin ink-tinted scrollbars, gold focus rings.
- **Motion:** one authored system — the header folds (0.45s), entries rise with a slight stagger, and the gold marker swipes under exact-word hits; the same gold lands on the PDF box with `pulse-gold 1.2s`. Respect `prefers-reduced-motion`.

### 5.2 Header: Essay Cover that Folds Away (Progressive Disclosure)
Do NOT show advanced settings, sliders, or model pickers. Before the first search, the header is a centered **essay cover**: the `WeHPAH!` masthead (the "!" carries a gold marker swipe), the serif-italic tagline, a mono docket stat line (real counts), one large serif search input on a hairline underline that turns gold on focus, an ink Search button, and **three numbered intent pills** beneath. Switching pills updates the placeholder and the single-sentence helper micro-copy underneath (a serif-italic footnote with a small gold marker):

| Pill Label | Search Placeholder | Helper Micro-Copy Shown Below Input |
|---|---|---|
| **1. Find Exact Words** *(Default)* | `Search names, dates, statute numbers, or exact phrases...` | *"Finds documents containing your exact words, just like Ctrl+F across every file at once."* |
| **2. Explore Concepts** | `Describe a topic or event (e.g., notice of structural defects)...` | *"Finds passages with similar meaning, even if the document uses different wording."* |
| **3. Ask the Dossier** | `Ask a specific question to get a cited summary...` | *"Synthesizes an answer strictly from matching passages. Click any sentence to inspect the original page."* |

After the first search the cover folds into a slim sticky strip (wordmark, compact input with the query, compact pills, an expand chevron that restores the cover; Escape in the input also restores it), giving nearly the whole window to results + page. Mode 3 must remain labeled **experimental** in both states.

### 5.3 Left Pane (2fr): The Evidence Stream
Never display raw similarity scores (e.g., `0.812`). Results are **rule-separated entries, ink on paper — not cards**:
1. **Entry Header:**
   - **Document Title** (bold, 15px, ellipsized, `title` tooltip) + **page** in mono (`p. 14`).
   - **Match Reason Tag** (right-aligned mono small-caps tag):
     - *Find Exact Words*: `Exact word match`.
     - *Explore Concepts* with `score >= 0.72`: `Strong conceptual match`.
     - `score < 0.72`: `Related context`.
     - `content_type === 'visual_evidence'`: `Figure · Stamp` (gold dot — the only adorned tag).
2. **Entry Body (The Verbatim Excerpt):**
   - `passage_text` set in `Source Serif 4` 16px — no borders, no boxes; the excerpt IS the entry.
   - In *Find Exact Words* mode, wrap matching query terms in `<mark class="exact-hit">`; the gold marker swipes under each term (staggered with the entry index).
   - A mono meta footnote line (`doc_date · doc_id.pdf`, ellipsized with tooltip).
3. **Single-Click Interaction:**
   - Hover lifts an entry to a half-white ground; clicking marks it **active** (white plate, ink ring + soft shadow, 3px gold underline under the title) and immediately instructs the Right Pane (`pdf_stage.js`) to open `pdfs/{doc_id}.pdf` at `page_number` and highlight `[bbox_x_pct, bbox_y_pct, bbox_w_pct, bbox_h_pct]`.
   - A mono results-count line (`15 PASSAGES · "QUERY"`) heads the stream under a heavier ink rule.

### 5.4 "Ask the Dossier" Plate & Deterministic Verification Gate (`verifier.js`)
When the user selects **Ask the Dossier**:
1. Retrieve the top 6 passages using Hybrid Search (union of *Find Exact Words* and *Explore Concepts*).
2. **Default Zero-Download Extractive Synthesis:** If WebGPU/LLM synthesis is disabled or unavailable on the user's machine, group the top passages by document and display an **"Extracted Key Passages"** white answer plate at the top of the Left Pane. The plate carries the question restated in serif italic, then numbered claims — each the exact sentence most relevant to the question — separated by hairlines, with the claim number in gold mono hanging in the margin.
3. **If a Local/API Model is Enabled:** Prompt the model to return ONLY JSON matching:
   `{ "claims": [{ "statement": "...", "passage_id": "...", "verbatim_quote": "..." }] }`.
4. **Mandatory Verification Gate (`verifier.js`):** Before rendering any claim on screen:
   - Normalize whitespace on both `claim.verbatim_quote` and the source `passage.passage_text`.
   - Check if `passage_text.toLowerCase().includes(verbatim_quote.toLowerCase())`.
   - **If FALSE:** Discard the claim silently. Never render unverified text.
   - **If TRUE:** Render the claim sentence followed, on its own line, by an interactive gold citation chip `verify · Doc Title, p. X` (gold-ink mono, gold border). Hovering the chip displays the exact `verbatim_quote` in an ink tooltip; clicking it opens the cited page with the PDF bounding box on the right pane.

### 5.5 Right Pane (3fr): The Grounding Stage (`pdf_stage.js`)
The Right Pane must make source verification effortless:
1. **Ghost Context Header:** paper ground, hairline bottom rule: the open Document Title (600 weight, ellipsized), a mono `Page X of Y` counter with `< Prev` / `Next >` ghost buttons (authored inline SVG chevrons), a mono Zoom toggle (`Fit Width` / `100%` / `150%`), and a mono `"Original PDF"` download link. Controls sit at 60% ink until hover — chrome recedes until needed.
2. **On-Demand PDF Streaming:** Load `pdfjs-dist` (`PDFJS.getDocument({ url: pdfUrl, disableAutoFetch: true, disableStream: false })`). Setting `disableAutoFetch: true` forces PDF.js to use HTTP Range Requests so opening page 45 of a 300-page PDF downloads only page 45.
3. **The Plate:** the white PDF page sits centered on the recessed well like a figure — ink ring + soft offset shadow — with a mono uppercase caption beneath: `fig. — {doc_title}, p. X`.
4. **Zoom-Proof Percentage Overlay (`div.bbox-overlay-layer`):**
   - Place an absolutely positioned `<div class="bbox-overlay-layer">` directly over the `<canvas>` inside a `position: relative` wrapper of the exact same width and height as the rendered canvas.
   - When highlighting a passage, create a `<div class="evidence-highlight-box">` using CSS percentages directly from the DuckDB row (no pixel conversion math required):
     ```javascript
     box.style.left   = `${row.bbox_x_pct * 100}%`;
     box.style.top    = `${row.bbox_y_pct * 100}%`;
     box.style.width  = `${row.bbox_w_pct * 100}%`;
     box.style.height = `${row.bbox_h_pct * 100}%`;
     ```
   - Apply the `pulse-gold 1.2s ease-out` keyframe when the box first appears, and center the box by scrolling **only the stage scroll container** — never `box.scrollIntoView`, which scrolls every scrollable ancestor and can push the whole page sideways — so the lawyer never has to hunt for the cited paragraph on a dense legal page.

---

## 6. SELF-VERIFICATION CHECKLIST FOR THE AGENT
Before declaring the task complete, run and verify all four checks:
1. [ ] Run `uv run python ingestion/build_corpus.py` and verify `dist_data/corpus.parquet` and `dist_data/centroids.json` exist and are non-empty.
2. [ ] Run a Python assertion checking that `0.0 <= bbox_x_pct <= 1.0` and `0.0 <= bbox_y_pct <= 1.0` for 100% of rows in `corpus.parquet`.
3. [ ] Authenticate via `huggingface-cli login` and upload `dist_data/` to your HF dataset repo. Verify in the browser Network tab that running a Concept Search executes HTTP `206 Partial Content` range requests against `corpus.parquet` rather than downloading the full file.
4. [ ] Click a search result card in the Left Pane and verify that the Right Pane renders the PDF page and positions the gold highlight box directly over the cited text.
