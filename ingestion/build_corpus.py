#!/usr/bin/env python3
"""build_corpus.py — End-to-end OCR, captioning, clustering & DuckDB Parquet export.

Implements the AGENTS.md contract:
  raw_pdfs/*.pdf  ->  dist_data/corpus.parquet + dist_data/centroids.json + dist_data/pdfs/*.pdf

Stages (checkpointed in .cache/ so re-runs skip finished documents):
  A. Layout-aware extraction (docling) with top-left percentage bbox normalization
  B. Embedding (BAAI/bge-small-en-v1.5, 384-d, normalized)
  C. MiniBatchKMeans clustering -> centroids.json
  D. DuckDB insert + sorted Parquet export (ROW_GROUP_SIZE 500, ZSTD)

Usage:
  uv run python build_corpus.py [--limit N] [--workers N] [--force] [--verify-only]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import math
import tempfile
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (project root = parent of ingestion/)
# ---------------------------------------------------------------------------
INGESTION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = INGESTION_DIR.parent
RAW_PDFS_DIR = PROJECT_ROOT / "raw_pdfs"
DIST_DATA_DIR = PROJECT_ROOT / "dist_data"
PDFS_OUT_DIR = DIST_DATA_DIR / "pdfs"
CACHE_DIR = INGESTION_DIR / ".cache"
CENTROIDS_PATH = DIST_DATA_DIR / "centroids.json"
CORPUS_PARQUET_PATH = DIST_DATA_DIR / "corpus.parquet"

EMBED_MODEL_ID = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
CENTROIDS_MODEL_ID = "Xenova/bge-small-en-v1.5"  # browser-side counterpart
RANDOM_STATE = 42
MAX_CLUSTERS = 64
CLUSTER_DIVISOR = 50

TN_DATE_RE = re.compile(r"^TN\d+_(\d{8})T", re.IGNORECASE)

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


CACHE_VERSION = 3

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


def _check_merge() -> None:
    base = {"doc_id": "d", "doc_title": "D", "doc_date": "Undated", "content_type": "text",
            "bbox_x_pct": 0.1, "bbox_w_pct": 0.8, "bbox_h_pct": 0.02}
    rows = [
        {**base, "passage_id": "d_p1_i0", "page_number": 1, "passage_text": "3.1. Water use", "bbox_y_pct": 0.10},
        {**base, "passage_id": "d_p1_i1", "page_number": 1, "passage_text": "x" * 300, "bbox_y_pct": 0.13},
        {**base, "passage_id": "d_p1_i2", "page_number": 1, "passage_text": "y" * 300, "bbox_y_pct": 0.20},
        {**base, "passage_id": "d_p2_i3", "page_number": 2, "passage_text": "Next page", "bbox_y_pct": 0.05},
        {**base, "passage_id": "d_p2_i4", "page_number": 2, "content_type": "table", "passage_text": "|a|", "bbox_y_pct": 0.3},
    ]
    merged = merge_short_passages(rows)
    assert [r["passage_id"] for r in merged] == ["d_p1_i0", "d_p1_i2", "d_p2_i3", "d_p2_i4"], merged
    assert merged[0]["passage_text"].startswith("3.1. Water use\n") and abs(merged[0]["bbox_h_pct"] - 0.05) < 1e-9
    validate_rows(merged)


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


def slugify(filename: str) -> str:
    """Clean filename -> stable doc slug (lowercase alphanumerics + dashes)."""
    stem = Path(filename).stem
    # One docket file ends in ".pd.pdf" — strip that artifact.
    if stem.lower().endswith(".pd"):
        stem = stem[: -len(".pd")]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return slug or "document"


def humanize_title(filename: str) -> str:
    """'TN230528_20191107T132749_Memo to Open New Docket.pdf' -> 'Memo to Open New Docket'."""
    stem = Path(filename).stem
    if stem.lower().endswith(".pd"):
        stem = stem[: -len(".pd")]
    parts = stem.split("_", 2)
    if len(parts) == 3 and re.fullmatch(r"TN\d+", parts[0], re.IGNORECASE) and re.fullmatch(r"\d{8}T\d{6}", parts[1]):
        title = parts[2]
    else:
        title = stem
    title = re.sub(r"\s+", " ", title).strip()
    return title or stem


def extract_date(filename: str, pdf_path: Path) -> str:
    """Use a document date only when the name or PDF metadata supplies one."""
    m = TN_DATE_RE.match(Path(filename).stem)
    if m:
        d = m.group(1)
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    # PDF metadata creation date
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


# ---------------------------------------------------------------------------
# Stage A: layout-aware extraction with coordinate normalization
# ---------------------------------------------------------------------------

_CONVERTER = None
_CONVERTER_FALLBACK = None
_OCR_CHECKED = False


def _make_converter(pdf_backend: str | None = None):
    """Build a docling DocumentConverter with OCR + table structure enabled."""
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.do_table_structure = True

    # Prefer Apple Vision OCR on macOS (fast, high quality, no big model downloads);
    # fall back to EasyOCR (docling default) elsewhere or if the extra is missing.
    ocr_engine_used = "easyocr(default)"
    try:
        from docling.datamodel.pipeline_options import OcrMacOptions

        pipeline_options.ocr_options = OcrMacOptions(force_full_page_ocr=False)
        ocr_engine_used = "ocrmac(AppleVision)"
    except Exception:
        pass

    fmt_kwargs = {"pipeline_options": pipeline_options}
    if pdf_backend is not None:
        try:
            from docling.datamodel.base_models import PdfBackend

            fmt_kwargs["backend"] = getattr(PdfBackend, pdf_backend)
        except Exception:
            pass

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(**fmt_kwargs)}
    )
    return converter, ocr_engine_used


def _get_converter(fallback: bool = False):
    global _CONVERTER, _CONVERTER_FALLBACK
    if fallback:
        if _CONVERTER_FALLBACK is None:
            _CONVERTER_FALLBACK = _make_converter("PYPDFIUM2")[0]
        return _CONVERTER_FALLBACK
    if _CONVERTER is None:
        _CONVERTER, _ = _make_converter()
    return _CONVERTER


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


def extract_single_pdf(args: tuple[str, str, str, str]) -> dict:
    """Worker: convert one PDF into cached row dicts (no embeddings yet)."""
    global _OCR_CHECKED
    if not _OCR_CHECKED:
        verify_ocr_available()
        _OCR_CHECKED = True
    filename, doc_title, doc_date, _ = args
    pdf_path = RAW_PDFS_DIR / filename
    doc_id = slugify(filename)
    t0 = time.time()

    from docling_core.types.doc import PictureItem, TableItem

    try:
        conv_res = _get_converter().convert(str(pdf_path))
    except Exception as exc:
        # Resilience: retry problematic scans with the alternative backend.
        print(f"[extract] {filename}: primary converter failed ({exc}); retrying fallback", flush=True)
        conv_res = _get_converter(fallback=True).convert(str(pdf_path))

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


def stage_extract(file_args: list[tuple[str, str, str, str]], workers: int, force: bool) -> list[dict]:
    """Cache successful extraction by source bytes and extraction configuration."""
    cache_dir = CACHE_DIR / "extraction-v2"
    cache_dir.mkdir(parents=True, exist_ok=True)
    extraction_config = {"version": CACHE_VERSION, "ocr": True, "table_structure": True,
                         "platform": sys.platform, "force_full_page_ocr": False, "tools": versions("docling", "docling-core", "pypdfium2", "ocrmac")}
    results, pending = [], []
    for fa in file_args:
        key = fingerprint({"source_sha256": sha256_file(RAW_PDFS_DIR / fa[0]), "metadata": fa, "config": extraction_config})
        cache_file = cache_dir / f"{key}.json"
        if cache_file.exists() and not force:
            try:
                payload = json.loads(cache_file.read_text())
                if payload.get("cache_key") == key and not payload.get("error") and payload.get("rows"):
                    validate_rows(payload["rows"])
                    results.append(payload)
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                print(f"[extract] invalid checkpoint {cache_file.name}; retrying", flush=True)
        pending.append((fa, key, cache_file))
    print(f"[extract] {len(file_args)} docs | cached {len(results)} | to process {len(pending)}", flush=True)
    failures = []
    if pending:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(extract_single_pdf, fa): (fa, key, path) for fa, key, path in pending}
            for fut in as_completed(futures):
                fa, key, cache_file = futures[fut]
                try:
                    payload = fut.result()
                    if not payload.get("rows"):
                        raise ValueError("no passages extracted")
                    validate_rows(payload["rows"])
                    payload["cache_key"] = key
                    atomic_json(cache_file, payload)
                    results.append(payload)
                    print(f"[extract] {fa[0]}: {len(payload['rows'])} rows", flush=True)
                except Exception as exc:
                    failures.append(f"{fa[0]}: {exc}")
                    print(f"[extract] FAILED {fa[0]}: {exc}", flush=True)
    if failures:
        raise RuntimeError("Extraction failed; existing release left intact. Retry after fixing: " + "; ".join(failures))
    return sorted(results, key=lambda r: r["doc_id"])


# ---------------------------------------------------------------------------
# Stage B/C: embeddings + K-means
# ---------------------------------------------------------------------------

def stage_embed_and_cluster(all_rows: list[dict]) -> str:
    """Attach cluster_id to every row; write centroids.json."""
    import numpy as np

    texts = [r["passage_text"] for r in all_rows]
    from huggingface_hub import snapshot_download
    model_snapshot = Path(snapshot_download(repo_id=EMBED_MODEL_ID))
    model_revision = model_snapshot.name
    embed_config = {"version": CACHE_VERSION, "model": EMBED_MODEL_ID, "revision": model_revision, "dimensions": EMBED_DIM,
                    "normalize": True, "tools": versions("sentence-transformers", "transformers", "torch")}
    embed_dir = CACHE_DIR / "embeddings-v2" / fingerprint(embed_config)
    embed_dir.mkdir(parents=True, exist_ok=True)
    vectors = {}
    missing = {}
    for text in texts:
        key = hashlib.sha256(text.encode()).hexdigest()
        path = embed_dir / f"{key}.json"
        try:
            vector = json.loads(path.read_text())
            if len(vector) != EMBED_DIM or not all(math.isfinite(float(v)) for v in vector):
                raise ValueError("invalid vector")
            vectors[key] = vector
        except (OSError, ValueError, TypeError):
            missing[key] = text
    if missing:
        print(f"[embed] computing {len(missing)} new passages; {len(texts) - len(missing)} reused", flush=True)
        from sentence_transformers import SentenceTransformer
        device = "mps" if sys.platform == "darwin" else "cpu"
        try:
            model = SentenceTransformer(str(model_snapshot), device=device)
        except Exception:
            model = SentenceTransformer(str(model_snapshot), device="cpu")
        keys = list(missing)
        computed = model.encode([missing[k] for k in keys], normalize_embeddings=True, batch_size=64,
                                show_progress_bar=False, convert_to_numpy=True).astype(np.float32)
        if computed.shape != (len(keys), EMBED_DIM) or not np.isfinite(computed).all():
            raise ValueError("Embedding model returned invalid vectors")
        for key, vec in zip(keys, computed):
            vectors[key] = [float(v) for v in vec]
            atomic_json(embed_dir / f"{key}.json", vectors[key])
    embeddings = np.asarray([vectors[hashlib.sha256(t.encode()).hexdigest()] for t in texts], dtype=np.float32)

    from sklearn.cluster import MiniBatchKMeans

    n_clusters = min(MAX_CLUSTERS, max(1, len(all_rows) // CLUSTER_DIVISOR))
    print(f"[cluster] MiniBatchKMeans n_clusters={n_clusters} random_state={RANDOM_STATE}", flush=True)
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
    atomic_json(CENTROIDS_PATH, centroids)

    for row, cid, vec in zip(all_rows, labels, embeddings):
        row["cluster_id"] = int(cid)
        row["_embedding"] = [float(v) for v in vec]
    return model_revision


# ---------------------------------------------------------------------------
# Stage D: DuckDB insert + sorted Parquet export
# ---------------------------------------------------------------------------

def stage_export(all_rows: list[dict]) -> None:
    import duckdb

    DIST_DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    B = 500
    for r in all_rows:
        batch.append((
            r["passage_id"], r["doc_id"], r["doc_title"], r["doc_date"],
            int(r["page_number"]), r["content_type"], r["passage_text"],
            int(r["cluster_id"]), r["_embedding"],
            float(r["bbox_x_pct"]), float(r["bbox_y_pct"]),
            float(r["bbox_w_pct"]), float(r["bbox_h_pct"]),
        ))
        if len(batch) >= B:
            con.executemany(insert_sql, batch)
            batch = []
    if batch:
        con.executemany(insert_sql, batch)

    n = con.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
    assert n == len(all_rows), f"row count mismatch: {n} != {len(all_rows)}"

    print(f"[export] COPY {n} rows -> {CORPUS_PARQUET_PATH} (ROW_GROUP_SIZE 500, ZSTD)", flush=True)
    con.execute(
        f"""
        COPY (SELECT * FROM corpus ORDER BY cluster_id ASC, doc_id ASC, page_number ASC, passage_id ASC)
        TO '{CORPUS_PARQUET_PATH.as_posix()}'
        (FORMAT PARQUET, ROW_GROUP_SIZE 500, COMPRESSION ZSTD)
        """
    )
    shard_dir = DIST_DATA_DIR / "clusters"
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


def verify_corpus() -> None:
    """Self-verification checklist (AGENTS.md §6 items 1-2)."""
    import duckdb

    con = duckdb.connect()
    con.execute(f"CREATE VIEW corpus AS SELECT * FROM read_parquet('{CORPUS_PARQUET_PATH.as_posix()}')")
    n = con.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
    manifest_path = DIST_DATA_DIR / "dossier.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("dimensions") != EMBED_DIM or manifest.get("model_id") != CENTROIDS_MODEL_ID:
        raise ValueError("Unsupported or incompatible dossier manifest")
    if manifest.get("passage_count") != n:
        raise ValueError("Manifest passage count does not match corpus")
    centroids = json.loads(CENTROIDS_PATH.read_text())
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
        verify_shards(con, DIST_DATA_DIR / "clusters", n)
    assert n > 0, "corpus.parquet is empty"
    assert CORPUS_PARQUET_PATH.stat().st_size > 0
    assert CENTROIDS_PATH.stat().st_size > 0

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

    emb_ok = con.execute(
        "SELECT COUNT(*) FROM corpus WHERE array_length(embedding) <> 384"
    ).fetchone()[0]
    assert emb_ok == 0, f"{emb_ok} rows have wrong embedding dimension"

    try:
        norms = con.execute(
            """
            SELECT min(n), max(n) FROM (
                SELECT sqrt(list_sum(list_transform(embedding, lambda x: x * x))) AS n FROM corpus
            )
            """
        ).fetchone()
    except Exception:
        # Older DuckDB builds use the arrow-lambda syntax
        norms = con.execute(
            """
            SELECT min(n), max(n) FROM (
                SELECT sqrt(list_sum(list_transform(embedding, x -> x * x))) AS n FROM corpus
            )
            """
        ).fetchone()
    types = con.execute(
        "SELECT content_type, COUNT(*) FROM corpus GROUP BY content_type ORDER BY 2 DESC"
    ).fetchall()
    print(
        f"[verify] OK — {n} rows | bbox violations: 0 | embeddings 384-d | "
        f"norms in [{norms[0]:.4f}, {norms[1]:.4f}]\n[verify] content types: {types}",
        flush=True,
    )


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Build dist_data corpus for the Dossier Explorer")
    ap.add_argument("--limit", type=int, default=0, help="only process first N PDFs (alphabetical)")
    ap.add_argument("--workers", type=int, default=max(2, min(6, (os.cpu_count() or 4) - 2)))
    ap.add_argument("--force", action="store_true", help="ignore extraction cache")
    ap.add_argument("--verify-only", action="store_true", help="only run the §6 verification checks")
    ap.add_argument("--title", default="Untitled dossier", help="display name for dossier.json")
    ap.add_argument("--description", default="", help="short docket description")
    args = ap.parse_args()

    if args.verify_only:
        verify_corpus()
        return

    if not RAW_PDFS_DIR.is_dir():
        raise FileNotFoundError(f"missing input folder: {RAW_PDFS_DIR}")
    pdf_files = sorted((p for p in RAW_PDFS_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"), key=lambda p: p.name)
    if args.limit > 0:
        pdf_files = pdf_files[:args.limit]
    if not pdf_files:
        raise ValueError(f"no PDFs found in {RAW_PDFS_DIR}")
    check_slug_collisions(pdf_files)
    file_args = [(p.name, humanize_title(p.name), extract_date(p.name, p), slugify(p.name)) for p in pdf_files]
    verify_ocr_available()
    source_hashes = {p.name: sha256_file(p) for p in pdf_files}
    extracted = stage_extract(file_args, workers=args.workers, force=args.force)
    if any(sha256_file(p) != source_hashes[p.name] for p in pdf_files):
        raise RuntimeError("A source PDF changed during extraction; retry the build")
    all_rows = [row for doc in extracted for row in doc["rows"]]
    if not all_rows:
        raise ValueError("no rows extracted")
    validate_rows(all_rows)
    _check_merge()
    before = len(all_rows)
    all_rows = merge_short_passages(all_rows)
    validate_rows(all_rows)
    print(f"[merge] {before} items -> {len(all_rows)} passages (short items joined to the text that follows)", flush=True)

    # Build and verify a complete release alongside the existing one before publishing it.
    global DIST_DATA_DIR, PDFS_OUT_DIR, CENTROIDS_PATH, CORPUS_PARQUET_PATH
    release_dir = DIST_DATA_DIR
    staging = Path(tempfile.mkdtemp(prefix=".dist-data-staging-", dir=PROJECT_ROOT))
    DIST_DATA_DIR = staging
    PDFS_OUT_DIR = staging / "pdfs"
    CENTROIDS_PATH = staging / "centroids.json"
    CORPUS_PARQUET_PATH = staging / "corpus.parquet"
    backup = PROJECT_ROOT / ".dist-data-previous"
    try:
        PDFS_OUT_DIR.mkdir()
        sources = []
        for p in pdf_files:
            digest = source_hashes[p.name]
            shutil.copy2(p, PDFS_OUT_DIR / f"{slugify(p.name)}.pdf")
            if sha256_file(PDFS_OUT_DIR / f"{slugify(p.name)}.pdf") != digest:
                raise IOError(f"PDF changed during staging: {p.name}")
            sources.append({"filename": p.name, "doc_id": slugify(p.name), "sha256": digest})
        model_revision = stage_embed_and_cluster(all_rows)
        stage_export(all_rows)
        manifest = {
            "schema_version": 1, "title": args.title, "description": args.description,
            "document_count": len(extracted), "passage_count": len(all_rows),
            "model_id": CENTROIDS_MODEL_ID, "dimensions": EMBED_DIM, "config": "default", "split": "train",
            "corpus_path": "corpus.parquet", "centroids_path": "centroids.json", "pdfs_path": "pdfs",
            "clusters_path": "clusters",
            "build": {"cache_version": CACHE_VERSION, "source_files": sources,
                      "extraction_tools": versions("docling", "docling-core", "pypdfium2", "ocrmac"),
                      "ocr_backend": "ocrmac/Apple Vision" if sys.platform == "darwin" else "docling default",
                      "force_full_page_ocr": False,
                      "embedding_tools": versions("sentence-transformers", "transformers", "torch"),
                      "embedding_model": EMBED_MODEL_ID, "embedding_revision": model_revision, "cluster_tools": versions("scikit-learn", "duckdb"),
                      "cluster_random_state": RANDOM_STATE, "min_passage_chars": MIN_PASSAGE_CHARS, "max_passage_chars": MAX_PASSAGE_CHARS, "cluster_divisor": CLUSTER_DIVISOR},
        }
        atomic_json(staging / "dossier.json", manifest)
        verify_corpus()
        if backup.exists():
            raise RuntimeError(f"Previous release backup exists at {backup}; inspect and remove it before publishing")
        if release_dir.exists():
            os.replace(release_dir, backup)
        try:
            os.replace(staging, release_dir)
        except BaseException:
            if backup.exists():
                os.replace(backup, release_dir)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        print("[done] validated release published to dist_data/", flush=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        DIST_DATA_DIR = release_dir
        PDFS_OUT_DIR = release_dir / "pdfs"
        CENTROIDS_PATH = release_dir / "centroids.json"
        CORPUS_PARQUET_PATH = release_dir / "corpus.parquet"


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    main()
