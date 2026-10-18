"""Compare a folder's name with the capture dates of the files inside it.

The capture date is EXIF ``DateTimeOriginal`` when the file has one.
JPEG and HEIC are both read with exifread. A phone download that kept
the date in the filename is used when EXIF is missing. The proposed
folder month is the earliest month found. A folder already named for
that month is left alone when the other files fall within ``slack``
months after it, so March through May can stay labeled March.
"""

from __future__ import annotations

import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from no_file_mess.errors import NoFileMessError
from no_file_mess.names import (
    FolderRules,
    _collapse,
    _directories,
    parse_title,
    title_remainder,
)
from no_file_mess.paths import to_rel

_MEDIA = {
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".png",
    ".webp",
    ".tif",
    ".tiff",
    ".mov",
    ".mp4",
}
# Prefer the shutter date. Image DateTime is the last time software rewrote
# the file, so it is not a capture date.
_EXIF_TAGS = (
    "EXIF DateTimeOriginal",
    "Image DateTimeOriginal",
    "EXIF DateTimeDigitized",
    "Image DateTimeDigitized",
)
_EXIF_STAMP = re.compile(
    r"(?P<year>19\d{2}|20\d{2}):(?P<month>0[1-9]|1[0-2]):(?P<day>0[1-9]|[12]\d|3[01])"
)
_SEPARATED = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})[-_.](?P<month>0?[1-9]|1[0-2])[-_.](?P<day>0?[1-9]|[12]\d|3[01])(?!\d)"
)
_COMPACT = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})(?P<month>0[1-9]|1[0-2])(?P<day>0[1-9]|[12]\d|3[01])(?!\d)"
)


@dataclass(frozen=True)
class DateVerdict:
    """Whether a folder month agrees with the files inside it.

    Attributes:
        status: ``ok``, ``rename``, or ``note``.
        year: Earliest year, when any capture date was found.
        month: Earliest month.
        detail: The span the decision was based on.
    """

    status: str
    year: int | None
    month: int | None
    detail: str


def filename_date(name: str) -> date | None:
    """Return a capture date embedded in a phone filename.

    Args:
        name: File name, with or without a directory.

    Returns:
        The first valid ``YYYY-MM-DD`` or ``YYYYMMDD`` in the name, or
        ``None`` when the name has neither. A short counter such as
        ``IMG_4521`` is not a date.
    """
    stem = Path(name).stem
    found: list[tuple[int, date]] = []
    for pattern in (_SEPARATED, _COMPACT):
        for match in pattern.finditer(stem):
            parsed = _make_date(match.group("year"), match.group("month"), match.group("day"))
            if parsed is not None:
                found.append((match.start(), parsed))
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[0][1]


def exif_date(path: Path) -> date | None:
    """Return ``DateTimeOriginal`` from a JPEG, HEIC, or other supported file.

    Args:
        path: Image to open. The file is only read.

    Returns:
        The capture date, or ``None`` when the file has no such tag or
        cannot be read.
    """
    try:
        import exifread
    except ImportError as exc:
        raise NoFileMessError("exifread is not installed; pip install exifread") from exc
    # Missing EXIF is normal for downloads. exifread warns on every such file.
    logging.getLogger("exifread").setLevel(logging.ERROR)
    try:
        with path.open("rb") as handle:
            tags = exifread.process_file(handle, details=False)
    except (OSError, Exception):
        # A truncated or unfamiliar file should not stop the scan.
        # The filename can still supply a date.
        return None
    for key in _EXIF_TAGS:
        value = tags.get(key)
        if value is None:
            continue
        match = _EXIF_STAMP.search(str(value))
        if match is None:
            continue
        parsed = _make_date(match.group("year"), match.group("month"), match.group("day"))
        if parsed is not None:
            return parsed
    return None


def capture_date(path: Path) -> date | None:
    """Return the capture date of one file.

    EXIF wins when both EXIF and the filename carry a date. The filename
    is the fallback for downloads that dropped the metadata.

    Args:
        path: File to inspect.

    Returns:
        A date, or ``None`` when neither source has one.
    """
    found = exif_date(path)
    if found is not None:
        return found
    return filename_date(path.name)


