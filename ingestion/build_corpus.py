#!/usr/bin/env python3
"""build_corpus.py — the dossier builder engine behind `wehpah build`.

  PDF folder  ->  output/{dossier.json, corpus.parquet, clusters/, centroids.json, pdfs/}

Stages (checkpointed in the cache folder so re-runs skip finished work):
  A. Layout-aware extraction (docling) with top-left percentage bbox normalization
  B. Passage representations (BAAI/bge-small-en-v1.5, 384-d, normalized)
  C. MiniBatchKMeans subject areas -> centroids.json
  D. DuckDB insert + sorted Parquet export (ROW_GROUP_SIZE 500, ZSTD)

Run it through the `wehpah` command (see INGESTION.md). Running this file directly
still works and is forwarded to `wehpah build` / `wehpah check`.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

INGESTION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = INGESTION_DIR.parent

EMBED_MODEL_ID = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
CENTROIDS_MODEL_ID = "Xenova/bge-small-en-v1.5"  # browser-side counterpart
RANDOM_STATE = 42
MAX_CLUSTERS = 64
CLUSTER_DIVISOR = 50
CACHE_VERSION = 3

# Exact DDL from the AGENTS.md data contract (Section 2.1)
CORPUS_DDL = """
CREATE TABLE corpus (
    passage_id VARCHAR PRIMARY KEY,
    doc_id VARCHAR,
    doc_title VARCHAR,
    doc_date VARCHAR,
    page_number INTEGER,
    content_type VARCHAR,
    passage_text VARCHAR,
    cluster_id INTEGER,
    embedding FLOAT[384],
    bbox_x_pct FLOAT,
    bbox_y_pct FLOAT,
    bbox_w_pct FLOAT,
    bbox_h_pct FLOAT
)
"""


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def versions(*packages: str) -> dict:
    result = {}
    for package in packages:
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "not-installed"
    return result


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, sort_keys=True)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_slug_collisions(paths: list[Path]) -> None:
    slugs = {}
    for path in paths:
        slug = slugify(path.name)
        if slug in slugs:
            raise ValueError(f"PDF filename collision: {slugs[slug]!r} and {path.name!r} both map to {slug!r}")
        slugs[slug] = path.name


def validate_rows(rows: list[dict]) -> None:
    seen = set()
    for row in rows:
        pid = row["passage_id"]
        if pid in seen:
            raise ValueError(f"Duplicate passage ID: {pid}")
        seen.add(pid)
        x, y, w, h = (float(row[k]) for k in ("bbox_x_pct", "bbox_y_pct", "bbox_w_pct", "bbox_h_pct"))
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in (x, y, w, h)) or w <= 0 or h <= 0 or x + w > 1.000001 or y + h > 1.000001:
            raise ValueError(f"Invalid bounding box for {pid}: {(x, y, w, h)}")


# Passages shorter than this (headings, labels, list items, stray numbers) are
# merged into the text that follows them on the same page, so a heading travels
# with its paragraph and the embedding carries that context.
MIN_PASSAGE_CHARS = 200
MAX_PASSAGE_CHARS = 1500


def merge_short_passages(rows: list[dict]) -> list[dict]:
    """Greedy, order-preserving: keep appending consecutive same-page text rows
    while the passage is still short. Tables and figures are never merged.
    The merged passage keeps the first item's ID and the union of the boxes."""
    # ponytail: length heuristic, not docling's heading label (not in the cache);
    # store item labels at extraction if merges ever join unrelated columns.
    out: list[dict] = []
    for row in rows:
        prev = out[-1] if out else None
        if (prev and row["content_type"] == "text" and prev["content_type"] == "text"
                and prev["doc_id"] == row["doc_id"] and prev["page_number"] == row["page_number"]
                and len(prev["passage_text"]) < MIN_PASSAGE_CHARS
                and len(prev["passage_text"]) + len(row["passage_text"]) <= MAX_PASSAGE_CHARS):
            x0 = min(prev["bbox_x_pct"], row["bbox_x_pct"])
            y0 = min(prev["bbox_y_pct"], row["bbox_y_pct"])
            x1 = max(prev["bbox_x_pct"] + prev["bbox_w_pct"], row["bbox_x_pct"] + row["bbox_w_pct"])
            y1 = max(prev["bbox_y_pct"] + prev["bbox_h_pct"], row["bbox_y_pct"] + row["bbox_h_pct"])
            out[-1] = {**prev, "passage_text": f'{prev["passage_text"]}\n{row["passage_text"]}',
                       "bbox_x_pct": x0, "bbox_y_pct": y0,
                       "bbox_w_pct": min(1.0, x1) - x0, "bbox_h_pct": min(1.0, y1) - y0}
        else:
            out.append(dict(row))
    return out


