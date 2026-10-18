"""Keeper suggestions for identical files."""

import unittest

import tests.bootstrap  # noqa: F401
from no_file_mess.catalog import FileRecord
from no_file_mess.duplicates import Labels, plan_duplicates


def _file(path: str, digest: str, mtime: float) -> FileRecord:
    return FileRecord("disk-a", path, 3, mtime, "sha256", digest)


class DuplicatePlanTests(unittest.TestCase):
    def test_pruned_directory_is_preferred(self) -> None:
        files = [
            _file("pick/a.txt", "aaa", 10),
            _file("dump/a.txt", "aaa", 50),
            _file("dump/b.txt", "bbb", 50),
        ]
        plan = plan_duplicates(files, Labels(), "disk-a")
        ops = plan["operations"]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["path"], "dump/a.txt")
        self.assertEqual(ops[0]["keep"], "pick/a.txt")
        self.assertIn("subset", ops[0]["detail"])

    def test_dump_name_loses_to_another_copy(self) -> None:
        files = [
            _file("DCIM/a.txt", "aaa", 10),
            _file("album/a.txt", "aaa", 10),
        ]
        ops = plan_duplicates(files, Labels(), "disk-a")["operations"]
        self.assertEqual(ops[0]["keep"], "album/a.txt")
        self.assertIn("dump", ops[0]["detail"])

    def test_curated_prefix_beats_a_newer_dump(self) -> None:
        files = [
            _file("camera-dump/a.txt", "aaa", 90),
            _file("albums/a.txt", "aaa", 10),
        ]
        labels = Labels(curated=("albums",), dump=("camera-dump",))
        ops = plan_duplicates(files, labels, "disk-a")["operations"]
        self.assertEqual(ops[0]["keep"], "albums/a.txt")
        self.assertIn("curated", ops[0]["detail"])

    def test_newer_mtime_then_shortest_path(self) -> None:
        newer = plan_duplicates(
            [_file("one/a.txt", "aaa", 10), _file("two/b.txt", "aaa", 20)],
            Labels(),
            "disk-a",
        )["operations"]
        self.assertEqual(newer[0]["keep"], "two/b.txt")
        self.assertIn("newer", newer[0]["detail"])

        shorter = plan_duplicates(
            [_file("a/x.txt", "aaa", 10), _file("folder/x.txt", "aaa", 10)],
            Labels(),
            "disk-a",
        )["operations"]
        self.assertEqual(shorter[0]["keep"], "a/x.txt")
        self.assertIn("shortest", shorter[0]["detail"])

    def test_unique_files_produce_no_operations(self) -> None:
        plan = plan_duplicates([_file("only.txt", "aaa", 1)], Labels(), "disk-a")
        self.assertEqual(plan["operations"], [])


if __name__ == "__main__":
    unittest.main()