def judge_dates(
    folder_year: int | None,
    folder_month: int | None,
    dates: list[date],
    slack: int = 2,
) -> DateVerdict:
    """Decide whether ``folder_month`` can stand for ``dates``.

    Args:
        folder_year: Year parsed from the folder title, if any.
        folder_month: Month parsed from the folder title, if any.
        dates: Capture dates of the files directly inside the folder.
        slack: How many months after the earliest month may still share
            that folder. ``2`` allows March through May to stay March.

    Returns:
        ``ok`` when the folder is the earliest month and the span is within
        ``slack``. ``rename`` proposes the earliest month otherwise.
        ``note`` when there are no dates, or the folder is already the
        earliest month but the photos run further than ``slack``.
    """
    if slack < 0:
        raise NoFileMessError("slack must be zero or more")
    if not dates:
        return DateVerdict("note", None, None, "no capture dates")
    earliest = min(dates)
    latest = max(dates)
    span = (latest.year - earliest.year) * 12 + (latest.month - earliest.month)
    detail = f"photos run {earliest:%Y-%m} through {latest:%Y-%m}"
    labeled = folder_year == earliest.year and folder_month == earliest.month
    if labeled and span <= slack:
        return DateVerdict("ok", earliest.year, earliest.month, detail)
    if labeled:
        return DateVerdict("note", earliest.year, earliest.month, detail + f"; spans more than {slack} months")
    return DateVerdict("rename", earliest.year, earliest.month, detail)


def plan_dates(
    root: Path,
    rules: FolderRules,
    slack: int = 2,
    volume: str | None = None,
    interactive: bool = False,
    reader=None,
    writer=None,
) -> dict:
    """Plan renames for folders whose names disagree with capture dates.

    Only files directly inside a folder are counted. A folder with no
    photos or videos is skipped. Ambiguous titles are notes.

    Args:
        root: Tree to read. Nothing under it is modified.
        rules: Template for a proposed name. The day is left out.
        slack: Months after the earliest that still match that month.
        volume: Optional name copied into the plan.
        interactive: Ask for a key on each folder that still needs a
            decision. A year-only name whose year already has the most
            photos is left alone. ``s`` skips, ``r`` uses the earliest
            month, ``y`` keeps the year only, ``m`` splits by month,
            ``q`` stops, ``!`` uses the default for the rest, and ``h``
            explains the keys. A letter acts immediately. Enter selects
            the default. Nothing is renamed until a later confirmation.
        reader: Called with the prompt. Returns the typed text. Defaults
            to a single keystroke when standard input is a terminal.
        writer: Line writer used when ``interactive`` is set. Defaults to
            :func:`print`.

    Returns:
        A versioned plan. Rename and split operations are safe to ``apply``.
    """
    root = root.resolve()
    if not root.is_dir():
        raise NoFileMessError(f"not a directory: {root}")
    if reader is None:
        reader = read_key
    if writer is None:
        writer = print

    proposals: list[tuple[str, str, str]] = []
    splits: list[dict] = []
    notes: list[dict] = []
    accept_rest = False
    directories = sorted(_directories(root), key=lambda path: to_rel(root, path))
    for directory in directories:
        media = _direct_media(directory)
        if media is None:
            continue
        dates = [item for _path, item in media if item is not None]
        rel = to_rel(root, directory)
        parsed = parse_title(directory.name, rules)
        if parsed is not None and parsed.status != "rename":
            notes.append(
                {
                    "op": "note",
                    "path": rel,
                    "status": parsed.status,
                    "detail": parsed.detail,
                }
            )
            continue
        folder_year = parsed.year if parsed is not None else None
        folder_month = parsed.month if parsed is not None else None
        verdict = judge_dates(folder_year, folder_month, dates, slack)
        parent = PurePosixPath(rel).parent
        parent_text = "" if parent == PurePosixPath(".") else parent.as_posix()
        remainder = title_remainder(directory.name)
        months = sorted({(item.year, item.month) for item in dates})
        chosen_year = majority_year(dates) if dates else None
        default = ""
        year_only = ""
        if verdict.year is not None and verdict.month is not None:
            default = _dated_name(parent_text, verdict.year, verdict.month, remainder)
        if chosen_year is not None:
            year_only = _dated_name(parent_text, chosen_year, None, remainder)
        month_folders = [
            _dated_name(parent_text, year, month, remainder) for year, month in months
        ]
        if folder_month is None and chosen_year is not None and folder_year == chosen_year:
            continue
        if interactive and _needs_choice(verdict, months):
            if accept_rest:
                choice = _default_choice(len(months))
            else:
                choice = ask_action(
                    rel,
                    verdict.detail,
                    default,
                    year_only,
                    month_folders,
                    reader,
                    writer,
                )
                if choice == "!":
                    accept_rest = True
                    choice = _default_choice(len(months))
            if choice == "q":
                break
            if choice == "s" or verdict.year is None:
                continue
            if choice == "y":
                if year_only and year_only != rel:
                    proposals.append((rel, year_only, f"majority year {chosen_year}"))
                continue
            if choice == "r":
                if default and default != rel:
                    proposals.append((rel, default, verdict.detail))
                continue
            split = _split_operation(rel, media, month_folders, months, verdict.detail)
            if split is not None:
                splits.append(split)
            continue
        if folder_month is None and year_only and year_only != rel:
            proposals.append((rel, year_only, f"majority year {chosen_year}"))
            continue
        if verdict.status == "ok":
            continue
        if verdict.status != "rename" or not default or default == rel:
            notes.append(
                {
                    "op": "note",
                    "path": rel,
                    "status": "dates",
                    "detail": verdict.detail,
                }
            )
            continue
        proposals.append((rel, default, verdict.detail))

    operations = list(notes)
    operations.extend(splits)
    operations.extend(_collapse(root, proposals))
    return {
        "version": 1,
        "kind": "dates",
        "volume": volume,
        "operations": operations,
    }