def verify_ocr_available() -> None:
    """Fail early when macOS Vision silently returns no observations (e.g. sandbox denial)."""
    if sys.platform != "darwin":
        return
    from PIL import Image, ImageDraw, ImageFont
    from ocrmac import ocrmac
    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    font = ImageFont.truetype(font_path, 48)
    image = Image.new("RGB", (1000, 160), "white")
    ImageDraw.Draw(image).text((30, 35), "DOCUMENT EVIDENCE 12345", fill="black", font=font)
    with tempfile.NamedTemporaryFile(suffix=".png") as probe:
        image.save(probe.name)
        observations = ocrmac.OCR(probe.name, language_preference=["en-US"]).recognize()
    text = " ".join(str(item[0]) for item in observations).upper()
    if "DOCUMENT" not in text or "EVIDENCE" not in text:
        raise RuntimeError("macOS Vision OCR preflight failed; run ingestion outside a sandbox that blocks Vision. No release was changed.")


# ---------------------------------------------------------------------------
# Document names, titles and dates
# ---------------------------------------------------------------------------

def _stem(filename: str) -> str:
    stem = Path(filename).stem
    # Some docket downloads end in ".pd.pdf" — strip that artifact.
    return stem[: -len(".pd")] if stem.lower().endswith(".pd") else stem


def slugify(filename: str) -> str:
    """Clean filename -> stable doc slug (lowercase alphanumerics + dashes)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", _stem(filename)).strip("-").lower()
    return slug or "document"


def humanize_title(filename: str, title_re: re.Pattern | None = None) -> str:
    """Title from the filename; `title_re`'s first group wins when it matches."""
    stem = _stem(filename)
    m = title_re.search(stem) if title_re else None
    title = m.group(1) if m and m.group(1) else stem
    return re.sub(r"\s+", " ", title).strip() or stem


def extract_date(filename: str, pdf_path: Path, date_re: re.Pattern | None = None) -> str:
    """Use a document date only when the name (via `date_re`, YYYYMMDD) or PDF metadata supplies one."""
    m = date_re.search(Path(filename).stem) if date_re else None
    if m and re.fullmatch(r"\d{8}", m.group(1) or ""):
        d = m.group(1)
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    try:
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(str(pdf_path)) as pdf:
            meta = pdf.get_metadata_dict() or {}
            created = meta.get("CreationDate") or ""
            m2 = re.search(r"D:(\d{4})(\d{2})?(\d{2})?", created)
            if m2:
                return f"{m2.group(1)}-{m2.group(2) or '01'}-{m2.group(3) or '01'}"
    except Exception:
        pass
    # File modification time describes a copy/download operation, not the
    # document's date, and changes across machines.
    return "Undated"


def list_pdfs(pdf_dir: Path) -> list[Path]:
    if not pdf_dir.is_dir():
        raise FileNotFoundError(f"PDF folder not found: {pdf_dir}")
    files = sorted((p for p in pdf_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"), key=lambda p: p.name)
    check_slug_collisions(files)
    return files


def document_meta(path: Path, settings) -> tuple[str, str, str, str]:
    """(filename, title, date, doc_id) — part of the extraction cache key."""
    return (path.name, humanize_title(path.name, settings.title_re),
            extract_date(path.name, path, settings.date_re), slugify(path.name))


# ---------------------------------------------------------------------------
# Stage A: layout-aware extraction with coordinate normalization
# ---------------------------------------------------------------------------

_CONVERTERS: dict = {}
_OCR_CHECKED = False


def _make_converter(full_page_ocr: bool, pdf_backend: str | None = None):
    """Build a docling DocumentConverter with OCR + table structure enabled."""
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.do_table_structure = True

    # Prefer Apple Vision OCR on macOS (fast, high quality, no big model downloads);
    # fall back to EasyOCR (docling default) elsewhere or if the extra is missing.
    try:
        from docling.datamodel.pipeline_options import OcrMacOptions

        pipeline_options.ocr_options = OcrMacOptions(force_full_page_ocr=full_page_ocr)
    except Exception:
        pipeline_options.ocr_options.force_full_page_ocr = full_page_ocr

    fmt_kwargs = {"pipeline_options": pipeline_options}
    if pdf_backend is not None:
        try:
            from docling.datamodel.base_models import PdfBackend

            fmt_kwargs["backend"] = getattr(PdfBackend, pdf_backend)
        except Exception:
            pass
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(**fmt_kwargs)})


