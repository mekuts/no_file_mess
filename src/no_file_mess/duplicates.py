"""Suggested keepers for byte-identical files on one volume.

The suggestion is a sort, not a deletion. A copy that sits in a smaller
directory whose hashes all appear in some other directory is preferred:
that smaller directory looks like a pruned selection. Labels and a few
well-known camera-dump names are the next signal. Newer modification
time is used only after those. The plan quarantines the other copies
when it is applied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from no_file_mess.catalog import FileRecord
from no_file_mess.errors import NoFileMessError

# Path components that usually hold an unedited camera or chat dump.
_BUILTIN_DUMPS = {"dcim", "whatsapp", "whatsapp images", "screenshots"}


@dataclass(frozen=True)
class Labels:
    """Path prefixes that bias which copy is kept.

    Attributes:
        curated: Relative prefixes whose copies should win.
        dump: Extra dump prefixes, added to the built-in names.
    """

    curated: tuple[str, ...] = ()
    dump: tuple[str, ...] = ()

    @classmethod
    def from_path(cls, path: Path | None) -> "Labels":
        """Load labels from JSON, or return empty labels.

        Args:
            path: Labels file. ``None`` selects the built-in dump names only.

        Returns:
            Parsed labels.

        Raises:
            NoFileMessError: The file cannot be read or is the wrong shape.
        """
        if path is None:
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise NoFileMessError(f"cannot read labels: {path}") from exc
        except json.JSONDecodeError as exc:
            raise NoFileMessError(f"labels are not valid JSON: {path}") from exc
        if not isinstance(data, dict):
            raise NoFileMessError("labels must be a JSON object")
        return cls(
            curated=_prefix_tuple(data.get("curated", [])),
            dump=_prefix_tuple(data.get("dump", [])),
        )


def plan_duplicates(files: list[FileRecord], labels: Labels, volume: str) -> dict:
    """Build a quarantine plan for identical files.

    Args:
        files: Every file on one volume.
        labels: Optional curated and dump prefixes.
        volume: Volume name copied into the plan.

    Returns:
        A versioned plan. One ``quarantine`` operation per copy that is
        not the suggested keeper. Unique files produce no operations.
    """
    groups: dict[str, list[FileRecord]] = {}
    for record in files:
        groups.setdefault(record.digest, []).append(record)

    subset_dirs = _subset_directories(files)
    operations: list[dict] = []
    for digest, group in sorted(groups.items()):
        if len(group) < 2:
            continue
        ranked = sorted(group, key=lambda rec: _rank(rec, subset_dirs, labels))
        keeper = ranked[0]
        reason = _reason(keeper, ranked[1:], subset_dirs, labels)
        for loser in ranked[1:]:
            operations.append(
                {
                    "op": "quarantine",
                    "path": loser.rel_path,
                    "keep": keeper.rel_path,
                    "digest": digest,
                    "detail": reason,
                }
            )
    return {
        "version": 1,
        "kind": "duplicates",
        "volume": volume,
        "operations": operations,
    }


def _rank(record: FileRecord, subset_dirs: set[str], labels: Labels) -> tuple:
    """Sort key. Earlier fields are the stronger signals."""
    return (
        0 if _inside(record.rel_path, subset_dirs) else 1,
        0 if _has_prefix(record.rel_path, labels.curated) else 1,
        1 if _is_dump(record.rel_path, labels) else 0,
        -record.mtime,
        len(record.rel_path),
        record.rel_path,
    )


def _reason(
    keeper: FileRecord,
    losers: list[FileRecord],
    subset_dirs: set[str],
    labels: Labels,
) -> str:
    """Explain the first signal that actually separates the keeper."""
    if _inside(keeper.rel_path, subset_dirs) and any(
        not _inside(loser.rel_path, subset_dirs) for loser in losers
    ):
        return "keeper is in a directory whose files are a subset of another directory"
    if _has_prefix(keeper.rel_path, labels.curated) and any(
        not _has_prefix(loser.rel_path, labels.curated) for loser in losers
    ):
        return "keeper is under a curated prefix"
    if not _is_dump(keeper.rel_path, labels) and any(
        _is_dump(loser.rel_path, labels) for loser in losers
    ):
        return "keeper is outside a dump directory"
    if all(keeper.mtime > loser.mtime for loser in losers):
        return "keeper has the newer modification time"
    if all(len(keeper.rel_path) < len(loser.rel_path) for loser in losers):
        return "keeper has the shortest path"
    return "keeper path sorts first when the other signals tie"


def _subset_directories(files: list[FileRecord]) -> set[str]:
    """Directories whose hashes are a proper subset of some other directory.

    A directory inside another is not a useful comparison: its files are
    a subset of its parent by construction. Comparisons stay between
    directories that do not contain each other. Candidates are limited
    to directories that share the least common hash, so this does not
    compare every pair.
    """
    digests: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for record in files:
        parent = str(PurePosixPath(record.rel_path).parent)
        ancestors: list[str] = []
        if parent != ".":
            ancestors.append(parent)
            while "/" in parent:
                parent = parent.rsplit("/", 1)[0]
                ancestors.append(parent)
        for directory in ancestors:
            digests.setdefault(directory, set()).add(record.digest)
            counts[directory] = counts.get(directory, 0) + 1

    owners: dict[str, list[str]] = {}
    for directory, owned in digests.items():
        for digest in owned:
            owners.setdefault(digest, []).append(directory)

    found: set[str] = set()
    for directory, owned in digests.items():
        if not owned:
            continue
        rare = min(owned, key=lambda digest: len(owners[digest]))
        for other in owners[rare]:
            if other == directory or _nested(directory, other):
                continue
            if counts[other] > counts[directory] and owned <= digests[other]:
                found.add(directory)
                break
    return found


def _nested(left: str, right: str) -> bool:
    return left.startswith(right + "/") or right.startswith(left + "/")


def _inside(rel_path: str, directories: set[str]) -> bool:
    parent = str(PurePosixPath(rel_path).parent)
    while parent != ".":
        if parent in directories:
            return True
        if "/" not in parent:
            return False
        parent = parent.rsplit("/", 1)[0]
    return False


def _has_prefix(rel_path: str, prefixes: tuple[str, ...]) -> bool:
    folded = rel_path.casefold()
    for prefix in prefixes:
        if folded == prefix or folded.startswith(prefix + "/"):
            return True
    return False


def _is_dump(rel_path: str, labels: Labels) -> bool:
    components = {part.casefold() for part in PurePosixPath(rel_path).parts[:-1]}
    if components & _BUILTIN_DUMPS:
        return True
    return _has_prefix(rel_path, labels.dump)


def _prefix_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise NoFileMessError("label lists must contain strings")
    cleaned = tuple(item.strip("/").replace("\\", "/").casefold() for item in value if item.strip())
    return cleaned