_KEYS = "s/r/y/m/q"
_HELP = """\
s  skip this folder
r  rename to the earliest photo month
y  year only, using the year with the most photos
m  one folder per month, when the photos span more than one
q  stop, and keep the choices already made
!  use the default for this folder and every folder after it
h  show this help
Enter  the capital letter"""


def read_key(prompt: str = "") -> str:
    """Read one key when input is a terminal, otherwise one line.

    A letter acts immediately. Enter selects the default. When input is
    not a terminal, a whole line is read so tests and pipes still work.

    Args:
        prompt: Text written before the key, without a trailing newline.

    Returns:
        The character, or the line when input is not a terminal.

    Raises:
        EOFError: Input ended, or Ctrl-D was pressed.
    """
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line
    char = _one_char()
    if char == "\x04":
        raise EOFError
    if char == "\x03":
        raise KeyboardInterrupt
    sys.stdout.write("\n" if char in {"\r", "\n"} else char + "\n")
    sys.stdout.flush()
    return char


def ask_action(
    path: str,
    detail: str,
    rename_to: str,
    year_only: str,
    month_folders: list[str],
    reader,
    writer,
) -> str:
    """Ask whether to skip, rename, use the year, or split one folder.

    Args:
        path: Folder being decided, relative to the scan root.
        detail: Span of the capture dates.
        rename_to: Earliest-month name, keeping the folder's own words.
        year_only: Year-only name. The year is the one with the most photos.
        month_folders: One folder per month present, earliest first.
        reader: Called with the prompt. Returns the key or the typed line.
        writer: Called with each line to show before the prompt.

    Returns:
        ``s``, ``r``, ``y``, ``m``, or ``q``. ``q`` stops the scan.
        Enter returns ``y`` when the folder has more than one month,
        and ``r`` otherwise. The prompt shows that letter in capitals.
        ``!`` means use that default from here on.

    Raises:
        NoFileMessError: Input ended before a choice was made.
    """

    def show() -> None:
        writer(path)
        writer(f"  {detail}")
        writer(f"  r rename to {rename_to}")
        writer(f"  y year only {year_only}")
        if len(month_folders) > 1:
            writer("  m split into " + ", ".join(month_folders))

    default = _default_choice(len(month_folders))
    show()
    while True:
        try:
            answer = reader(_prompt(default)).strip().lower()
        except EOFError as exc:
            raise NoFileMessError("stopped before every folder was answered") from exc
        if answer == "":
            return default
        if answer == "h":
            writer(_HELP)
            show()
            continue
        if answer == "m" and len(month_folders) <= 1:
            writer("only one month in this folder")
            continue
        if answer in {"s", "r", "y", "m", "q", "!"}:
            return answer
        writer("choose s, r, y, m, q, !, or h")


def _default_choice(month_count: int) -> str:
    """``y`` when a folder spans months, otherwise ``r``."""
    return "y" if month_count > 1 else "r"