def _get_converter(full_page_ocr: bool, fallback: bool = False):
    key = (full_page_ocr, fallback)
    if key not in _CONVERTERS:
        _CONVERTERS[key] = _make_converter(full_page_ocr, "PYPDFIUM2" if fallback else None)
    return _CONVERTERS[key]


def normalize_bbox(bbox, page_width: float, page_height: float) -> tuple[float, float, float, float]:
    """PDF-space bbox -> top-left relative percentages (0..1). AGENTS.md §3.2.

    Handles both BOTTOMLEFT (PDF-native) and TOPLEFT coordinate origins.
    """
    origin = getattr(bbox, "coord_origin", None)
    l, r = float(bbox.l), float(bbox.r)
    t, b = float(bbox.t), float(bbox.b)
    if page_width <= 0 or page_height <= 0:
        raise ValueError("Page dimensions must be positive")
    if origin is not None and str(origin).upper().endswith("BOTTOMLEFT"):
        x_pct = l / page_width
        y_pct = (page_height - t) / page_height
        w_pct = (r - l) / page_width
        h_pct = (t - b) / page_height
    elif origin is not None and str(origin).upper().endswith("TOPLEFT"):
        x_pct = l / page_width
        y_pct = t / page_height
        w_pct = (r - l) / page_width
        h_pct = (b - t) / page_height
    else:
        raise ValueError(f"Unknown coordinate origin: {origin}")
    clamp = lambda v: max(0.0, min(1.0, v))
    left, top = clamp(x_pct), clamp(y_pct)
    right, bottom = clamp(x_pct + w_pct), clamp(y_pct + h_pct)
    return left, top, right - left, bottom - top


def extract_single_pdf(job: tuple[str, tuple[str, str, str, str], bool]) -> dict:
    """Worker: convert one PDF into row dicts (no representations yet)."""
    global _OCR_CHECKED
    if not _OCR_CHECKED:
        verify_ocr_available()
        _OCR_CHECKED = True
    pdf_path, (filename, doc_title, doc_date, doc_id), full_page_ocr = job
    t0 = time.time()

    from docling_core.types.doc import PictureItem, TableItem

    try:
        conv_res = _get_converter(full_page_ocr).convert(pdf_path)
    except Exception as exc:
        # Resilience: retry problematic scans with the alternative backend.
        print(f"[extract] {filename}: first reader failed ({exc}); retrying with the alternative reader", flush=True)
        conv_res = _get_converter(full_page_ocr, fallback=True).convert(pdf_path)

    doc = conv_res.document
    page_sizes: dict[int, tuple[float, float]] = {}
    for pno, page in doc.pages.items():
        size = getattr(page, "size", None)
        if size is not None and getattr(size, "width", 0) and getattr(size, "height", 0):
            page_sizes[int(pno)] = (float(size.width), float(size.height))

    rows: list[dict] = []
    item_idx = 0
    for item, _level in doc.iterate_items():
        provs = getattr(item, "prov", None) or []
        if not provs:
            continue
        prov = provs[0]
        page_no = int(prov.page_no)
        if page_no not in page_sizes:
            raise ValueError(f"Missing page dimensions for page {page_no} in {filename}")
        page_w, page_h = page_sizes[page_no]

        if isinstance(item, TableItem):
            try:
                text = item.export_to_markdown(doc)
            except Exception as exc:
                raise ValueError(f"Table extraction failed in {filename}, page {page_no}") from exc
            content_type = "table"
        elif isinstance(item, PictureItem):
            caption = ""
            try:
                caption = (item.caption_text(doc) or "").strip()
            except Exception as exc:
                print(f"[extract] {filename}, page {page_no}: caption lookup failed: {exc}", flush=True)
            if not caption:
                try:
                    for c in getattr(item, "captions", None) or []:
                        caption = c.resolve(doc).text or ""
                        if caption.strip():
                            caption = caption.strip()
                            break
                except Exception as exc:
                    print(f"[extract] {filename}, page {page_no}: caption reference failed: {exc}", flush=True)
            text = f"[Visual Evidence on Page {page_no}]: {caption}" if caption else (
                f"[Visual Evidence on Page {page_no}]: (figure, stamp, signature or diagram without extractable text)"
            )
            content_type = "visual_evidence"
        else:
            text = (getattr(item, "text", "") or "").strip()
            content_type = "text"
        # Isolated OCR punctuation and letters add no useful evidence and can
        # arrive with point-sized boxes; do not give them citable passage IDs.
        if not text or (content_type == "text" and len(text.strip()) < 3):
            continue

        x, y, w, h = normalize_bbox(prov.bbox, page_w, page_h)
        rows.append(
            {
                "passage_id": f"{doc_id}_p{page_no}_i{item_idx}",
                "doc_id": doc_id,
                "doc_title": doc_title,
                "doc_date": doc_date,
                "page_number": page_no,
                "content_type": content_type,
                "passage_text": text,
                "bbox_x_pct": x,
                "bbox_y_pct": y,
                "bbox_w_pct": w,
                "bbox_h_pct": h,
            }
        )
        item_idx += 1

    return {
        "doc_id": doc_id,
        "source_filename": filename,
        "doc_title": doc_title,
        "doc_date": doc_date,
        "n_pages": len(page_sizes),
        "rows": rows,
        "seconds": round(time.time() - t0, 1),
    }


