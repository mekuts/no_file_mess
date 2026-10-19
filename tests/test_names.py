"""Folder title recognition and rename plans."""

import tempfile
import unittest
from pathlib import Path

import tests.bootstrap  # noqa: F401
from no_file_mess.names import FolderRules, parse_title, plan_renames, span_remainder, title_remainder


class ParseTests(unittest.TestCase):
    def test_default_template_moves_the_date_forward(self) -> None:
        rules = FolderRules()
        cases = {
            "March 2026 France": "2026-03-France",
            "France 2026_03": "2026-03-France",
            "03-2026-France": "2026-03-France",
            "South of France, March 2026": "2026-03-South-of-France",
            "2026-03-France": "2026-03-France",
        }
        for title, expected in cases.items():
            parsed = parse_title(title, rules)
            self.assertIsNotNone(parsed, title)
            assert parsed is not None
            self.assertEqual(parsed.basename, expected, title)

    def test_missing_month_or_day_is_skipped(self) -> None:
        rules = FolderRules()
        cases = {
            "2026-03-15 France": "2026-03-15-France",
            "March 2026 France": "2026-03-France",
            "Mar 2018 Trip": "2018-03-Trip",
            "Queen of Hearts 2018": "2018-Queen-of-Hearts",
            "2018 Queen of Hearts": "2018-Queen-of-Hearts",
        }
        for title, expected in cases.items():
            parsed = parse_title(title, rules)
            self.assertIsNotNone(parsed, title)
            assert parsed is not None
            self.assertEqual(parsed.status, "rename", title)
            self.assertEqual(parsed.basename, expected, title)

    def test_template_without_day_records_that_the_day_was_dropped(self) -> None:
        parsed = parse_title("2026-03-15 France", FolderRules(template="{year}-{month}-{name}"))
        assert parsed is not None
        self.assertEqual(parsed.basename, "2026-03-France")
        self.assertEqual(parsed.detail, "day omitted")

    def test_shapes_with_a_year_first_day(self) -> None:
        rules = FolderRules()
        cases = {
            "2026-08-02 France": "2026-08-02-France",
            "2026-08-02 South of France": "2026-08-02-South-of-France",
            "March 02 2026 France": "2026-03-02-France",
            "2024-02-20 France": "2024-02-20-France",
            "2026-02": "2026-02",
            "06-2026 South of France": "2026-06-South-of-France",
            "2026_Queen_of-Hearts": "2026-Queen-of-Hearts",
            "202502__": "2025-02",
            "202403": "2024-03",
            "IMG_20191225_092711": "2019-12-25-IMG",
        }
        for title, expected in cases.items():
            parsed = parse_title(title, rules)
            self.assertIsNotNone(parsed, title)
            assert parsed is not None
            self.assertEqual(parsed.basename, expected, title)

    def test_year_span_and_undated_title_are_left_alone(self) -> None:
        span = parse_title("2025-2026 France", FolderRules())
        assert span is not None
        self.assertEqual(span.status, "ambiguous")
        self.assertIsNone(parse_title("South of France", FolderRules()))

    def test_remainder_keeps_the_written_separators(self) -> None:
        self.assertEqual(title_remainder("2026-02-Gymnastics_Slony Cup"), "Gymnastics_Slony Cup")
        self.assertEqual(title_remainder("2026-Toronto"), "Toronto")
        self.assertEqual(title_remainder("April 2026 France"), "France")
        self.assertEqual(title_remainder("IMG_20191225_092711"), "IMG")
        self.assertEqual(title_remainder("202502__"), "")

    def test_ambiguous_and_year_only_opt_out(self) -> None:
        rules = FolderRules()
        ambiguous = parse_title("01-02-2026 Trip", rules)
        assert ambiguous is not None
        self.assertEqual(ambiguous.status, "ambiguous")

        year_only = parse_title("Queen of Hearts 2018", FolderRules(year_only=False))
        self.assertIsNone(year_only)


class PlanTests(unittest.TestCase):
    def test_collision_is_a_note_and_canonical_names_are_quiet(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: _remove(root))
        (root / "March 2026 France").mkdir()
        (root / "2026_03 France").mkdir()
        (root / "2026-03-Spain").mkdir()
        (root / "loose").mkdir()
        plan = plan_renames(root, FolderRules(), volume="disk-a")
        ops = plan["operations"]
        self.assertEqual(plan["volume"], "disk-a")
        self.assertFalse(any(op["op"] == "rename_dir" for op in ops))
        collisions = [op for op in ops if op.get("status") == "collision"]
        self.assertEqual(len(collisions), 2)
        self.assertFalse(any(op["path"] == "loose" for op in ops))
        self.assertFalse(any(op["path"] == "2026-03-Spain" for op in ops))

    def test_rename_keeps_the_parent(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: _remove(root))
        (root / "trips" / "March 2026 France").mkdir(parents=True)
        plan = plan_renames(root, FolderRules())
        renames = [op for op in plan["operations"] if op["op"] == "rename_dir"]
        self.assertEqual(renames, [{"op": "rename_dir", "path": "trips/March 2026 France", "to": "trips/2026-03-France"}])

    def test_a_year_under_a_year_keeps_the_year_and_month(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nfm-"))
        self.addCleanup(lambda: _remove(root))
        (root / "2025" / "02_2025").mkdir(parents=True)
        (root / "2025" / "Spain_2025").mkdir()
        (root / "2025" / "2019").mkdir()
        plan = plan_renames(root, FolderRules())
        renames = {op["path"]: op["to"] for op in plan["operations"] if op["op"] == "rename_dir"}
        self.assertEqual(renames["2025/02_2025"], "2025/2025-02")
        self.assertEqual(renames["2025/Spain_2025"], "2025/2025-Spain")
        notes = [op for op in plan["operations"] if op.get("status") == "year"]
        self.assertEqual([op["path"] for op in notes], ["2025/2019"])
        self.assertEqual(span_remainder("2014 Jun-Dec Phone"), "Phone")
        self.assertEqual(span_remainder("2021-2024-desktop"), "desktop")


def _remove(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
