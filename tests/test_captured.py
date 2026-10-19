"""Capture dates from EXIF and filenames, and folder month plans."""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.bootstrap  # noqa: F401
from datetime import date

from no_file_mess.captured import (
    ask_action,
    ask_keep_or_split,
    confirm_changes,
    filename_date,
    judge_dates,
    majority_year,
    plan_dates,
)
from no_file_mess.names import FolderRules
from no_file_mess.plans import apply_plan, restore

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dated.jpg"


class FilenameTests(unittest.TestCase):
    def test_phone_names(self) -> None:
        cases = {
            "PXL_20260315_123045123.jpg": date(2026, 3, 15),
            "PXL_20260315_123045123.PORTRAIT.jpg": date(2026, 3, 15),
            "Screenshot20260315-120405.png": date(2026, 3, 15),
            "WhatsApp Image 2026-03-15 at 12.04.05.jpeg": date(2026, 3, 15),
            "WhatsApp Image 2026-03-15 at 12.04.05 (1).jpeg": date(2026, 3, 15),
            "20260315_120405-notes.jpg": date(2026, 3, 15),
            "IMG_4521.HEIC": None,
            "DSC01234.jpg": None,
            "PXL_20260231_120000000.jpg": None,
        }
        for name, expected in cases.items():
            self.assertEqual(filename_date(name), expected, name)


class JudgeTests(unittest.TestCase):
    def test_first_month_covers_a_two_month_spill(self) -> None:
        dates = [date(2026, 3, 2), date(2026, 5, 28)]
        verdict = judge_dates(2026, 3, dates)
        self.assertEqual(verdict.status, "ok")

    def test_a_later_label_proposes_the_earliest_month(self) -> None:
        dates = [date(2026, 3, 2), date(2026, 5, 28)]
        verdict = judge_dates(2026, 4, dates)
        self.assertEqual(verdict.status, "rename")
        self.assertEqual((verdict.year, verdict.month), (2026, 3))

    def test_a_spill_past_two_months_is_a_note_when_already_labeled_first(self) -> None:
        dates = [date(2026, 3, 2), date(2026, 7, 1)]
        verdict = judge_dates(2026, 3, dates)
        self.assertEqual(verdict.status, "note")

    def test_december_into_february_is_two_months(self) -> None:
        dates = [date(2025, 12, 20), date(2026, 2, 3)]
        verdict = judge_dates(2025, 12, dates)
        self.assertEqual(verdict.status, "ok")

    def test_no_dates(self) -> None:
        verdict = judge_dates(2026, 3, [])
        self.assertEqual(verdict.status, "note")