def extraction_config(full_page_ocr: bool) -> dict:
    return {"version": CACHE_VERSION, "ocr": True, "table_structure": True, "platform": sys.platform,
            "force_full_page_ocr": full_page_ocr, "tools": versions("docling", "docling-core", "pypdfium2", "ocrmac")}


def extraction_cache_file(cache_dir: Path, source_sha256: str, meta, config: dict) -> tuple[str, Path]:
    key = fingerprint({"source_sha256": source_sha256, "metadata": meta, "config": config})
    return key, cache_dir / "extraction-v2" / f"{key}.json"


def stage_extract(docs: list[tuple[Path, tuple]], cache_dir: Path, workers: int, force: bool,
                  full_page_ocr: bool = False, hashes: dict | None = None) -> list[dict]:
    """Cache successful extraction by source bytes and extraction configuration."""
    config = extraction_config(full_page_ocr)
    hashes = hashes or {}
    results, pending = [], []
    for path, meta in docs:
        key, cache_file = extraction_cache_file(cache_dir, hashes.get(path.name) or sha256_file(path), meta, config)
        if cache_file.exists() and not force:
            try:
                payload = json.loads(cache_file.read_text())
                if payload.get("cache_key") == key and not payload.get("error") and payload.get("rows"):
                    validate_rows(payload["rows"])
                    results.append(payload)
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                print(f"[extract] unreadable saved result for {path.name}; reading it again", flush=True)
        pending.append(((str(path), tuple(meta), full_page_ocr), key, cache_file))
    print(f"[extract] {len(docs)} documents · {len(results)} already read · {len(pending)} to read", flush=True)
    failures = []
    if pending:
        verify_ocr_available()
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(extract_single_pdf, job): (job, key, path) for job, key, path in pending}
            for done, fut in enumerate(as_completed(futures), 1):
                job, key, cache_file = futures[fut]
                name = job[1][0]
                try:
                    payload = fut.result()
                    if not payload.get("rows"):
                        raise ValueError("no readable text was found in this PDF")
                    validate_rows(payload["rows"])
                    payload["cache_key"] = key
                    atomic_json(cache_file, payload)
                    results.append(payload)
                    print(f"[extract] {done}/{len(pending)} {name}: {len(payload['rows'])} passages", flush=True)
                except Exception as exc:
                    failures.append(f"{name}: {exc}")
                    print(f"[extract] {done}/{len(pending)} FAILED {name}: {exc}", flush=True)
    if failures:
        raise RuntimeError(f"{len(failures)} PDF(s) could not be read; the existing release was left untouched. "
                           "Fix or remove them and build again: " + "; ".join(failures))
    return sorted(results, key=lambda r: r["doc_id"])


# ---------------------------------------------------------------------------
# Stage B/C: passage representations + subject areas
# ---------------------------------------------------------------------------

class VectorCache:
    """One DuckDB file of passage vectors keyed by (model config, text hash).

    Older builds kept one JSON file per passage under embeddings-v2/<config>/;
    those are read when present and copied in, so nothing is recomputed."""

    def __init__(self, cache_dir: Path, config: dict):
        import duckdb
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.config = fingerprint(config)
        self.legacy = cache_dir / "embeddings-v2" / self.config
        self.con = duckdb.connect(str(cache_dir / "vectors.duckdb"))
        self.con.execute(f"CREATE TABLE IF NOT EXISTS vectors (config VARCHAR, text_hash VARCHAR, vector FLOAT[{EMBED_DIM}], PRIMARY KEY (config, text_hash))")

    def get(self, hashes: list[str]) -> dict[str, list[float]]:
        self.con.execute("CREATE OR REPLACE TEMP TABLE wanted AS SELECT unnest(?::VARCHAR[]) AS text_hash", [hashes])
        found = dict(self.con.execute("SELECT v.text_hash, v.vector FROM vectors v JOIN wanted w USING (text_hash) WHERE v.config = ?",
                                      [self.config]).fetchall())
        migrated = {}
        if self.legacy.is_dir():
            for h in hashes:
                if h in found:
                    continue
                try:
                    vector = json.loads((self.legacy / f"{h}.json").read_text())
                except (OSError, ValueError):
                    continue
                if len(vector) == EMBED_DIM and all(math.isfinite(float(v)) for v in vector):
                    migrated[h] = vector
            if migrated:
                print(f"[index] moved {len(migrated)} saved passages into the single cache file", flush=True)
                self.put(migrated)
        return {**found, **migrated}

    def put(self, vectors: dict[str, list[float]]) -> None:
        self.con.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)",
                             [(self.config, h, v) for h, v in vectors.items()])

    def close(self) -> None:
        self.con.close()


