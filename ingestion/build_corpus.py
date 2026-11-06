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
    """Docket filenames embed the filing date; fall back to PDF metadata, then mtime."""
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
    try:
        import datetime

        ts = pdf_path.stat().st_mtime
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return "Undated"


# ---------------------------------------------------------------------------
# Stage A: layout-aware extraction with coordinate normalization
# ---------------------------------------------------------------------------

_CONVERTER = None
_CONVERTER_FALLBACK = None


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

        pipeline_options.ocr_options = OcrMacOptions()
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
    if origin is not None and str(origin).upper().endswith("BOTTOMLEFT"):
        x_pct = l / page_width
        y_pct = (page_height - t) / page_height
        w_pct = (r - l) / page_width
        h_pct = (t - b) / page_height
    else:  # TOPLEFT or unknown — treat as top-left
        x_pct = l / page_width
        y_pct = t / page_height
        w_pct = (r - l) / page_width
        h_pct = (b - t) / page_height
    clamp = lambda v: max(0.0, min(1.0, v))
    return clamp(x_pct), clamp(y_pct), clamp(w_pct), clamp(h_pct)


def extract_single_pdf(args: tuple[str, str, str, str]) -> dict:
    """Worker: convert one PDF into cached row dicts (no embeddings yet)."""
    filename, doc_title, doc_date, _ = args
    pdf_path = RAW_PDFS_DIR / filename
    doc_id = slugify(filename)
    t0 = time.time()

    from docling_core.types.doc import PictureItem, TableItem

    try:
        conv_res = _get_converter().convert(str(pdf_path))
    except Exception:
        # Resilience: some legal scans choke the default parser — retry with pypdfium2 backend.
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
            continue
        page_w, page_h = page_sizes[page_no]

        if isinstance(item, TableItem):
            try:
                text = item.export_to_markdown(doc)
            except Exception:
                text = ""
            content_type = "table"
        elif isinstance(item, PictureItem):
            caption = ""
            try:
                caption = (item.caption_text(doc) or "").strip()
            except Exception:
                pass
            if not caption:
                try:
                    for c in getattr(item, "captions", None) or []:
                        caption = c.resolve(doc).text or ""
                        if caption.strip():
                            caption = caption.strip()
                            break
                except Exception:
                    pass
            text = f"[Visual Evidence on Page {page_no}]: {caption}" if caption else (
                f"[Visual Evidence on Page {page_no}]: (figure, stamp, signature or diagram without extractable text)"
            )
            content_type = "visual_evidence"
        else:
            text = (getattr(item, "text", "") or "").strip()
            content_type = "text"
        if not text:
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
    """Extract every PDF, using per-document JSON checkpoints in .cache/."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    pending: list[tuple[str, str, str, str]] = []
    for fa in file_args:
        cache_file = CACHE_DIR / f"{slugify(fa[0])}.json"
        if cache_file.exists() and not force:
            try:
                results.append(json.loads(cache_file.read_text()))
                continue
            except Exception:
                pass
        pending.append(fa)

    print(f"[extract] {len(file_args)} docs total | cached {len(results)} | to process {len(pending)}", flush=True)
    if pending:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(extract_single_pdf, fa): fa for fa in pending}
            done = 0
            for fut in as_completed(futures):
                fa = futures[fut]
                try:
                    payload = fut.result()
                except Exception as exc:
                    print(f"[extract] FAILED {fa[0]}: {exc}", flush=True)
                    payload = {
                        "doc_id": slugify(fa[0]), "source_filename": fa[0],
                        "doc_title": fa[1], "doc_date": fa[2], "n_pages": 0,
                        "rows": [], "seconds": 0.0, "error": str(exc),
                    }
                (CACHE_DIR / f"{payload['doc_id']}.json").write_text(json.dumps(payload))
                results.append(payload)
                done += 1
                print(f"[extract] ({done}/{len(pending)}) {payload['doc_id']}: "
                      f"{len(payload['rows'])} rows in {payload['seconds']}s", flush=True)
    results.sort(key=lambda r: r["doc_id"])
    return results


# ---------------------------------------------------------------------------
# Stage B/C: embeddings + K-means
# ---------------------------------------------------------------------------

def stage_embed_and_cluster(all_rows: list[dict]) -> None:
    """Attach cluster_id to every row; write centroids.json."""
    import numpy as np

    texts = [r["passage_text"] for r in all_rows]
    print(f"[embed] {len(texts)} passages with {EMBED_MODEL_ID} (dim {EMBED_DIM})", flush=True)
    t0 = time.time()
    from sentence_transformers import SentenceTransformer

    device = "mps" if sys.platform == "darwin" else "cpu"
    try:
        model = SentenceTransformer(EMBED_MODEL_ID, device=device)
    except Exception:
        model = SentenceTransformer(EMBED_MODEL_ID, device="cpu")
    embeddings = model.encode(
        texts, normalize_embeddings=True, batch_size=64, show_progress_bar=False,
        convert_to_numpy=True,
    ).astype(np.float32)
    print(f"[embed] done in {time.time() - t0:.0f}s", flush=True)

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
    CENTROIDS_PATH.write_text(json.dumps(centroids))

    for row, cid, vec in zip(all_rows, labels, embeddings):
        row["cluster_id"] = int(cid)
        row["_embedding"] = [float(v) for v in vec]


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
        COPY (SELECT * FROM corpus ORDER BY cluster_id ASC, doc_id ASC, page_number ASC)
        TO '{CORPUS_PARQUET_PATH.as_posix()}'
        (FORMAT PARQUET, ROW_GROUP_SIZE 500, COMPRESSION ZSTD)
        """
    )
    con.close()


