"""Applying plans, restoring them, and building a bundle."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import tests.bootstrap  # noqa: F401
from no_file_mess.bundle import build_bundle
from no_file_mess.catalog import connect, scan
from no_file_mess.cli import main
from no_file_mess.duplicates import Labels, plan_duplicates
from no_file_mess.errors import NoFileMessError
from no_file_mess.names import FolderRules, plan_renames
from no_file_mess.paths import from_rel
from no_file_mess.plans import apply_plan, restore


class ApplyTests(unittest.TestCase):
    def test_quarantine_and_restore_round_trip(self) -> None:
        root = _workspace()
        self.addCleanup(lambda: _remove(root.parent))
        _write(root / "album" / "a.txt", b"same")
        _write(root / "DCIM" / "a.txt", b"same")
        db = root.parent / "catalog.sqlite"
        with connect(db) as conn:
            scan(conn, "disk-a", root)
            from no_file_mess.catalog import files_for_volume

            plan = plan_duplicates(files_for_volume(conn, "disk-a"), Labels(), "disk-a")
        self.assertEqual(apply_plan(plan, root), 1)
        self.assertFalse((root / "DCIM" / "a.txt").exists())
        self.assertTrue((root / "album" / "a.txt").is_file())
        quarantined = list((root / ".no-file-mess-quarantine" / "files").rglob("a.txt"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(restore(root), 2)
        self.assertEqual((root / "DCIM" / "a.txt").read_bytes(), b"same")
        self.assertFalse((root / ".no-file-mess-quarantine" / "journal.jsonl").exists())

    def test_merge_moves_the_unique_file_and_restores_it(self) -> None:
        from no_file_mess.catalog import FileRecord
        from no_file_mess.duplicates import folder_merge_plan
        from no_file_mess.hashing import hash_file

        root = _workspace()
        self.addCleanup(lambda: _remove(root.parent))
        _write(root / "keep" / "a.jpg", b"same")
        _write(root / "drop" / "a.jpg", b"same")
        _write(root / "drop" / "only.jpg", b"unique")

        def record(rel: str) -> FileRecord:
            path = root.joinpath(*rel.split("/"))
            return FileRecord("disk", rel, path.stat().st_size, 1, "sha256", hash_file(path))

        files = [record("keep/a.jpg"), record("drop/a.jpg"), record("drop/only.jpg")]
        decisions = [{"path": "keep", "other": "drop", "choice": "1", "action": "merge"}]
        plan = folder_merge_plan(files, decisions, "disk", root)
        apply_plan(plan, root)
        self.assertEqual((root / "keep" / "only.jpg").read_bytes(), b"unique")
        self.assertEqual((root / "keep" / "a.jpg").read_bytes(), b"same")
        self.assertFalse((root / "drop").exists())
        restore(root)
        self.assertEqual((root / "drop" / "a.jpg").read_bytes(), b"same")
        self.assertEqual((root / "drop" / "only.jpg").read_bytes(), b"unique")

    def test_rename_and_restore_round_trip(self) -> None:
        root = _workspace()
        self.addCleanup(lambda: _remove(root.parent))
        _write(root / "March 2026 France" / "photo.txt", b"pic")
        plan = plan_renames(root, FolderRules())
        apply_plan(plan, root)
        self.assertEqual((root / "2026-03-France" / "photo.txt").read_bytes(), b"pic")
        restore(root)
        self.assertTrue((root / "March 2026 France" / "photo.txt").is_file())

    def test_stale_digest_and_escaping_path_are_refused(self) -> None:
        root = _workspace()
        self.addCleanup(lambda: _remove(root.parent))
        _write(root / "a.txt", b"one")
        plan = {
            "version": 1,
            "kind": "duplicates",
            "volume": "disk-a",
            "operations": [
                {
                    "op": "quarantine",
                    "path": "a.txt",
                    "keep": "a.txt",
                    "digest": "0" * 64,
                    "detail": "test",
                }
            ],
        }
        with self.assertRaises(NoFileMessError):
            apply_plan(plan, root)
        self.assertTrue((root / "a.txt").is_file())
        plan["operations"][0]["path"] = "../outside.txt"
        with self.assertRaises(NoFileMessError):
            apply_plan(plan, root)

    def test_from_rel_rejects_parent_segments(self) -> None:
        with self.assertRaises(NoFileMessError):
            from_rel(Path("."), "../outside.txt")


class BundleTests(unittest.TestCase):
    def test_bundle_copies_only_missing_bytes(self) -> None:
        source = _workspace()
        other = _workspace()
        self.addCleanup(lambda: _remove(source.parent))
        self.addCleanup(lambda: _remove(other.parent))
        _write(source / "trips" / "a.txt", b"only-here")
        _write(source / "trips" / "b.txt", b"shared")
        _write(source / "elsewhere" / "b.txt", b"shared")
        _write(other / "kept" / "b.txt", b"shared")
        db = source.parent / "catalog.sqlite"
        with connect(db) as conn:
            scan(conn, "disk-b", source)
            scan(conn, "disk-a", other)
            out = source.parent / "bundle"
            manifest = build_bundle(
                conn,
                have="disk-a",
                source_volume="disk-b",
                source_root=source,
                output=out,
            )
        self.assertEqual(manifest["file_count"], 1)
        self.assertEqual((out / "files" / "trips" / "a.txt").read_bytes(), b"only-here")
        self.assertFalse((out / "files" / "trips" / "b.txt").exists())
        self.assertEqual(len(manifest["same_bytes_different_path"]), 1)
        readme = (out / "README.txt").read_text(encoding="utf-8")
        self.assertIn("disk-b", readme)
        self.assertIn("trips: 1", readme)
        self.assertTrue((out / "MANIFEST.json").is_file())

    def test_bundle_refuses_one_volume_as_both_sides(self) -> None:
        source = _workspace()
        self.addCleanup(lambda: _remove(source.parent))
        _write(source / "a.txt", b"x")
        with connect(source.parent / "catalog.sqlite") as conn:
            scan(conn, "disk-a", source)
            with self.assertRaises(NoFileMessError):
                build_bundle(
                    conn,
                    have="disk-a",
                    source_volume="disk-a",
                    source_root=source,
                    output=source.parent / "bundle",
                )


class CliTests(unittest.TestCase):
    def test_scan_search_and_missing_directory(self) -> None:
        root = _workspace()
        self.addCleanup(lambda: _remove(root.parent))
        _write(root / "album" / "photo.txt", b"pic")
        db = root.parent / "catalog.sqlite"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = main(["scan", "--db", str(db), "--volume", "disk-a", str(root)])
        self.assertEqual(status, 0)
        self.assertIn("1 files", stdout.getvalue())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = main(["search", "--db", str(db), "photo"])
        self.assertEqual(status, 0)
        self.assertIn("album/photo.txt", stdout.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(["scan", "--db", str(db), "--volume", "disk-a", str(root / "missing")])
        self.assertEqual(status, 2)
        self.assertIn("not a directory", stderr.getvalue())

    def test_rules_file_is_accepted(self) -> None:
        root = _workspace()
        self.addCleanup(lambda: _remove(root.parent))
        rules = root.parent / "rules.json"
        rules.write_text(json.dumps({"template": "{name}-{year}-{month}", "separator": "-"}), encoding="utf-8")
        (root / "March 2026 France").mkdir()
        out = root.parent / "plan.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = main(
                ["plan-names", "--root", str(root), "--rules", str(rules), "--out", str(out)]
            )
        self.assertEqual(status, 0)
        plan = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(plan["operations"][0]["to"], "France-2026-03")


def _workspace() -> Path:
    base = Path(tempfile.mkdtemp(prefix="nfm-"))
    disk = base / "disk"
    disk.mkdir()
    return disk


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _remove(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