def _prompt(default: str) -> str:
    """Show the keys, with ``default`` in capitals. Enter selects it."""
    marked = "/".join(key.upper() if key == default else key for key in _KEYS.split("/"))
    return f"[{marked}] or [!] rest or [h] for help "


def change_lines(plan: dict) -> list[str]:
    """Lines that describe the renames and moves in ``plan``."""
    lines: list[str] = []
    for op in plan["operations"]:
        if op["op"] == "rename_dir":
            lines.append(f"{op['path']}  ->  {op['to']}")
            continue
        if op["op"] != "split_months":
            continue
        counts: Counter[str] = Counter()
        for move in op["moves"]:
            parent = str(PurePosixPath(move["to"]).parent)
            counts[parent] += 1
        lines.append(op["path"])
        for dest, count in sorted(counts.items()):
            word = "file" if count == 1 else "files"
            lines.append(f"  {count} {word} -> {dest}")
    return lines


def confirm_changes(plan: dict, reader, writer) -> bool:
    """Ask for an explicit ``y`` or ``n`` before any rename or move.

    Enter does not accept the plan. A plan with nothing to change is
    refused without a question.

    Args:
        plan: A date plan from :func:`plan_dates`.
        reader: Called with the confirmation prompt.
        writer: Called with each review line.

    Returns:
        ``True`` only when the answer is ``y``.
    """
    lines = change_lines(plan)
    if not lines:
        writer("nothing to rename or move")
        return False
    writer("review")
    for line in lines:
        writer(line)
    while True:
        try:
            answer = reader("apply these changes? [y/n] ").strip().lower()
        except EOFError as exc:
            raise NoFileMessError("stopped before the changes were confirmed") from exc
        if answer in {"y", "n"}:
            return answer == "y"
        writer("press y or n")


def _needs_choice(verdict: DateVerdict, months: list[tuple[int, int]]) -> bool:
    """A single month that already matches the folder name needs no question."""
    if verdict.status == "note" and verdict.year is None:
        return False
    if verdict.status == "ok" and len(months) <= 1:
        return False
    return True


def _split_operation(
    folder: str,
    media: list[tuple[Path, date | None]],
    month_folders: list[str],
    months: list[tuple[int, int]],
    detail: str,
) -> dict | None:
    """Build a split that moves each dated file into its month folder."""
    by_month = dict(zip(months, month_folders))
    moves = []
    for path, captured in media:
        if captured is None:
            continue
        dest_dir = by_month[(captured.year, captured.month)]
        dest = f"{dest_dir}/{path.name}"
        source = f"{folder}/{path.name}"
        if dest == source:
            continue
        moves.append({"path": source, "to": dest})
    if not moves:
        return None
    return {
        "op": "split_months",
        "path": folder,
        "detail": detail,
        "moves": moves,
    }


def majority_year(dates: list[date]) -> int:
    """Year that contains the most of ``dates``. A tie uses the later year.

    December 2025 through May 2026 is 2026 when May has at least as many
    photos as December. Equal counts also pick the later year.
    """
    counts = Counter(item.year for item in dates)
    return max(counts, key=lambda year: (counts[year], year))


def _dated_name(parent: str, year: int, month: int | None, remainder: str) -> str:
    """Place ``year`` and an optional month in front of the original words."""
    head = f"{year:04d}" if month is None else f"{year:04d}-{month:02d}"
    base = head if not remainder else f"{head}-{remainder}"
    return base if not parent else f"{parent}/{base}"


def _one_char() -> str:
    """Read one character without waiting for Enter."""
    try:
        import termios
        import tty
    except ImportError:
        import msvcrt

        return msvcrt.getwch()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _direct_media(directory: Path) -> list[tuple[Path, date | None]] | None:
    """Media files placed directly in ``directory``, with capture dates.

    Returns:
        ``None`` when the folder contains no media files. Dates are
        ``None`` when a file has neither EXIF nor a date in its name.
    """
    found: list[tuple[Path, date | None]] = []
    try:
        children = list(directory.iterdir())
    except OSError as exc:
        raise NoFileMessError(f"cannot list {directory}") from exc
    for child in children:
        if not child.is_file() or child.is_symlink():
            continue
        if child.suffix.lower() not in _MEDIA:
            continue
        found.append((child, capture_date(child)))
    if not found:
        return None
    return found


def _make_date(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None
