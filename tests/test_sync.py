"""Copy plans and folders that share identical photos."""

import os
import tempfile
import unittest
from pathlib import Path

import tests.bootstrap  # noqa: F401
from no_file_mess.catalog import FileRecord, collect, connect, files_for_volume, scan
from no_file_mess.duplicates import plan_folder_duplicates
from no_file_mess.errors import NoFileMessError
from no_file_mess.plans import apply_plan
from no_file_mess.sync import (
    apply_copy,
    apply_diff,
    copy_lines,
    files_from_progress,
    load_progress,
    plan_copy,
    save_progress,
)


class CopyPlanTests(unittest.TestCase):
    def test_plan_copies_only_bytes_the_destination_lacks(self) -> None:
        source = [
            FileRecord("src", "trips/a.txt", 9, 1, "sha256", "aaa"),
            FileRecord("src", "trips/b.txt", 6, 1, "sha256", "bbb"),
        ]
        dest = [FileRecord("dst", "kept/b.txt", 6, 1, "sha256", "bbb")]
        plan = plan_copy(source, dest, source_name="src", dest_name="dst")
        self.assertEqual(_paths(plan, "add"), ["trips/a.txt", "trips/b.txt"])
        self.assertEqual(_paths(plan, "extra"), ["kept/b.txt"])
        notes = [op for op in plan["operations"] if op["op"] == "note"]
        self.assertIn("kept/b.txt", notes[0]["detail"])
        self.assertEqual(plan["actions"], {"kept": "sync", "trips": "sync"})

    def test_catalogs_and_folders_make_the_same_plan(self) -> None:
        source = _workspace()
        dest = _workspace()
        self.addCleanup(lambda: _remove(source.parent))
        self.addCleanup(lambda: _remove(dest.parent))
        _write(source / "trips" / "a.txt", b"only-here")
        _write(source / "trips" / "b.txt", b"shared")
        _write(dest / "kept" / "b.txt", b"shared")
        from_dirs = plan_copy(*_named(source, dest), source_name="src", dest_name="dst")
        db = source.parent / "catalog.sqlite"
        with connect(db) as conn:
            scan(conn, "src", source)
            scan(conn, "dst", dest)
            from_catalog = plan_copy(
                files_for_volume(conn, "src"),
                files_for_volume(conn, "dst"),
                source_name="src",
                dest_name="dst",
            )
            from_source_dir = plan_copy(
                collect(source)[0],
                files_for_volume(conn, "dst"),
                source_name="src",
                dest_name="dst",
            )
        self.assertEqual(_paths(from_dirs, "add"), ["trips/a.txt", "trips/b.txt"])
        self.assertEqual(_paths(from_catalog, "add"), _paths(from_dirs, "add"))
        self.assertEqual(_paths(from_source_dir, "extra"), _paths(from_dirs, "extra"))

    def test_copy_into_destination_or_a_diff_folder(self) -> None:
        source = _workspace()
        dest = _workspace()
        self.addCleanup(lambda: _remove(source.parent))
        self.addCleanup(lambda: _remove(dest.parent))
        _write(source / "trips" / "a.txt", b"only-here")
        _write(source / "trips" / "kept.txt", b"same")
        _write(dest / "trips" / "kept.txt", b"same")
        _write(dest / "trips" / "clash.txt", b"old!!")
        _write(source / "trips" / "clash.txt", b"newer")
        _write(dest / "trips" / "old.txt", b"extra")
        plan = plan_copy(*_named(source, dest), source_name="src", dest_name="dst", action="merge")
        self.assertEqual(_paths(plan, "update"), ["trips/clash.txt"])
        self.assertEqual(_paths(plan, "extra"), ["trips/old.txt"])
        seen: list[tuple[int, int, str]] = []
        into = apply_copy(
            plan,
            source,
            dest,
            diff=False,
            progress=lambda copied, total, folder: seen.append((copied, total, folder)),
        )
        self.assertEqual(seen, [(len(b"only-here"), len(b"only-here"), "trips")])
        self.assertEqual((dest / "trips" / "a.txt").read_bytes(), b"only-here")
        self.assertEqual((dest / "trips" / "clash.txt").read_bytes(), b"old!!")
        self.assertTrue((dest / "trips" / "old.txt").is_file())
        self.assertEqual(into["copied"], 1)
        self.assertEqual(into["overwritten"], 0)
        self.assertEqual(into["deleted"], 0)

        plan["actions"] = {"trips": "identical"}
        diff = source.parent / "diff"
        carried = apply_copy(plan, source, diff, diff=True)
        self.assertEqual((diff / "files" / "trips" / "a.txt").read_bytes(), b"only-here")
        self.assertEqual((diff / "files" / "trips" / "clash.txt").read_bytes(), b"newer")
        self.assertEqual(carried["deleted_paths"], ["trips/old.txt"])
        applied = apply_diff(diff, dest)
        self.assertEqual((dest / "trips" / "clash.txt").read_bytes(), b"newer")
        self.assertFalse((dest / "trips" / "old.txt").exists())
        self.assertEqual(applied["deleted"], 1)
        with self.assertRaises(NoFileMessError):
            apply_plan(plan, dest)

    def test_folder_action_can_update_or_only_remove(self) -> None:
        source = _workspace()
        dest = _workspace()
        self.addCleanup(lambda: _remove(source.parent))
        self.addCleanup(lambda: _remove(dest.parent))
        _write(source / "trips" / "a.txt", b"new")
        _write(source / "trips" / "clash.txt", b"newer")
        _write(dest / "trips" / "clash.txt", b"old!!")
        _write(dest / "trips" / "old.txt", b"extra")
        plan = plan_copy(*_named(source, dest), source_name="src", dest_name="dst", action="update")
        apply_copy(plan, source, dest, diff=False)
        self.assertEqual((dest / "trips" / "a.txt").read_bytes(), b"new")
        self.assertEqual((dest / "trips" / "clash.txt").read_bytes(), b"newer")
        self.assertTrue((dest / "trips" / "old.txt").is_file())

        again = _workspace()
        other = _workspace()
        self.addCleanup(lambda: _remove(again.parent))
        self.addCleanup(lambda: _remove(other.parent))
        _write(again / "trips" / "a.txt", b"new")
        _write(other / "trips" / "old.txt", b"extra")
        _write(other / "trips" / "clash.txt", b"keep")
        _write(again / "trips" / "clash.txt", b"keep")
        removal = plan_copy(*_named(again, other), source_name="src", dest_name="dst", action="remove")
        apply_copy(removal, again, other, diff=False)
        self.assertFalse((other / "trips" / "a.txt").exists())
        self.assertFalse((other / "trips" / "old.txt").exists())
        self.assertEqual((other / "trips" / "clash.txt").read_bytes(), b"keep")

    def test_interactive_lists_files_and_enter_updates(self) -> None:
        source = [
            FileRecord("src", "trips/a.txt", 3, 1, "sha256", "aaa"),
            FileRecord("src", "trips/clash.txt", 3, 1, "sha256", "new"),
            FileRecord("src", "zz/a.txt", 1, 1, "sha256", "zzz"),
        ]
        dest = [
            FileRecord("dst", "trips/clash.txt", 3, 1, "sha256", "old"),
            FileRecord("dst", "trips/old.txt", 1, 1, "sha256", "xxx"),
        ]
        prompts: list[str] = []
        shown: list[str] = []
        answers = iter(["", "q"])

        def reader(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        plan = plan_copy(
            source,
            dest,
            source_name="src",
            dest_name="dst",
            action="update",
            interactive=True,
            reader=reader,
            writer=shown.append,
        )
        self.assertEqual(
            prompts,
            [
                "[s/U/m/r/i/q] [d] inside [o] open [!] rest [h] help ",
                "[s/U/m/r/i/q] [d] inside [o] open [!] rest [h] help ",
            ],
        )
        self.assertTrue(any("clash.txt" in line and "not byte-identical" in line for line in shown))
        self.assertTrue(any("missing -> old.txt" in line for line in shown))
        self.assertEqual(plan["actions"], {"trips": "update"})
        self.assertEqual(plan["status"], "quit")
        self.assertEqual(plan["resume_at"], "zz")

    def test_quit_continues_from_the_saved_listing(self) -> None:
        source = [
            FileRecord("src", "trips/a.txt", 3, 1, "sha256", "aaa"),
            FileRecord("src", "zz/a.txt", 1, 1, "sha256", "zzz"),
        ]
        dest = [FileRecord("dst", "trips/old.txt", 1, 1, "sha256", "xxx")]
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: _remove(root))
        progress = root / "copy.progress.json"
        save_progress(
            progress,
            source="src",
            destination="dst",
            source_files=source,
            dest_files=dest,
            actions={},
            resume_at=None,
            operations=[],
        )
        answers = iter(["", "q"])
        first = plan_copy(
            source,
            dest,
            source_name="src",
            dest_name="dst",
            action="update",
            interactive=True,
            reader=lambda _prompt: next(answers),
            writer=lambda _line: None,
            progress_path=progress,
        )
        saved = load_progress(progress, "src", "dst")
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(first["resume_at"], "zz")
        self.assertEqual(saved["actions"], {"trips": "update"})
        self.assertEqual(saved["source_files"][0]["path"], "trips/a.txt")
        self.assertIsNone(load_progress(progress, "other", "dst"))
        prompts: list[str] = []

        def reader(prompt: str) -> str:
            prompts.append(prompt)
            return "s"

        second = plan_copy(
            files_from_progress(saved["source_files"], "source"),
            files_from_progress(saved["dest_files"], "dest"),
            source_name="src",
            dest_name="dst",
            action="update",
            interactive=True,
            reader=reader,
            writer=lambda _line: None,
            progress_path=progress,
            prior_actions=saved["actions"],
            resume_at=saved["resume_at"],
            assignment=saved["assignment"],
        )
        self.assertEqual(prompts, ["[s/U/m/r/i/q] [d] inside [o] open [!] rest [h] help "])
        self.assertEqual(second["actions"], {"trips": "update", "zz": "skip"})
        self.assertEqual(second["status"], "ready")

    def test_a_shared_parent_is_split_into_subfolders(self) -> None:
        source = [
            FileRecord("src", "2023/2023_10/a.jpg", 1, 1, "sha256", "aaa"),
            FileRecord("src", "2023/keep/b.jpg", 1, 1, "sha256", "bbb"),
        ]
        dest = [FileRecord("dst", "2023/keep/b.jpg", 1, 1, "sha256", "bbb")]
        plan = plan_copy(source, dest, source_name="src", dest_name="dst")
        self.assertEqual([op["folder"] for op in plan["operations"]], ["2023/2023_10"])
        self.assertEqual(plan["actions"], {"2023/2023_10": "sync"})

    def test_descend_asks_for_each_subfolder(self) -> None:
        source = [
            FileRecord("src", "year/one/a.txt", 1, 1, "sha256", "aaa"),
            FileRecord("src", "year/two/b.txt", 1, 1, "sha256", "bbb"),
        ]
        dest: list[FileRecord] = []
        answers = iter(["d", "s", "m"])

        def reader(_prompt: str) -> str:
            return next(answers)

        plan = plan_copy(
            source,
            dest,
            source_name="src",
            dest_name="dst",
            interactive=True,
            reader=reader,
            writer=lambda _line: None,
        )
        self.assertEqual(plan["actions"], {"year/one": "skip", "year/two": "merge"})

    def test_review_counts_a_large_folder(self) -> None:
        operations = [
            {"op": "add", "path": f"olympia/{index}.jpg", "folder": "olympia"} for index in range(13)
        ]
        plan = {"actions": {"olympia": "merge"}, "operations": operations}
        self.assertIn("  add 13 files", copy_lines(plan))

    def test_prompt_lists_source_and_destination_apart(self) -> None:
        source = [
            FileRecord("src", "2018/H-bird/DSC_2753_OM.jpg", 1029325, 1_775_997_540, "", ""),
            FileRecord("src", "2018/H-bird/DSC_3133.JPG", 7_500_000, 1_535_415_900, "sha256", "src"),
            FileRecord("src", "2018/H-bird/DSC_3134.JPG", 6_600_000, 1_535_415_000, "sha256", "src"),
            FileRecord("src", "2018/H-bird/DSC_3135.JPG", 6_600_000, 1_535_416_000, "sha256", "src"),
        ]
        dest = [
            FileRecord("dst", "2018/H-bird/DSC_3133.JPG", 7_200_000, 1_535_415_000, "sha256", "dst"),
            FileRecord("dst", "2018/H-bird/DSC_3134.JPG", 6_300_000, 1_535_416_000, "sha256", "dst"),
            FileRecord("dst", "2018/H-bird/DSC_3135.JPG", 6_300_000, 1_535_416_000, "sha256", "dst"),
            FileRecord("dst", "2018/H-bird/DSC_3204_oM.jpg", 917_000, 1_775_997_780, "", ""),
        ]
        shown: list[str] = []
        plan_copy(
            source,
            dest,
            source_name="src",
            dest_name="dst",
            interactive=True,
            reader=lambda _prompt: "s",
            writer=shown.append,
        )
        text = "\n".join(shown)
        source_at = text.index("  only in source: 1")
        dest_at = text.index("  only in destination: 1")
        mismatch_at = text.index("  mismatched: 3")
        self.assertLess(source_at, dest_at)
        self.assertLess(dest_at, mismatch_at)
        only = next(line for line in shown if "DSC_2753_OM.jpg" in line)
        extra = next(line for line in shown if "DSC_3204_oM.jpg" in line)
        older = next(line for line in shown if "DSC_3133.JPG" in line)
        newer = next(line for line in shown if "DSC_3134.JPG" in line)
        tied = next(line for line in shown if "DSC_3135.JPG" in line)
        self.assertIn("1005.2 KiB", only)
        self.assertIn("-> missing", only)
        self.assertNotIn("not byte-identical", only)
        self.assertTrue(extra.strip().startswith("missing ->"))
        self.assertNotIn("not byte-identical", extra)
        self.assertTrue(older.endswith(" older"))
        self.assertIn("7.2 MiB", older)
        self.assertIn("6.9 MiB", older)
        self.assertTrue(newer.endswith(" newer"))
        self.assertTrue(tied.endswith(" not byte-identical"))

    def test_sync_copies_missing_and_older_copies(self) -> None:
        source = _workspace()
        dest = _workspace()
        self.addCleanup(lambda: _remove(source.parent))
        self.addCleanup(lambda: _remove(dest.parent))
        _write(source / "album" / "new.jpg", b"new-file")
        _write(source / "album" / "older.jpg", b"from-source")
        _write(dest / "album" / "older.jpg", b"old")
        _write(source / "album" / "newer.jpg", b"stale-source")
        _write(dest / "album" / "newer.jpg", b"fresh-dest")
        _write(source / "album" / "tied.jpg", b"source-tie")
        _write(dest / "album" / "tied.jpg", b"dest-tie!!")
        _write(dest / "album" / "extra.jpg", b"keep-me")
        os.utime(source / "album" / "older.jpg", (50, 50))
        os.utime(dest / "album" / "older.jpg", (10, 10))
        os.utime(source / "album" / "newer.jpg", (10, 10))
        os.utime(dest / "album" / "newer.jpg", (50, 50))
        os.utime(source / "album" / "tied.jpg", (20, 20))
        os.utime(dest / "album" / "tied.jpg", (20, 20))
        plan = plan_copy(*_named(source, dest), source_name="src", dest_name="dst", action="sync")
        apply_copy(plan, source, dest, diff=False)
        self.assertEqual((dest / "album" / "new.jpg").read_bytes(), b"new-file")
        self.assertEqual((dest / "album" / "older.jpg").read_bytes(), b"from-source")
        self.assertEqual((dest / "album" / "newer.jpg").read_bytes(), b"fresh-dest")
        self.assertEqual((dest / "album" / "tied.jpg").read_bytes(), b"dest-tie!!")
        self.assertEqual((dest / "album" / "extra.jpg").read_bytes(), b"keep-me")

    def test_semi_asks_only_when_the_dates_do_not_decide(self) -> None:
        source = [
            FileRecord("src", "trips/a.txt", 3, 5, "", ""),
            FileRecord("src", "tied/a.txt", 4, 10, "sha256", "aaa"),
            FileRecord("src", "old/a.txt", 4, 30, "sha256", "new"),
        ]
        dest = [
            FileRecord("dst", "tied/a.txt", 5, 10, "sha256", "bbb"),
            FileRecord("dst", "old/a.txt", 2, 10, "sha256", "old"),
            FileRecord("dst", "gone/a.txt", 1, 1, "", ""),
        ]
        prompts: list[str] = []
        shown: list[str] = []

        def reader(prompt: str) -> str:
            prompts.append(prompt)
            return "u"

        plan = plan_copy(
            source,
            dest,
            source_name="src",
            dest_name="dst",
            semi=True,
            reader=reader,
            writer=shown.append,
        )
        self.assertEqual(prompts, ["[u] overwrite  [s] keep  [o] open  [!] rest  [q] stop  [h] help "])
        tied_lines = [line for line in shown if "not byte-identical" in line]
        self.assertEqual(len(tied_lines), 1)
        self.assertIn("a.txt", tied_lines[0])
        self.assertTrue(any(line.startswith("tied") for line in shown))
        self.assertFalse(any(line.startswith("trips") or line.startswith("old") or line.startswith("gone") for line in shown))
        tied = next(op for op in plan["operations"] if op["path"] == "tied/a.txt")
        self.assertEqual(tied["choice"], "copy")
        self.assertEqual(plan["actions"], {"gone": "sync", "old": "sync", "tied": "sync", "trips": "sync"})
        self.assertEqual(plan["status"], "ready")

    def test_appledouble_sidecars_are_left_out_of_a_copy(self) -> None:
        source = _workspace()
        dest = _workspace()
        self.addCleanup(lambda: _remove(source.parent))
        self.addCleanup(lambda: _remove(dest.parent))
        _write(source / "album" / "DSC_7310.JPG", b"photo")
        _write(source / "album" / "._DSC_7310.JPG", b"sidecar")
        _write(dest / "album" / "._only-here.JPG", b"dest-sidecar")
        plan = plan_copy(*_named(source, dest), source_name="src", dest_name="dst")
        paths = [op["path"] for op in plan["operations"]]
        self.assertEqual(paths, ["album/DSC_7310.JPG"])


