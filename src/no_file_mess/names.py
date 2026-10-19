"""Folder-name recognition and configurable rewriting.

The recognizer looks for one year, and then a month and a day when they
are present. Everything else, in the same order, is the name. The
default template is ``{year}-{month}-{day}-{name}``. An empty part is
left out, together with the separator written in front of it, so a
title with only a year still renames. Ambiguous titles are reported
and left alone.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from no_file_mess.errors import NoFileMessError
from no_file_mess.paths import to_rel

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_TOKEN_SPLIT = re.compile(r"[\s_.,-]+")
_PLACEHOLDER = re.compile(r"\{([a-z]+)\}")
_SEPARATOR = re.compile(r"[\s._-]+")
_YEAR_MIN = 1900
_YEAR_MAX = 2099


@dataclass(frozen=True)
class FolderRules:
    """How a recognized folder title is written back.

    Attributes:
        template: Placeholders ``year``, ``month``, ``day``, and ``name``.
            A placeholder with no value is omitted, along with the
            separator written immediately before it.
        separator: Joiner placed between the leftover words.
        year_only: When false, a title with a year and no month is left
            unchanged. The default renames it with the month and day omitted.
    """

    template: str = "{year}-{month}-{day}-{name}"
    separator: str = "-"
    year_only: bool = True

    @classmethod
    def from_path(cls, path: Path | None) -> "FolderRules":
        """Load rules from JSON, or return the defaults.

        Args:
            path: Rules file. ``None`` selects the defaults.

        Returns:
            Parsed rules.

        Raises:
            NoFileMessError: The file is missing or a field has the wrong type.
        """
        if path is None:
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise NoFileMessError(f"cannot read folder rules: {path}") from exc
        except json.JSONDecodeError as exc:
            raise NoFileMessError(f"folder rules are not valid JSON: {path}") from exc
        if not isinstance(data, dict):
            raise NoFileMessError("folder rules must be a JSON object")
        try:
            return cls(
                template=str(data.get("template", cls.template)),
                separator=str(data.get("separator", cls.separator)),
                year_only=bool(data.get("year_only", cls.year_only)),
            )
        except (TypeError, ValueError) as exc:
            raise NoFileMessError("folder rules have an invalid field") from exc


@dataclass(frozen=True)
class ParsedTitle:
    """A folder title after date tokens have been taken out.

    Attributes:
        status: ``rename`` or ``ambiguous``.
        basename: Proposed folder name when status is ``rename``.
        detail: Why the title was kept or how the name was built.
        name: Leftover words, joined with the rules separator.
        year: Four-digit year found in the title.
        month: Month number when one was found.
    """

    status: str
    basename: str | None
    detail: str
    name: str = ""
    year: int | None = None
    month: int | None = None


def parse_title(title: str, rules: FolderRules) -> ParsedTitle | None:
    """Recognize a date in ``title`` and render the configured name.

    Args:
        title: A single folder name, not a path.
        rules: Output template and options.

    Returns:
        ``None`` when the title has no year. A :class:`ParsedTitle` when
        a year was found, including ambiguous titles that must not be renamed.
    """
    tokens = _date_tokens(title)
    years = [token for token in tokens if _is_year(token)]
    if not years:
        return None
    if len(years) > 1:
        return ParsedTitle("ambiguous", None, "more than one year")

    year = years[0]
    found = _month_and_day(tokens, year)
    if found.detail:
        return ParsedTitle("ambiguous", None, found.detail)
    month = found.month
    day = found.day
    used = set(years) | found.used

    name_tokens = [token for token in tokens if token not in used]
    name = rules.separator.join(name_tokens)
    if month is None and not rules.year_only:
        return None

    # A recognized month or day that the template does not mention would
    # otherwise vanish with no trace. Absence of a part is normal.
    omitted: list[str] = []
    if month is not None and "{month}" not in rules.template:
        omitted.append("month")
    if day is not None and "{day}" not in rules.template:
        omitted.append("day")
    rendered = _render(
        rules.template,
        {
            "year": year,
            "month": f"{month:02d}" if month is not None else "",
            "day": f"{day:02d}" if day is not None else "",
            "name": name,
        },
    )
    if not rendered:
        return None
    detail = " and ".join(omitted) + " omitted" if omitted else ""
    return ParsedTitle("rename", rendered, detail, name, int(year), month)


def title_remainder(title: str) -> str:
    """Return the folder title with its date tokens removed.

    Separators between the remaining words stay as they were written.
    ``2026-02-Gymnastics_Slony Cup`` becomes ``Gymnastics_Slony Cup``.

    Args:
        title: A single folder name, not a path.

    Returns:
        The non-date text, or the original title when it has no single year.
    """
    pieces = re.split(r"([\s_.,-]+)", title.strip())
    raw = [piece for piece in pieces if piece and not _TOKEN_SPLIT.fullmatch(piece)]
    glued = {piece for piece in raw if _glued_date(piece) is not None}
    clocks = {piece for piece in raw if _has_glued_day(raw) and _is_clock(piece)}
    tokens = _date_tokens(title)
    years = [token for token in tokens if _is_year(token)]
    if len(years) != 1:
        return title.strip()
    found = _month_and_day(tokens, years[0])
    used = set(years) | found.used
    kept: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        if _TOKEN_SPLIT.fullmatch(piece):
            kept.append(piece)
            continue
        if piece in glued or piece in clocks or piece in used:
            used.discard(piece)
            if kept and _TOKEN_SPLIT.fullmatch(kept[-1]):
                kept.pop()
            continue
        kept.append(piece)
    while kept and _TOKEN_SPLIT.fullmatch(kept[0]):
        kept.pop(0)
    while kept and _TOKEN_SPLIT.fullmatch(kept[-1]):
        kept.pop()
    return "".join(kept).strip()


def bare_year(title: str) -> bool:
    """Whether ``title`` is a year and nothing else."""
    return _is_year(title.strip())


def span_remainder(title: str) -> str:
    """Words left after every year, month name, and glued date is removed.

    ``2014 Jun-Dec Phone`` becomes ``Phone``. A title with no year is
    returned unchanged.

    Args:
        title: A single folder name, not a path.

    Returns:
        The leftover words, with the separators that were between them.
    """
    pieces = re.split(r"([\s_.,-]+)", title.strip())
    raw = [piece for piece in pieces if piece and not _TOKEN_SPLIT.fullmatch(piece)]
    drop = {piece for piece in raw if _glued_date(piece) is not None or _is_year(piece) or piece.casefold() in _MONTHS}
    if _has_glued_day(raw):
        drop.update(piece for piece in raw if _is_clock(piece))
    if not any(_is_year(token) for token in _date_tokens(title)):
        return title.strip()
    kept: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        if _TOKEN_SPLIT.fullmatch(piece):
            kept.append(piece)
            continue
        if piece in drop:
            if kept and _TOKEN_SPLIT.fullmatch(kept[-1]):
                kept.pop()
            continue
        kept.append(piece)
    while kept and _TOKEN_SPLIT.fullmatch(kept[0]):
        kept.pop(0)
    while kept and _TOKEN_SPLIT.fullmatch(kept[-1]):
        kept.pop()
    return "".join(kept).strip()


def format_folder(
    year: int,
    month: int | None,
    name: str,
    rules: FolderRules,
    day: int | None = None,
) -> str:
    """Render a folder name from parts that are already known.

    Args:
        year: Four-digit year.
        month: Month number, or ``None`` to omit it.
        name: Leftover words, already joined.
        rules: Output template.
        day: Day of the month, or ``None`` to omit it.

    Returns:
        The template with empty parts and their separators removed.
    """
    return _render(
        rules.template,
        {
            "year": f"{year:04d}",
            "month": f"{month:02d}" if month is not None else "",
            "day": f"{day:02d}" if day is not None else "",
            "name": name,
        },
    )


def plan_renames(root: Path, rules: FolderRules, volume: str | None = None) -> dict:
    """Build a rename plan for directories under ``root``.

    The root directory itself is never renamed. Two directories that would
    land on the same name, or a rename onto a name that already exists,
    become notes. Apply ignores notes.

    Args:
        root: Tree whose child directories are considered.
        rules: Output template.
        volume: Optional name copied into the plan for the reader.

    Returns:
        A versioned plan document.
    """
    root = root.resolve()
    if not root.is_dir():
        raise NoFileMessError(f"not a directory: {root}")

    proposals: list[tuple[str, str, str]] = []
    notes: list[dict] = []
    for directory in _directories(root):
        rel = to_rel(root, directory)
        parent_path = PurePosixPath(rel).parent
        parent = "" if parent_path == PurePosixPath(".") else parent_path.as_posix()
        if bare_year(directory.name) and parent:
            notes.append(
                {
                    "op": "note",
                    "path": rel,
                    "status": "year",
                    "detail": "a year-only folder belongs at the top and was left unchanged",
                }
            )
            continue
        parsed = parse_title(directory.name, rules)
        if parsed is None:
            continue
        if parsed.status != "rename" or not parsed.basename:
            notes.append(
                {
                    "op": "note",
                    "path": rel,
                    "status": parsed.status,
                    "detail": parsed.detail,
                }
            )
            continue
        target = parsed.basename if not parent or parent == "." else f"{parent}/{parsed.basename}"
        proposals.append((rel, target, parsed.detail))

    operations = list(notes)
    operations.extend(_collapse(root, proposals))
    return {
        "version": 1,
        "kind": "rename",
        "volume": volume,
        "operations": operations,
    }


def _collapse(root: Path, proposals: list[tuple[str, str, str]]) -> list[dict]:
    """Drop renames that would collide, and emit a note for each collision."""
    by_target: dict[str, list[tuple[str, str]]] = {}
    for current, target, detail in proposals:
        by_target.setdefault(target, []).append((current, detail))

    singles: list[tuple[str, str, str]] = []
    operations: list[dict] = []
    for target, group in sorted(by_target.items()):
        if len(group) > 1:
            for current, _detail in group:
                operations.append(
                    {
                        "op": "note",
                        "path": current,
                        "status": "collision",
                        "detail": f"more than one folder would become {target}",
                    }
                )
            continue
        current, detail = group[0]
        singles.append((current, target, detail))

    sources = {current for current, target, _detail in singles if current != target}
    for current, target, detail in singles:
        if current == target:
            continue
        destination = root.joinpath(*target.split("/"))
        if target in sources or destination.exists():
            operations.append(
                {
                    "op": "note",
                    "path": current,
                    "status": "collision",
                    "detail": f"{target} already exists",
                }
            )
            continue
        op = {
            "op": "rename_dir",
            "path": current,
            "to": target,
        }
        if detail:
            op["detail"] = detail
        operations.append(op)
    return operations


def _directories(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirnames, _filenames in os.walk(root, followlinks=False):
        dirpath = Path(current)
        kept: list[str] = []
        for name in dirnames:
            if name.casefold() == ".no-file-mess-quarantine":
                continue
            path = dirpath / name
            if path.is_symlink():
                continue
            kept.append(name)
            found.append(path)
        dirnames[:] = kept
    return found


@dataclass(frozen=True)
class _FoundDate:
    """Month and day taken from one title, plus the tokens those parts used."""

    month: int | None
    day: int | None
    used: frozenset[str]
    detail: str


def _month_and_day(tokens: list[str], year: str) -> _FoundDate:
    """Tell a month from a day without treating every 1–12 as a month.

    ``2026-08-02`` is year, month, day, because the year comes first. The
    same two numbers in ``08-02-2026`` stay ambiguous. A month name claims
    the month, so the remaining number is the day even when it is 12 or less.
    """
    named: list[str] = []
    numbers: list[tuple[int, str]] = []
    year_at = tokens.index(year)
    for index, token in enumerate(tokens):
        if token == year:
            continue
        if token.casefold() in _MONTHS:
            named.append(token)
        elif _is_day(token):
            numbers.append((index, token))

    if len(named) > 1:
        return _FoundDate(None, None, frozenset(), "more than one month")
    if named:
        if len(numbers) > 1:
            return _FoundDate(None, None, frozenset(), "more than one day")
        used = set(named)
        day = None
        if numbers:
            used.add(numbers[0][1])
            day = int(numbers[0][1])
        return _FoundDate(_MONTHS[named[0].casefold()], day, frozenset(used), "")

    if len(numbers) > 2:
        return _FoundDate(None, None, frozenset(), "more than one day")
    if len(numbers) == 2:
        first_at, first = numbers[0]
        second_at, second = numbers[1]
        if year_at < first_at and year_at < second_at:
            ordered = sorted(numbers)
            month_token, day_token = ordered[0][1], ordered[1][1]
            if _month_number(month_token) is None and _month_number(day_token) is not None:
                month_token, day_token = day_token, month_token
            if _month_number(month_token) is None or not _is_day(day_token):
                return _FoundDate(None, None, frozenset(), "more than one day")
            return _FoundDate(int(month_token), int(day_token), frozenset({first, second}), "")
        month_tokens = [token for _index, token in numbers if _month_number(token) is not None]
        day_tokens = [token for _index, token in numbers if _month_number(token) is None]
        if len(month_tokens) == 1 and len(day_tokens) == 1:
            return _FoundDate(
                int(month_tokens[0]),
                int(day_tokens[0]),
                frozenset(month_tokens + day_tokens),
                "",
            )
        return _FoundDate(None, None, frozenset(), "more than one month")
    if len(numbers) == 1:
        token = numbers[0][1]
        month = _month_number(token)
        if month is None:
            return _FoundDate(None, None, frozenset(), "")
        return _FoundDate(month, None, frozenset({token}), "")
    return _FoundDate(None, None, frozenset(), "")


def _date_tokens(title: str) -> list[str]:
    """Split ``title`` and expand a glued ``YYYYMM`` or ``YYYYMMDD`` token.

    A six-digit clock time beside a glued day is left out. ``202502``
    becomes ``2025`` and ``02``. ``IMG_20191225_092711`` becomes ``IMG``,
    ``2019``, ``12``, and ``25``.
    """
    raw = [token for token in _TOKEN_SPLIT.split(title.strip()) if token]
    has_day = _has_glued_day(raw)
    tokens: list[str] = []
    for token in raw:
        glued = _glued_date(token)
        if glued is not None:
            tokens.extend(glued)
        elif has_day and _is_clock(token):
            continue
        else:
            tokens.append(token)
    return tokens


def _has_glued_day(tokens: list[str]) -> bool:
    return any((glued := _glued_date(token)) is not None and len(glued) == 3 for token in tokens)


def _glued_date(token: str) -> tuple[str, ...] | None:
    """Year, month, and maybe day when ``token`` is only a compact date."""
    if len(token) == 8 and token.isdigit() and _is_year(token[:4]):
        try:
            found = date(int(token[:4]), int(token[4:6]), int(token[6:]))
        except ValueError:
            return None
        return f"{found.year:04d}", f"{found.month:02d}", f"{found.day:02d}"
    if len(token) == 6 and token.isdigit() and _is_year(token[:4]):
        month = int(token[4:])
        if 1 <= month <= 12:
            return token[:4], f"{month:02d}"
    return None


def _is_clock(token: str) -> bool:
    if len(token) != 6 or not token.isdigit():
        return False
    hour, minute, second = int(token[:2]), int(token[2:4]), int(token[4:])
    return hour <= 23 and minute <= 59 and second <= 59


def _is_year(token: str) -> bool:
    if len(token) != 4 or not token.isdigit():
        return False
    return _YEAR_MIN <= int(token) <= _YEAR_MAX


def _month_number(token: str) -> int | None:
    named = _MONTHS.get(token.casefold())
    if named is not None:
        return named
    if token.isdigit() and len(token) <= 2:
        value = int(token)
        if 1 <= value <= 12:
            return value
    return None


def _is_day(token: str) -> bool:
    if not token.isdigit() or len(token) > 2:
        return False
    return 1 <= int(token) <= 31


def _render(template: str, fields: dict[str, str]) -> str:
    """Fill ``template``, dropping empty placeholders and the separator before them.

    ``{year}-{month}-{day}-{name}`` with no day becomes ``2026-03-France``,
    and with neither month nor day becomes ``2018-Queen-of-Hearts``.
    """
    pieces: list[tuple[str, str]] = []
    position = 0
    for match in _PLACEHOLDER.finditer(template):
        if match.start() > position:
            pieces.append(("lit", template[position : match.start()]))
        key = match.group(1)
        if key not in fields:
            raise NoFileMessError(f"unknown folder placeholder: {{{key}}}")
        pieces.append(("field", fields[key]))
        position = match.end()
    if position < len(template):
        pieces.append(("lit", template[position:]))

    kept: list[str] = []
    for kind, value in pieces:
        if kind == "field" and value == "":
            if kept and _SEPARATOR.fullmatch(kept[-1]):
                kept.pop()
            continue
        kept.append(value)
    while kept and _SEPARATOR.fullmatch(kept[0]):
        kept.pop(0)
    while kept and _SEPARATOR.fullmatch(kept[-1]):
        kept.pop()
    return "".join(kept).strip()
