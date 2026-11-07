import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("build_corpus", Path(__file__).parents[1] / "build_corpus.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class Box:
    l, r, t, b = 10, 50, 80, 20
    coord_origin = "BOTTOMLEFT"


class PipelineTests(unittest.TestCase):
    def test_bottom_left_and_clipping(self):
        self.assertEqual(build.normalize_bbox(Box(), 100, 100), (0.1, 0.2, 0.4, 0.6000000000000001))
        box = Box()
        box.coord_origin, box.l, box.r, box.t, box.b = "TOPLEFT", -10, 120, 10, 80
        self.assertEqual(build.normalize_bbox(box, 100, 100), (0, 0.1, 1, 0.7))
        box.coord_origin = "unknown"
        with self.assertRaises(ValueError):
            build.normalize_bbox(box, 100, 100)

    def test_reject_collision_and_bad_box(self):
        with self.assertRaises(ValueError):
            build.check_slug_collisions([Path("A B.pdf"), Path("A-B.pdf")])
        with self.assertRaises(ValueError):
            build.validate_rows([dict(passage_id="a", bbox_x_pct=.9, bbox_y_pct=0, bbox_w_pct=.2, bbox_h_pct=.1)])
        with self.assertRaises(ValueError):
            build.validate_rows([dict(passage_id="a", bbox_x_pct=0, bbox_y_pct=0, bbox_w_pct=0, bbox_h_pct=.1)])

    def test_fingerprint_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            build.atomic_json(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            path.write_bytes(b"one")
            first = build.sha256_file(path)
            path.write_bytes(b"two")
            self.assertNotEqual(first, build.sha256_file(path))
            self.assertNotEqual(build.fingerprint({"source": first}), build.fingerprint({"source": build.sha256_file(path)}))

    def test_valid_cache_is_reused_and_failure_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.pdf").write_bytes(b"pdf")
            args = [("a.pdf", "A", "Undated", "a")]
            config = {"version": build.CACHE_VERSION, "ocr": True, "table_structure": True,
                      "platform": build.sys.platform, "force_full_page_ocr": False, "tools": build.versions("docling", "docling-core", "pypdfium2", "ocrmac")}
            key = build.fingerprint({"source_sha256": build.sha256_file(root / "a.pdf"), "metadata": args[0], "config": config})
            row = dict(passage_id="a_p1_i0", bbox_x_pct=0, bbox_y_pct=0, bbox_w_pct=.1, bbox_h_pct=.1)
            cache = root / "cache" / "extraction-v2" / f"{key}.json"
            build.atomic_json(cache, {"cache_key": key, "doc_id": "a", "rows": [row]})
            with patch.object(build, "RAW_PDFS_DIR", root), patch.object(build, "CACHE_DIR", root / "cache"):
                self.assertEqual(len(build.stage_extract(args, 1, False)), 1)
                build.atomic_json(cache, {"cache_key": key, "doc_id": "a", "rows": [], "error": "failed"})
                with patch.object(build, "ProcessPoolExecutor") as pool:
                    pool.return_value.__enter__.side_effect = RuntimeError("retry started")
                    with self.assertRaisesRegex(RuntimeError, "retry started"):
                        build.stage_extract(args, 1, False)

    def test_export_and_verify_cluster_shards(self):
        import duckdb
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for i, cid in enumerate([1, 0, 1]):
                rows.append(dict(passage_id=f"doc_p1_i{i}", doc_id="doc", doc_title="Doc",
                                 doc_date="Undated", page_number=1, content_type="text",
                                 passage_text=f"passage {i}", cluster_id=cid, _embedding=[0.0] * 384,
                                 bbox_x_pct=0.1, bbox_y_pct=0.1, bbox_w_pct=0.2, bbox_h_pct=0.2))
            with patch.object(build, "DIST_DATA_DIR", root), patch.object(build, "CORPUS_PARQUET_PATH", root / "corpus.parquet"):
                build.stage_export(rows)
                con = duckdb.connect()
                con.execute(f"CREATE VIEW corpus AS SELECT * FROM read_parquet('{root / 'corpus.parquet'}')")
                build.verify_shards(con, root / "clusters", 3)
                self.assertEqual(sorted(p.name for p in (root / "clusters").iterdir()),
                                 ["cluster_00.parquet", "cluster_01.parquet"])
                (root / "clusters" / "cluster_00.parquet").unlink()
                with self.assertRaisesRegex(ValueError, "Shard files"):
                    build.verify_shards(con, root / "clusters", 3)
                con.close()


if __name__ == "__main__":
    unittest.main()