class FolderDuplicateTests(unittest.TestCase):
    def test_partial_overlap_is_a_folder_pair(self) -> None:
        files = [
            _media("trip/a.jpg", "1"),
            _media("trip/b.jpg", "2"),
            _media("trip/edited.jpg", "3"),
            _media("copy/a.jpg", "1"),
            _media("copy/b.jpg", "2"),
            _media("copy/extra.jpg", "4"),
            _media("trip/notes.txt", "9"),
            _media("copy/notes.txt", "9"),
        ]
        plan = plan_folder_duplicates(files, "disk", min_shared=2, min_ratio=0.5)
        pairs = [(op["path"], op["other"], op["shared"]) for op in plan["operations"]]
        self.assertEqual(pairs, [("copy", "trip", 2)])

    def test_nested_echo_and_small_overlap_are_left_out(self) -> None:
        files = [
            _media("trip/a.jpg", "1"),
            _media("trip/day/b.jpg", "2"),
            _media("copy/a.jpg", "1"),
            _media("copy/day/b.jpg", "2"),
            _media("wide/1.jpg", "1"),
            _media("wide/2.jpg", "5"),
            _media("wide/3.jpg", "6"),
            _media("wide/4.jpg", "7"),
            _media("other/1.jpg", "1"),
            _media("other/2.jpg", "8"),
            _media("other/3.jpg", "9"),
            _media("other/4.jpg", "0"),
        ]
        plan = plan_folder_duplicates(files, "disk", min_shared=1, min_ratio=0.5, aggregate=True)
        pairs = {(op["path"], op["other"]) for op in plan["operations"]}
        self.assertIn(("copy", "trip"), pairs)
        self.assertNotIn(("copy/day", "trip/day"), pairs)
        self.assertNotIn(("other", "wide"), pairs)
        leaves = plan_folder_duplicates(files, "disk", min_shared=1, min_ratio=0.5)
        leaf_pairs = {(op["path"], op["other"]) for op in leaves["operations"]}
        self.assertIn(("copy/day", "trip/day"), leaf_pairs)
        self.assertIn(("copy", "trip"), leaf_pairs)

    def test_small_folder_qualifies_on_a_quarter_of_its_files(self) -> None:
        files = [_media(f"trip/{index}.jpg", str(index)) for index in range(8)]
        files += [_media(f"copy/{index}.jpg", str(index)) for index in range(3)]
        files += [_media(f"copy/only-{index}.jpg", f"x{index}") for index in range(5)]
        wide = plan_folder_duplicates(files, "disk", min_shared=10, min_ratio=0.25, either=True)
        strict = plan_folder_duplicates(files, "disk", min_shared=10, min_ratio=0.5)
        self.assertEqual([(op["path"], op["other"]) for op in wide["operations"]], [("copy", "trip")])
        self.assertEqual(strict["operations"], [])

    def test_interactive_merge_quarantines_the_other_folder(self) -> None:
        from no_file_mess.duplicates import ask_folder_merges, confirm_merge, folder_merge_plan

        pairs = [
            {
                "op": "note",
                "path": "year/album",
                "other": "year/loose",
                "shared": 2,
                "left_files": 2,
                "right_files": 2,
                "left_images": 2,
                "right_images": 2,
            },
            {
                "op": "note",
                "path": "year/month",
                "other": "outside",
                "shared": 1,
                "left_files": 1,
                "right_files": 2,
                "left_images": 1,
                "right_images": 2,
            },
        ]
        answers = iter(["1", "1", "m", "n"])
        opened: list[str] = []

        def reader(prompt: str) -> str:
            if prompt.startswith("apply"):
                return "n"
            return next(answers)

        decisions, stopped = ask_folder_merges(
            pairs,
            reader=reader,
            writer=lambda _line: None,
            opener=lambda left, right: opened.append(left) or [left, right],
        )
        self.assertFalse(stopped)
        self.assertEqual(decisions[0]["action"], "quarantine")
        self.assertEqual(decisions[1]["action"], "merge")
        files = [
            _media("year/album/a.jpg", "1"),
            _media("year/album/b.jpg", "2"),
            _media("year/loose/a.jpg", "1"),
            _media("year/loose/b.jpg", "2"),
            _media("year/month/a.jpg", "1"),
            _media("outside/a.jpg", "1"),
            _media("outside/only.jpg", "9"),
        ]
        plan = folder_merge_plan(files, decisions, "disk")
        self.assertEqual(
            sorted(op["path"] for op in plan["operations"] if op["op"] == "quarantine"),
            ["outside/a.jpg", "year/loose/a.jpg", "year/loose/b.jpg"],
        )
        moved = next(op for op in plan["operations"] if op["op"] == "move")
        self.assertEqual(moved["to"], "year/month/only.jpg")
        self.assertFalse(confirm_merge(plan, reader, lambda _line: None))

    def test_unhashed_files_still_count_in_the_folder(self) -> None:
        files = [
            _media("trip/a.jpg", "1"),
            _media("trip/b.jpg", "2"),
            _media("copy/a.jpg", "1"),
            _media("copy/b.jpg", "2"),
        ]
        files.extend(_media(f"trip/u{index}.jpg", "") for index in range(8))
        files.extend(_media(f"copy/v{index}.jpg", "") for index in range(8))
        plan = plan_folder_duplicates(files, "disk", min_shared=2, min_ratio=0.5)
        self.assertEqual(plan["operations"], [])

    def test_pair_counts_which_copy_is_newer(self) -> None:
        files = [
            FileRecord("disk", "a/one.jpg", 1, 10, "sha256", "1"),
            FileRecord("disk", "a/two.jpg", 1, 10, "sha256", "2"),
            FileRecord("disk", "b/one.jpg", 1, 20, "sha256", "1"),
            FileRecord("disk", "b/two.jpg", 1, 10, "sha256", "2"),
        ]
        plan = plan_folder_duplicates(files, "disk", min_shared=1, min_ratio=0.5)
        pair = plan["operations"][0]
        self.assertEqual(pair["path"], "a")
        self.assertEqual((pair["left_newer"], pair["right_newer"], pair["same_time"]), (0, 1, 1))

    def test_a_folder_is_not_compared_with_its_parent(self) -> None:
        files = [
            _media("trip/a.jpg", "1"),
            _media("trip/day/a.jpg", "1"),
        ]
        rolled = plan_folder_duplicates(files, "disk", min_shared=1, min_ratio=0.5, aggregate=True)
        self.assertEqual(rolled["operations"], [])
        leaves = plan_folder_duplicates(files, "disk", min_shared=1, min_ratio=0.5)
        self.assertEqual([(op["path"], op["other"]) for op in leaves["operations"]], [("trip", "trip/day")])


def _named(source: Path, dest: Path) -> tuple[list[FileRecord], list[FileRecord]]:
    return collect(source)[0], collect(dest)[0]


def _paths(plan: dict, kind: str) -> list[str]:
    return [op["path"] for op in plan["operations"] if op["op"] == kind]


def _media(rel: str, digest: str) -> FileRecord:
    return FileRecord("disk", rel, 1, 1, "sha256", digest)


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
