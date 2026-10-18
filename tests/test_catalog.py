"""Catalog scan and search."""

import hashlib
import unittest

import tests.bootstrap  # noqa: F401
from no_file_mess.catalog import connect, files_for_volume, scan, search
from no_file_mess.errors import NoFileMessError
from no_file_mess.hashing import HASH_ALGO, hash_file, is_dataless


class DatalessTests(unittest.TestCase):
    def test_flag_is_recognized_without_opening_a_file(self) -> None:
        class Cloud:
            st_flags = 0x40000000

        class Local:
            st_flags = 0

        class NoFlags:
            pass

        self.assertTrue(is_dataless(Cloud()))  # type: ignore[arg-type]
        self.assertFalse(is_dataless(Local()))  # type: ignore[arg-type]
        self.assertFalse(is_dataless(NoFlags()))  # type: ignore[arg-type]


class ScanTests(unittest.TestCase):
    def test_hash_matches_known_bytes(self) -> None:
        root = self._tree({"photo.txt": b"same-bytes"})
        self.assertEqual(hash_file(root / "photo.txt"), hashlib.sha256(b"same-bytes").hexdigest())

    def test_scan_search_and_rescan(self) -> None:
        root = self._tree(
            {
                "March 2026 France/photo.txt": b"same-bytes",
                "notes/café.txt": b"other",
                ".DS_Store": b"noise",
            }
        )
        db = root.parent / "catalog.sqlite"
        with connect(db) as conn:
            stats = scan(conn, "disk-a", root)
            self.assertEqual(stats.files, 2)
            self.assertEqual(stats.skipped_noise, 1)
            found = search(conn, "fran")
            self.assertEqual([row.rel_path for row in found], ["March 2026 France/photo.txt"])
            substring = search(conn, "café", substring=True)
            self.assertEqual(substring[0].rel_path, "notes/café.txt")
            self.assertEqual(substring[0].algo, HASH_ALGO)
            (root / "notes" / "café.txt").unlink()
            scan(conn, "disk-a", root)
            self.assertEqual(
                [row.rel_path for row in files_for_volume(conn, "disk-a")],
                ["March 2026 France/photo.txt"],
            )

    def test_unknown_volume_is_an_error(self) -> None:
        root = self._tree({})
        with connect(root / "catalog.sqlite") as conn:
            with self.assertRaises(NoFileMessError):
                files_for_volume(conn, "missing")

    def _tree(self, files: dict[str, bytes]):
        import tempfile
        from pathlib import Path

        base = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: _remove(base))
        directory = base / "disk"
        directory.mkdir()
        for rel, content in files.items():
            path = directory.joinpath(*rel.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return directory


def _remove(path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