def verify_corpus() -> None:
    """Self-verification checklist (AGENTS.md §6 items 1-2)."""
    import duckdb

    con = duckdb.connect()
    con.execute(f"CREATE VIEW corpus AS SELECT * FROM read_parquet('{CORPUS_PARQUET_PATH.as_posix()}')")
    n = con.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
    assert n > 0, "corpus.parquet is empty"
    assert CORPUS_PARQUET_PATH.stat().st_size > 0
    assert CENTROIDS_PATH.stat().st_size > 0

    bad = con.execute(
        """
        SELECT COUNT(*) FROM corpus WHERE
            bbox_x_pct < 0.0 OR bbox_x_pct > 1.0 OR
            bbox_y_pct < 0.0 OR bbox_y_pct > 1.0 OR
            bbox_w_pct < 0.0 OR bbox_w_pct > 1.0 OR
            bbox_h_pct < 0.0 OR bbox_h_pct > 1.0
        """
    ).fetchone()[0]
    assert bad == 0, f"{bad} rows violate 0.0 <= bbox_pct <= 1.0"

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

    assert RAW_PDFS_DIR.is_dir(), f"missing input folder: {RAW_PDFS_DIR}"
    PDFS_OUT_DIR.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(RAW_PDFS_DIR.glob("*.pdf"))
    if args.limit > 0:
        pdf_files = pdf_files[: args.limit]
    assert pdf_files, f"no PDFs found in {RAW_PDFS_DIR}"

    # 3.2.1 — copy each raw PDF into dist_data/pdfs/{doc_id}.pdf (web-streamed copy)
    file_args = []
    for p in pdf_files:
        doc_id = slugify(p.name)
        dest = PDFS_OUT_DIR / f"{doc_id}.pdf"
        if not dest.exists() or dest.stat().st_size != p.stat().st_size:
            shutil.copy2(p, dest)
        file_args.append((p.name, humanize_title(p.name), extract_date(p.name, p), doc_id))
    print(f"[copy] {len(file_args)} PDFs staged in {PDFS_OUT_DIR}", flush=True)

    # Stage A
    extracted = stage_extract(file_args, workers=args.workers, force=args.force)
    ok_docs = [e for e in extracted if not e.get("error")]
    failed_docs = [e for e in extracted if e.get("error")]
    all_rows: list[dict] = []
    for e in ok_docs:
        all_rows.extend(e["rows"])
    # de-dupe on PK (defensive)
    seen: set[str] = set()
    deduped = []
    for r in all_rows:
        if r["passage_id"] not in seen:
            seen.add(r["passage_id"])
            deduped.append(r)
    all_rows = deduped
    print(f"[extract] corpus rows: {len(all_rows)} from {len(ok_docs)} docs "
          f"({len(failed_docs)} failed: {[e['source_filename'] for e in failed_docs]})", flush=True)
    assert all_rows, "no rows extracted — check docling output"

    # Stages B + C
    stage_embed_and_cluster(all_rows)

    # Stage D
    stage_export(all_rows)
    (DIST_DATA_DIR / "dossier.json").write_text(json.dumps({
        "schema_version": 1,
        "title": args.title,
        "description": args.description,
        "document_count": len(ok_docs),
        "passage_count": len(all_rows),
        "model_id": CENTROIDS_MODEL_ID,
        "dimensions": EMBED_DIM,
        "config": "default",
        "split": "train",
        "corpus_path": "corpus.parquet",
        "centroids_path": "centroids.json",
        "pdfs_path": "pdfs",
    }, indent=2))

    # §6 verification
    verify_corpus()
    print("[done] dist_data/ is ready (corpus.parquet, centroids.json, pdfs/)", flush=True)


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    main()