def stage_embed_and_cluster(all_rows: list[dict], out_dir: Path, cache_dir: Path) -> str:
    """Attach cluster_id to every row; write centroids.json."""
    import numpy as np

    texts = [r["passage_text"] for r in all_rows]
    from huggingface_hub import snapshot_download
    model_snapshot = Path(snapshot_download(repo_id=EMBED_MODEL_ID))
    model_revision = model_snapshot.name
    embed_config = {"version": CACHE_VERSION, "model": EMBED_MODEL_ID, "revision": model_revision, "dimensions": EMBED_DIM,
                    "normalize": True, "tools": versions("sentence-transformers", "transformers", "torch")}
    text_hashes = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
    cache = VectorCache(cache_dir, embed_config)
    try:
        vectors = cache.get(list(dict.fromkeys(text_hashes)))
        missing = {h: t for h, t in zip(text_hashes, texts) if h not in vectors}
        print(f"[index] {len(set(text_hashes)) - len(missing)} passages already indexed · {len(missing)} new", flush=True)
        if missing:
            from sentence_transformers import SentenceTransformer
            device = "mps" if sys.platform == "darwin" else "cpu"
            try:
                model = SentenceTransformer(str(model_snapshot), device=device)
            except Exception:
                model = SentenceTransformer(str(model_snapshot), device="cpu")
            keys = list(missing)
            step = 2048
            for start in range(0, len(keys), step):
                batch = keys[start:start + step]
                computed = model.encode([missing[k] for k in batch], normalize_embeddings=True, batch_size=64,
                                        show_progress_bar=False, convert_to_numpy=True).astype(np.float32)
                if computed.shape != (len(batch), EMBED_DIM) or not np.isfinite(computed).all():
                    raise ValueError("The passage indexer returned invalid results")
                fresh = {k: [float(v) for v in vec] for k, vec in zip(batch, computed)}
                cache.put(fresh)
                vectors.update(fresh)
                print(f"[index] {min(start + step, len(keys))}/{len(keys)} new passages indexed", flush=True)
    finally:
        cache.close()
    embeddings = np.asarray([vectors[h] for h in text_hashes], dtype=np.float32)

    from sklearn.cluster import MiniBatchKMeans

    n_clusters = min(MAX_CLUSTERS, max(1, len(all_rows) // CLUSTER_DIVISOR))
    print(f"[subjects] grouping passages into {n_clusters} subject areas", flush=True)
    km = MiniBatchKMeans(n_clusters=n_clusters, random_state=RANDOM_STATE, batch_size=min(1024, len(texts)))
    km.fit(embeddings)
    labels = km.labels_.astype(int)

    centroids = {
        "model_id": CENTROIDS_MODEL_ID,
        "dimensions": EMBED_DIM,
        "clusters": [
            {"cluster_id": int(i), "centroid": [round(float(v), 6) for v in km.cluster_centers_[i]]}
            for i in range(len(km.cluster_centers_))
        ],
    }
    atomic_json(out_dir / "centroids.json", centroids)

    for row, cid, vec in zip(all_rows, labels, embeddings):
        row["cluster_id"] = int(cid)
        row["_embedding"] = [float(v) for v in vec]
    return model_revision


# ---------------------------------------------------------------------------
# Stage D: DuckDB insert + sorted Parquet export
# ---------------------------------------------------------------------------

def stage_export(all_rows: list[dict], out_dir: Path) -> None:
    import duckdb

    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = out_dir / "corpus.parquet"
    con = duckdb.connect(":memory:")
    con.execute(CORPUS_DDL)

    insert_sql = """
        INSERT INTO corpus VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?,
            CAST(? AS FLOAT[384]),
            ?, ?, ?, ?
        )
    """
    batch: list[tuple] = []
    for r in all_rows:
        batch.append((
            r["passage_id"], r["doc_id"], r["doc_title"], r["doc_date"],
            int(r["page_number"]), r["content_type"], r["passage_text"],
            int(r["cluster_id"]), r["_embedding"],
            float(r["bbox_x_pct"]), float(r["bbox_y_pct"]),
            float(r["bbox_w_pct"]), float(r["bbox_h_pct"]),
        ))
        if len(batch) >= 500:
            con.executemany(insert_sql, batch)
            batch = []
    if batch:
        con.executemany(insert_sql, batch)

    n = con.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
    assert n == len(all_rows), f"row count mismatch: {n} != {len(all_rows)}"

    print(f"[export] writing {n} passages", flush=True)
    safe_corpus = corpus_path.as_posix().replace("'", "''")
    con.execute(
        f"""
        COPY (SELECT * FROM corpus ORDER BY cluster_id ASC, doc_id ASC, page_number ASC, passage_id ASC)
        TO '{safe_corpus}'
        (FORMAT PARQUET, ROW_GROUP_SIZE 500, COMPRESSION ZSTD)
        """
    )
    shard_dir = out_dir / "clusters"
    shard_dir.mkdir(parents=True, exist_ok=True)
    cluster_ids = [int(row[0]) for row in con.execute("SELECT DISTINCT cluster_id FROM corpus ORDER BY cluster_id").fetchall()]
    for cluster_id in cluster_ids:
        if not 0 <= cluster_id < MAX_CLUSTERS:
            raise ValueError(f"Invalid cluster ID: {cluster_id}")
        shard_path = shard_dir / f"cluster_{cluster_id:02d}.parquet"
        safe_path = str(shard_path).replace("'", "''")
        con.execute(f"""COPY (SELECT * FROM corpus WHERE cluster_id = {cluster_id}
                     ORDER BY doc_id ASC, page_number ASC, passage_id ASC)
                     TO '{safe_path}' (FORMAT PARQUET, ROW_GROUP_SIZE 500, COMPRESSION ZSTD)""")
    con.close()


def verify_shards(con, shard_dir: Path, expected_rows: int) -> None:
    """Check every shard against the corpus before a release is published."""
    expected = {int(row[0]): int(row[1]) for row in con.execute(
        "SELECT cluster_id, COUNT(*) FROM corpus GROUP BY cluster_id").fetchall()}
    expected_names = {f"cluster_{cid:02d}.parquet" for cid in expected}
    actual_names = {p.name for p in shard_dir.glob("*.parquet")} if shard_dir.is_dir() else set()
    if actual_names != expected_names:
        raise ValueError(f"Shard files do not match corpus clusters: expected {sorted(expected_names)}, found {sorted(actual_names)}")
    schema = con.execute("DESCRIBE SELECT * FROM corpus").fetchall()
    total = 0
    for cid, count in sorted(expected.items()):
        safe_path = str(shard_dir / f"cluster_{cid:02d}.parquet").replace("'", "''")
        relation = f"read_parquet('{safe_path}')"
        if con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall() != schema:
            raise ValueError(f"Shard {cid} has a different schema")
        found, wrong = con.execute(f"SELECT COUNT(*), COUNT(*) FILTER (WHERE cluster_id IS NULL OR cluster_id <> ?) FROM {relation}", [cid]).fetchone()
        if found != count or wrong:
            raise ValueError(f"Shard {cid} has invalid row count or membership")
        total += found
    if total != expected_rows:
        raise ValueError(f"Shard row count {total} does not match corpus {expected_rows}")


def verify_corpus(out_dir: Path) -> dict:
    """Self-verification checklist (AGENTS.md §6 items 1-2). Returns the manifest."""
    import duckdb

    corpus_path = out_dir / "corpus.parquet"
    centroids_path = out_dir / "centroids.json"
    manifest_path = out_dir / "dossier.json"
    for path in (corpus_path, centroids_path, manifest_path):
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"The release is missing {path.name}")
    con = duckdb.connect()
    con.execute(f"CREATE VIEW corpus AS SELECT * FROM read_parquet('{corpus_path.as_posix().replace(chr(39), chr(39) * 2)}')")
    n = con.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("dimensions") != EMBED_DIM or manifest.get("model_id") != CENTROIDS_MODEL_ID:
        raise ValueError("Unsupported or incompatible dossier manifest")
    if manifest.get("passage_count") != n:
        raise ValueError("Manifest passage count does not match corpus")
    pdf_dir = out_dir / manifest.get("pdfs_path", "pdfs")
    docs = {r[0] for r in con.execute("SELECT DISTINCT doc_id FROM corpus").fetchall()}
    missing_pdfs = sorted(d for d in docs if not (pdf_dir / f"{d}.pdf").is_file())
    if missing_pdfs:
        raise ValueError(f"The release is missing original PDFs for: {', '.join(missing_pdfs[:5])}")
    centroids = json.loads(centroids_path.read_text())
    if centroids.get("dimensions") != EMBED_DIM or centroids.get("model_id") != CENTROIDS_MODEL_ID:
        raise ValueError("Centroid model does not match the corpus contract")
    clusters = centroids.get("clusters", [])
    if not clusters or any(len(c.get("centroid", [])) != EMBED_DIM for c in clusters):
        raise ValueError("Invalid centroid dimensions")
    if not {r[0] for r in con.execute("SELECT DISTINCT cluster_id FROM corpus").fetchall()} <= {c["cluster_id"] for c in clusters}:
        raise ValueError("Centroid IDs do not match corpus clusters")
    if "clusters_path" in manifest:
        if manifest["clusters_path"] != "clusters":
            raise ValueError("Unsupported cluster shard path")
        verify_shards(con, out_dir / "clusters", n)
    assert n > 0, "corpus.parquet is empty"

    bad = con.execute(
        """
        SELECT COUNT(*) FROM corpus WHERE
            bbox_x_pct < 0.0 OR bbox_x_pct > 1.0 OR
            bbox_y_pct < 0.0 OR bbox_y_pct > 1.0 OR
            bbox_w_pct < 0.0 OR bbox_w_pct > 1.0 OR
            bbox_h_pct <= 0.0 OR bbox_h_pct > 1.0 OR bbox_w_pct <= 0.0 OR
            bbox_x_pct + bbox_w_pct > 1.000001 OR bbox_y_pct + bbox_h_pct > 1.000001 OR
            bbox_x_pct IS NULL OR bbox_y_pct IS NULL OR bbox_w_pct IS NULL OR bbox_h_pct IS NULL
        """
    ).fetchone()[0]
    assert bad == 0, f"{bad} rows have invalid, empty, or out-of-page highlight boxes"

    emb_ok = con.execute("SELECT COUNT(*) FROM corpus WHERE array_length(embedding) <> 384").fetchone()[0]
    assert emb_ok == 0, f"{emb_ok} rows have the wrong search-index size"
    try:
        norms = con.execute(
            "SELECT min(n), max(n) FROM (SELECT sqrt(list_sum(list_transform(embedding, lambda x: x * x))) AS n FROM corpus)"
        ).fetchone()
    except duckdb.Error:  # older DuckDB builds only know the arrow-lambda syntax
        norms = con.execute(
            "SELECT min(n), max(n) FROM (SELECT sqrt(list_sum(list_transform(embedding, x -> x * x))) AS n FROM corpus)"
        ).fetchone()
    if not (0.99 < norms[0] and norms[1] < 1.01):
        raise ValueError("Search index values are not normalized")
    types = dict(con.execute("SELECT content_type, COUNT(*) FROM corpus GROUP BY content_type ORDER BY 2 DESC").fetchall())
    con.close()
    print(f"[verify] OK — {len(docs)} documents · {n} passages · every highlight box fits its page · "
          f"{types.get('text', 0)} text, {types.get('table', 0)} tables, {types.get('visual_evidence', 0)} figures", flush=True)
    return manifest


# ---------------------------------------------------------------------------
# Release: build in a staging folder, verify, then swap in
# ---------------------------------------------------------------------------

def backup_path(output: Path) -> Path:
    return output.parent / f".{output.name.replace('_', '-')}-previous"


def recover_interrupted_swap(output: Path) -> None:
    """Finish or undo a release swap that a crash interrupted."""
    backup = backup_path(output)
    if not backup.exists():
        return
    if not output.exists():
        os.replace(backup, output)
        print(f"[recover] the last build stopped mid-swap; restored the previous release to {output.name}/", flush=True)
        return
    try:
        verify_corpus(output)
    except Exception as exc:
        raise RuntimeError(f"Both {output} and the backup {backup} exist and the current one fails its check ({exc}). "
                           "Inspect them, keep the good one as the output folder, and delete the other.") from exc
    shutil.rmtree(backup)
    print("[recover] removed the backup left by an earlier build; the current release checks out", flush=True)


def pages_without_text(extracted: list[dict]) -> dict[str, list[int]]:
    result = {}
    for doc in extracted:
        seen = {r["page_number"] for r in doc["rows"]}
        empty = [p for p in range(1, int(doc.get("n_pages") or 0) + 1) if p not in seen]
        if empty:
            result[doc["doc_id"]] = empty
    return result


def build(settings, limit: int = 0, force: bool = False, workers: int | None = None) -> dict:
    """Build, verify and publish a release into settings.output. Returns the manifest."""
    output: Path = settings.output
    recover_interrupted_swap(output)
    pdf_files = list_pdfs(settings.pdfs)
    if limit > 0:
        pdf_files = pdf_files[:limit]
    if not pdf_files:
        raise ValueError(f"No PDFs found in {settings.pdfs}")
    print(f"[start] {len(pdf_files)} PDFs from {settings.pdfs}", flush=True)
    docs = [(p, document_meta(p, settings)) for p in pdf_files]
    source_hashes = {p.name: sha256_file(p) for p in pdf_files}
    extracted = stage_extract(docs, settings.cache, workers or settings.workers, force,
                              settings.full_page_ocr, source_hashes)
    if any(sha256_file(p) != source_hashes[p.name] for p in pdf_files):
        raise RuntimeError("A source PDF changed during the build; build again")
    all_rows = [row for doc in extracted for row in doc["rows"]]
    if not all_rows:
        raise ValueError("No passages were extracted")
    validate_rows(all_rows)
    before = len(all_rows)
    all_rows = merge_short_passages(all_rows)
    validate_rows(all_rows)
    print(f"[merge] {before} items -> {len(all_rows)} passages (short items joined to the text that follows)", flush=True)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-staging-", dir=output.parent))
    backup = backup_path(output)
    try:
        (staging / "pdfs").mkdir()
        sources = []
        for p in pdf_files:
            target = staging / "pdfs" / f"{slugify(p.name)}.pdf"
            shutil.copy2(p, target)
            if sha256_file(target) != source_hashes[p.name]:
                raise IOError(f"PDF changed while copying: {p.name}")
            sources.append({"filename": p.name, "doc_id": slugify(p.name), "sha256": source_hashes[p.name]})
        model_revision = stage_embed_and_cluster(all_rows, staging, settings.cache)
        stage_export(all_rows, staging)
        empty_pages = pages_without_text(extracted)
        manifest = {
            "schema_version": 1, "title": settings.title, "description": settings.description,
            "document_count": len(extracted), "passage_count": len(all_rows),
            "model_id": CENTROIDS_MODEL_ID, "dimensions": EMBED_DIM, "config": "default", "split": "train",
            "corpus_path": "corpus.parquet", "centroids_path": "centroids.json", "pdfs_path": "pdfs",
            "clusters_path": "clusters",
            "build": {"cache_version": CACHE_VERSION, "source_files": sources,
                      "trial_limit": limit or None,
                      "pages_without_text": empty_pages,
                      "extraction_tools": versions("docling", "docling-core", "pypdfium2", "ocrmac"),
                      "ocr_backend": "ocrmac/Apple Vision" if sys.platform == "darwin" else "docling default",
                      "force_full_page_ocr": settings.full_page_ocr,
                      "embedding_tools": versions("sentence-transformers", "transformers", "torch"),
                      "embedding_model": EMBED_MODEL_ID, "embedding_revision": model_revision, "cluster_tools": versions("scikit-learn", "duckdb"),
                      "cluster_random_state": RANDOM_STATE, "min_passage_chars": MIN_PASSAGE_CHARS, "max_passage_chars": MAX_PASSAGE_CHARS, "cluster_divisor": CLUSTER_DIVISOR},
        }
        atomic_json(staging / "dossier.json", manifest)
        verify_corpus(staging)
        if output.exists():
            os.replace(output, backup)
        try:
            os.replace(staging, output)
        except BaseException:
            if backup.exists():
                os.replace(backup, output)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    n_empty = sum(len(v) for v in empty_pages.values())
    print(f"[report] {len(extracted)} documents · {len(all_rows)} passages", flush=True)
    if n_empty:
        listed = "; ".join(f"{d} (p. {', '.join(map(str, pages[:8]))}{'…' if len(pages) > 8 else ''})"
                           for d, pages in list(empty_pages.items())[:10])
        print(f"[report] {n_empty} pages in {len(empty_pages)} documents have no readable text — check them against the original: {listed}"
              + (" …" if len(empty_pages) > 10 else ""), flush=True)
    if limit:
        print(f"[report] trial build of the first {limit} PDFs — build again without --limit before publishing", flush=True)
    print(f"[done] release ready in {output}", flush=True)
    return manifest


if __name__ == "__main__":
    # Old entry point: forward to `wehpah build` / `wehpah check`.
    sys.path.insert(0, str(INGESTION_DIR))
    import wehpah
    argv = sys.argv[1:]
    command = "check" if "--verify-only" in argv else "build"
    wehpah.main(["--dir", str(PROJECT_ROOT), command, *(a for a in argv if a != "--verify-only")])