class PlanTests(unittest.TestCase):
    def test_exif_and_filename_dates_propose_the_earliest_month(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        folder = root / "April 2026 France"
        folder.mkdir()
        shutil.copy(_FIXTURE, folder / "camera.jpg")
        (folder / "PXL_20260502_120000123.jpg").write_bytes(b"not-a-jpeg")
        (root / "March 2026 Spain").mkdir()
        shutil.copy(_FIXTURE, root / "March 2026 Spain" / "camera.jpg")
        (root / "March 2026 Spain" / "PXL_20260502_120000123.jpg").write_bytes(b"x")
        wide = root / "March 2026 Wide"
        wide.mkdir()
        shutil.copy(_FIXTURE, wide / "camera.jpg")
        (wide / "PXL_20260702_120000123.jpg").write_bytes(b"x")

        plan = plan_dates(root, FolderRules())
        renames = [op for op in plan["operations"] if op["op"] == "rename_dir"]
        notes = [op for op in plan["operations"] if op["op"] == "note"]
        self.assertEqual(
            renames,
            [
                {
                    "op": "rename_dir",
                    "path": "April 2026 France",
                    "to": "2026-03-France",
                    "detail": "photos run 2026-03 through 2026-05",
                }
            ],
        )
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["path"], "March 2026 Wide")
        self.assertIn("spans more than 2 months", notes[0]["detail"])

    def test_interactive_choices_skip_rename_and_split(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        april = root / "April 2026 France"
        april.mkdir()
        shutil.copy(_FIXTURE, april / "camera.jpg")
        (april / "PXL_20260502_120000123.jpg").write_bytes(b"may")
        (april / "notes.txt").write_bytes(b"stay")
        spain = root / "March 2026 Spain"
        spain.mkdir()
        shutil.copy(_FIXTURE, spain / "camera.jpg")
        (spain / "PXL_20260502_120000123.jpg").write_bytes(b"may")
        answers = iter(["m", "s"])

        def reader(_prompt: str) -> str:
            return next(answers)

        shown: list[str] = []
        plan = plan_dates(root, FolderRules(), interactive=True, reader=reader, writer=shown.append)
        self.assertTrue(any("r rename to 2026-03-France" in line for line in shown))
        self.assertTrue(any("2026-05-France" in line for line in shown))
        splits = [op for op in plan["operations"] if op["op"] == "split_months"]
        self.assertEqual(len(splits), 1)
        self.assertEqual(splits[0]["path"], "April 2026 France")
        self.assertEqual(
            sorted(move["to"] for move in splits[0]["moves"]),
            ["2026-03-France/camera.jpg", "2026-05-France/PXL_20260502_120000123.jpg"],
        )
        self.assertFalse(any(op["op"] == "rename_dir" for op in plan["operations"]))

        self.assertEqual(apply_plan(plan, root), 1)
        self.assertEqual((root / "2026-03-France" / "camera.jpg").read_bytes()[:2], b"\xff\xd8")
        self.assertEqual((root / "2026-05-France" / "PXL_20260502_120000123.jpg").read_bytes(), b"may")
        self.assertEqual((root / "April 2026 France" / "notes.txt").read_bytes(), b"stay")
        self.assertTrue((root / "March 2026 Spain" / "camera.jpg").is_file())
        self.assertEqual(restore(root), 2)
        self.assertEqual((root / "April 2026 France" / "camera.jpg").read_bytes()[:2], b"\xff\xd8")
        self.assertFalse((root / "2026-03-France").exists())
        self.assertFalse((root / "2026-05-France").exists())

    def test_two_months_are_kept_until_the_photos_have_dates(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        undated = root / "2014 Jun-Dec Phone"
        undated.mkdir()
        (undated / "photo.jpg").write_bytes(b"not-a-photo")
        quiet = plan_dates(root, FolderRules(), reader=lambda _prompt: "m", writer=lambda _line: None)
        self.assertFalse(any(op["op"] == "split_months" for op in quiet["operations"]))
        self.assertIn("no capture dates", quiet["operations"][0]["detail"])

        dated = root / "2019 March-April Trip"
        dated.mkdir()
        (dated / "PXL_20190302_120000123.jpg").write_bytes(b"march")
        (dated / "PXL_20190402_120000123.jpg").write_bytes(b"april")
        asked: list[str] = []

        def reader(prompt: str) -> str:
            asked.append(prompt)
            return "m"

        plan = plan_dates(root, FolderRules(), interactive=True, reader=reader, writer=lambda _line: None)
        self.assertEqual(asked, ["[s] keep  [m] split  [!] rest  [q] stop  [h] help "])
        splits = [op for op in plan["operations"] if op["op"] == "split_months"]
        self.assertEqual(splits[0]["path"], "2019 March-April Trip")
        self.assertEqual(
            sorted(move["to"] for move in splits[0]["moves"]),
            ["2019-03-Trip/PXL_20190302_120000123.jpg", "2019-04-Trip/PXL_20190402_120000123.jpg"],
        )

    def test_enter_does_not_split_a_span(self) -> None:
        answers = iter(["", "s"])

        def reader(prompt: str) -> str:
            return next(answers)

        shown: list[str] = []
        choice = ask_keep_or_split("2014 Jun-Dec Phone", "photos run 2014-06 through 2014-12", ["2014-06-Phone"], reader, shown.append)
        self.assertEqual(choice, "s")
        self.assertIn("press s or m", shown)

    def test_ask_action_rejects_other_letters(self) -> None:
        answers = iter(["x", "h", "r"])

        def reader(_prompt: str) -> str:
            return next(answers)

        shown: list[str] = []
        choice = ask_action(
            "trip",
            "photos run 2026-03 through 2026-03",
            "2026-03-trip",
            "2026-trip",
            ["2026-03-trip"],
            reader,
            shown.append,
        )
        self.assertEqual(choice, "r")
        self.assertIn("choose s, r, y, m, q, !, or h", shown)
        self.assertTrue(any("year only" in line for line in shown))
        self.assertFalse(any(line.startswith("  m ") for line in shown))

    def test_year_only_folder_is_left_alone(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        folder = root / "2026-Toronto"
        folder.mkdir()
        (folder / "PXL_20260115_120000123.jpg").write_bytes(b"jan")

        def reader(_prompt: str) -> str:
            raise AssertionError("a matching year-only folder needs no question")

        plan = plan_dates(root, FolderRules(), interactive=True, reader=reader, writer=lambda _line: None)
        self.assertEqual(plan["operations"], [])

    def test_year_key_uses_the_majority_year(self) -> None:
        self.assertEqual(
            majority_year([date(2025, 12, 1), date(2026, 5, 1), date(2026, 5, 2)]),
            2026,
        )
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        folder = root / "2026-02-Gymnastics_Slony Cup"
        folder.mkdir()
        (folder / "PXL_20260115_120000123.jpg").write_bytes(b"jan")
        answers = iter(["y"])

        def reader(_prompt: str) -> str:
            return next(answers)

        shown: list[str] = []
        plan = plan_dates(root, FolderRules(), interactive=True, reader=reader, writer=shown.append)
        self.assertFalse(any(line.startswith("  m ") for line in shown))
        self.assertTrue(any("r rename to 2026-01-Gymnastics_Slony Cup" in line for line in shown))
        renames = [op for op in plan["operations"] if op["op"] == "rename_dir"]
        self.assertEqual(renames[0]["to"], "2026-Gymnastics_Slony Cup")

    def test_quit_keeps_answers_and_stops(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        first = root / "April 2026 France"
        first.mkdir()
        shutil.copy(_FIXTURE, first / "camera.jpg")
        (first / "PXL_20260502_120000123.jpg").write_bytes(b"may")
        later = root / "May 2026 Spain"
        later.mkdir()
        (later / "PXL_20260115_120000123.jpg").write_bytes(b"jan")
        prompts: list[str] = []
        answers = iter(["r", "q"])

        def reader(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        plan = plan_dates(root, FolderRules(), interactive=True, reader=reader, writer=lambda _line: None)
        self.assertEqual(
            prompts,
            [
                "[s/r/Y/m/q] or [!] rest or [h] for help ",
                "[s/R/y/m/q] or [!] rest or [h] for help ",
            ],
        )
        renames = [op for op in plan["operations"] if op["op"] == "rename_dir"]
        self.assertEqual([op["path"] for op in renames], ["April 2026 France"])
        self.assertFalse(any("Spain" in op.get("path", "") for op in plan["operations"]))

    def test_enter_picks_the_marked_default(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        single = root / "2026-02-Trip"
        single.mkdir()
        (single / "PXL_20260115_120000123.jpg").write_bytes(b"jan")
        several = root / "April 2026 Wide"
        several.mkdir()
        shutil.copy(_FIXTURE, several / "camera.jpg")
        (several / "PXL_20260502_120000123.jpg").write_bytes(b"may")
        answers = iter(["", ""])

        def reader(_prompt: str) -> str:
            return next(answers)

        plan = plan_dates(root, FolderRules(), interactive=True, reader=reader, writer=lambda _line: None)
        renames = [op for op in plan["operations"] if op["op"] == "rename_dir"]
        self.assertEqual(
            [(op["path"], op["to"]) for op in renames],
            [("2026-02-Trip", "2026-01-Trip"), ("April 2026 Wide", "2026-Wide")],
        )

    def test_bang_uses_defaults_for_the_rest(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        several = root / "April 2026 Wide"
        several.mkdir()
        shutil.copy(_FIXTURE, several / "camera.jpg")
        (several / "PXL_20260502_120000123.jpg").write_bytes(b"may")
        single = root / "May 2026 Spain"
        single.mkdir()
        (single / "PXL_20260115_120000123.jpg").write_bytes(b"jan")
        calls = {"n": 0}

        def reader(_prompt: str) -> str:
            calls["n"] += 1
            return "!"

        plan = plan_dates(root, FolderRules(), interactive=True, reader=reader, writer=lambda _line: None)
        self.assertEqual(calls["n"], 1)
        renames = [(op["path"], op["to"]) for op in plan["operations"] if op["op"] == "rename_dir"]
        self.assertEqual(
            sorted(renames),
            [("April 2026 Wide", "2026-Wide"), ("May 2026 Spain", "2026-01-Spain")],
        )

    def test_confirmation_requires_an_explicit_answer(self) -> None:
        plan = {
            "operations": [
                {"op": "rename_dir", "path": "April 2026 Wide", "to": "2026-Wide"},
            ]
        }
        answers = iter(["", "n"])

        def reader(_prompt: str) -> str:
            return next(answers)

        shown: list[str] = []
        self.assertFalse(confirm_changes(plan, reader, shown.append))
        self.assertIn("press y or n", shown)
        self.assertTrue(any("April 2026 Wide  ->  2026-Wide" in line for line in shown))
        shown.clear()
        self.assertTrue(confirm_changes(plan, lambda _prompt: "y", shown.append))
        self.assertFalse(confirm_changes({"operations": []}, lambda _prompt: "y", shown.append))


if __name__ == "__main__":
    unittest.main()
