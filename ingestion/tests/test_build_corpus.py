import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import build_corpus as build  # noqa: E402
import wehpah  # noqa: E402


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

    def test_merge_short_passages(self):
        base = {"doc_id": "d", "doc_title": "D", "doc_date": "Undated", "content_type": "text",
                "bbox_x_pct": 0.1, "bbox_w_pct": 0.8, "bbox_h_pct": 0.02}
        rows = [
            {**base, "passage_id": "d_p1_i0", "page_number": 1, "passage_text": "3.1. Water use", "bbox_y_pct": 0.10},
            {**base, "passage_id": "d_p1_i1", "page_number": 1, "passage_text": "x" * 300, "bbox_y_pct": 0.13},
            {**base, "passage_id": "d_p1_i2", "page_number": 1, "passage_text": "y" * 300, "bbox_y_pct": 0.20},
            {**base, "passage_id": "d_p2_i3", "page_number": 2, "passage_text": "Next page", "bbox_y_pct": 0.05},
            {**base, "passage_id": "d_p2_i4", "page_number": 2, "content_type": "table", "passage_text": "|a|", "bbox_y_pct": 0.3},
        ]
        merged = build.merge_short_passages(rows)
        self.assertEqual([r["passage_id"] for r in merged], ["d_p1_i0", "d_p1_i2", "d_p2_i3", "d_p2_i4"])
        self.assertTrue(merged[0]["passage_text"].startswith("3.1. Water use\n"))
        self.assertAlmostEqual(merged[0]["bbox_h_pct"], 0.05)
        build.validate_rows(merged)

    def test_fingerprint_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            build.atomic_json(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            path.write_bytes(b"one")
            first = build.sha256_file(path)
            path.write_bytes(b"two")
            self.assertNotEqual(first, build.sha256_file(path))

    def test_valid_cache_is_reused_and_failure_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.pdf").write_bytes(b"pdf")
            meta = ("a.pdf", "A", "Undated", "a")
            key, cache = build.extraction_cache_file(root / "cache", build.sha256_file(root / "a.pdf"), meta,
                                                     build.extraction_config(False))
            row = dict(passage_id="a_p1_i0", bbox_x_pct=0, bbox_y_pct=0, bbox_w_pct=.1, bbox_h_pct=.1)
            build.atomic_json(cache, {"cache_key": key, "doc_id": "a", "rows": [row]})
            docs = [(root / "a.pdf", meta)]
            self.assertEqual(len(build.stage_extract(docs, root / "cache", 1, False)), 1)
            build.atomic_json(cache, {"cache_key": key, "doc_id": "a", "rows": [], "error": "failed"})
            with patch.object(build, "verify_ocr_available"), patch.object(build, "ProcessPoolExecutor") as pool:
                pool.return_value.__enter__.side_effect = RuntimeError("retry started")
                with self.assertRaisesRegex(RuntimeError, "retry started"):
                    build.stage_extract(docs, root / "cache", 1, False)

    def test_vector_cache_reads_legacy_files_once(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            config = {"model": "m"}
            legacy = cache_dir / "embeddings-v2" / build.fingerprint(config)
            build.atomic_json(legacy / "h1.json", [0.5] * 384)
            cache = build.VectorCache(cache_dir, config)
            self.assertEqual(set(cache.get(["h1", "h2"])), {"h1"})
            (legacy / "h1.json").unlink()
            cache.put({"h2": [0.25] * 384})
            found = cache.get(["h1", "h2"])
            self.assertEqual(set(found), {"h1", "h2"})
            self.assertAlmostEqual(found["h2"][0], 0.25)
            cache.close()

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
            build.stage_export(rows, root)
            con = duckdb.connect()
            con.execute(f"CREATE VIEW corpus AS SELECT * FROM read_parquet('{root / 'corpus.parquet'}')")
            build.verify_shards(con, root / "clusters", 3)
            self.assertEqual(sorted(p.name for p in (root / "clusters").iterdir()),
                             ["cluster_00.parquet", "cluster_01.parquet"])
            (root / "clusters" / "cluster_00.parquet").unlink()
            with self.assertRaisesRegex(ValueError, "Shard files"):
                build.verify_shards(con, root / "clusters", 3)
            con.close()

    def test_interrupted_swap_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "dist_data"
            backup = build.backup_path(out)
            self.assertEqual(backup.name, ".dist-data-previous")
            backup.mkdir()
            (backup / "dossier.json").write_text("{}")
            build.recover_interrupted_swap(out)
            self.assertTrue((out / "dossier.json").exists())
            self.assertFalse(backup.exists())
            backup.mkdir()  # both exist and the current release fails its check: stop, touch nothing
            with self.assertRaisesRegex(RuntimeError, "Inspect"):
                build.recover_interrupted_swap(out)
            self.assertTrue(backup.exists())


class SettingsTests(unittest.TestCase):
    def test_defaults_save_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            s = wehpah.load_settings(root)
            self.assertEqual((s.title, s.pdfs, s.output), ("", root.resolve(), (root / "dist_data").resolve()))
            wehpah.save_settings(root, title='Case "A"', description="x")
            wehpah.save_settings(root, publish_to="me/case-a", title="Case B")
            s = wehpah.load_settings(root)
            self.assertEqual((s.title, s.description, s.publish_to), ("Case B", "x", "me/case-a"))
            self.assertIn("# [advanced]", (root / "dossier.toml").read_text())  # comments survive
            with self.assertRaises(ValueError):
                wehpah.save_settings(root, publish_to="not a repo")
            (root / "dossier.toml").write_text('title = "A"\ntitel = "typo"\n')
            with self.assertRaisesRegex(ValueError, "titel"):
                wehpah.load_settings(root)
            (root / "dossier.toml").write_text('title = "A"\n[filenames]\ndate = "no group"\n')
            with self.assertRaisesRegex(ValueError, "group"):
                wehpah.load_settings(root)

    def test_pdf_folder_outside_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for bad in ("dist_data", "dist_data/pdfs"):
                (root / "dossier.toml").write_text(f'title = "A"\npdfs = "{bad}"\n')
                with self.assertRaisesRegex(ValueError, "output folder"):
                    wehpah.load_settings(root)
            (root / "dossier.toml").write_text('title = "A"\npdfs = "~/somewhere"\n')
            self.assertEqual(wehpah.load_settings(root).pdfs, (Path.home() / "somewhere").resolve())

    def test_filename_patterns(self):
        title_re = wehpah._pattern(r"^TN\d+_\d{8}T\d{6}_(.+)$", "title")
        date_re = wehpah._pattern(r"^TN\d+_(\d{8})T", "date")
        name = "TN230528_20191107T132749_Memo to  Open New Docket.pdf"
        self.assertEqual(build.humanize_title(name, title_re), "Memo to Open New Docket")
        self.assertEqual(build.extract_date(name, Path("missing.pdf"), date_re), "2019-11-07")
        self.assertEqual(build.humanize_title("Plain name.pd.pdf", title_re), "Plain name")
        self.assertEqual(build.extract_date("Plain.pdf", Path("missing.pdf"), date_re), "Undated")


if __name__ == "__main__":
    unittest.main()
