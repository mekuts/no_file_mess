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
from no_file_mess.hashing import hash_file
from no_file_mess.paths import from_rel

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
        if not record.digest:
            continue
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


# Photos and videos. A resaved edit has a new hash, so it does not count as identical.
_MEDIA = {
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".png",
    ".webp",
    ".tif",
    ".tiff",
    ".gif",
    ".mov",
    ".mp4",
}


def plan_folder_duplicates(
    files: list[FileRecord],
    volume: str,
    *,
    min_shared: int = 10,
    min_ratio: float = 0.5,
    either: bool = False,
    aggregate: bool = False,
) -> dict:
    """List folder pairs that share many identical photos or videos.

    By default a folder counts only the files placed directly in it.
    ``aggregate`` also counts files in its subfolders, skips a folder
    against one inside it, and leaves out the same pair of subfolders.
    Edited copies and files deleted from one side simply do not match.
    A pair is reported when the number of shared hashes is at least
    ``min_shared`` and at least ``min_ratio`` of the smaller folder.

    Args:
        files: Every file on one volume.
        volume: Volume name copied into the plan.
        min_shared: Fewest identical media files required.
        min_ratio: Share of the smaller folder. With ``either``, more than
            this share qualifies on its own. Otherwise the pair must also
            reach ``min_shared``.
        either: Qualify when either threshold is met. The interactive
            merge uses this so a small folder can match on a quarter of
            its files, and any folder can match on ten identical files.
        aggregate: Count subfolders toward each parent. The default
            compares each folder on its own files.

    Returns:
        A versioned plan of ``note`` operations. It does not propose a merge.

    Raises:
        NoFileMessError: A threshold is outside its allowed range.
    """
    if min_shared < 1:
        raise NoFileMessError("minimum shared count must be at least 1")
    if not 0 < min_ratio <= 1:
        raise NoFileMessError("minimum ratio must be greater than 0 and at most 1")

    digests: dict[str, set[str]] = {}
    file_counts: dict[str, int] = {}
    newest: dict[str, dict[str, float]] = {}
    for record in files:
        parent = str(PurePosixPath(record.rel_path).parent)
        if parent == ".":
            continue
        directories = [parent]
        if aggregate:
            while "/" in parent:
                parent = parent.rsplit("/", 1)[0]
                directories.append(parent)
        for directory in directories:
            file_counts[directory] = file_counts.get(directory, 0) + 1
        if PurePosixPath(record.rel_path).suffix.casefold() not in _MEDIA:
            continue
        for directory in directories:
            identity = _content_id(record)
            digests.setdefault(directory, set()).add(identity)
            if record.digest:
                times = newest.setdefault(directory, {})
                times[record.digest] = max(times.get(record.digest, 0), record.mtime)

    owners: dict[str, list[str]] = {}
    for directory, owned in digests.items():
        for digest in owned:
            owners.setdefault(digest, []).append(directory)

    shared_counts: dict[tuple[str, str], int] = {}
    for directories in owners.values():
        ordered = sorted(set(directories))
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                if aggregate and _nested(left, right):
                    continue
                shared_counts[(left, right)] = shared_counts.get((left, right), 0) + 1

    qualifying: list[tuple[str, str, int]] = []
    for (left, right), shared in shared_counts.items():
        smaller = min(len(digests[left]), len(digests[right]))
        if not _qualifies(shared, smaller, min_shared, min_ratio, either):
            continue
        qualifying.append((left, right, shared))

    operations = []
    for left, right, shared in sorted(qualifying, key=lambda item: (-item[2], item[0], item[1])):
        if aggregate and any(
            _same_shape(left, right, other_left, other_right)
            for other_left, other_right, _shared in qualifying
            if (other_left, other_right) != (left, right)
        ):
            continue
        smaller = min(len(digests[left]), len(digests[right]))
        left_newer, right_newer, same_time = _newer_counts(newest.get(left, {}), newest.get(right, {}))
        operations.append(
            {
                "op": "note",
                "path": left,
                "other": right,
                "shared": shared,
                "left_images": len(digests[left]),
                "right_images": len(digests[right]),
                "left_files": file_counts.get(left, 0),
                "right_files": file_counts.get(right, 0),
                "left_newer": left_newer,
                "right_newer": right_newer,
                "same_time": same_time,
                "detail": f"{shared} identical images; the smaller folder has {smaller}",
            }
        )
    return {
        "version": 1,
        "kind": "folder-duplicates",
        "volume": volume,
        "operations": operations,
    }


_PAIR_PROMPT = "[1] keep 1  [2] keep 2  [b] both  [o] open  [x] stop  [h] help "
_OTHER_PROMPT = "[q] quarantine  [m] merge  [o] open  [x] stop  [h] help "
_PAIR_HELP = """1  keep the first folder
2  keep the second folder
b  leave both folders as they are
o  open both folders
x  stop. The same command continues here
h  explain these keys
Enter does not choose."""
_OTHER_HELP = """q  quarantine the folder you did not keep
m  move files that exist only there into the folder you kept, and quarantine the identical copies
o  open both folders
x  stop. The same command continues here
h  explain these keys
Enter does not choose."""


def _newer_counts(left: dict[str, float], right: dict[str, float]) -> tuple[int, int, int]:
    """Count identical files by which folder has the later modification time."""
    left_newer = right_newer = same_time = 0
    for digest, left_mtime in left.items():
        right_mtime = right.get(digest)
        if right_mtime is None:
            continue
        if left_mtime > right_mtime:
            left_newer += 1
        elif right_mtime > left_mtime:
            right_newer += 1
        else:
            same_time += 1
    return left_newer, right_newer, same_time


def _qualifies(shared: int, smaller: int, min_shared: int, min_ratio: float, either: bool) -> bool:
    """Return whether one folder pair is worth showing."""
    if smaller <= 0:
        return False
    enough = shared >= min_shared
    if either:
        return enough or shared / smaller > min_ratio
    return enough and shared / smaller >= min_ratio


def ask_folder_merges(
    pairs: list[dict],
    *,
    reader,
    writer,
    opener,
    prior: list[dict] | None = None,
    keep=None,
) -> tuple[list[dict], bool]:
    """Ask which folder to keep, then what to do with the other one.

    ``1`` and ``2`` choose the folder that stays. When both folders hold
    the same files, the other folder is quarantined without a second
    question. Otherwise ``q`` quarantines that whole folder and ``m``
    merges its unique files into the folder that stays. ``b`` leaves the
    pair alone. ``o`` opens both folders. ``x`` stops so the command can
    continue later. Enter does not choose.

    Args:
        pairs: Folder pairs from :func:`plan_folder_duplicates`.
        reader: Called with the prompt.
        writer: Called with each line shown before the prompt.
        opener: Called with the two folder paths. Returns paths it opened.
        prior: Choices already made, loaded from a progress file.
        keep: Called with the choices so far and whether every pair is done.

    Returns:
        The choices, and whether the session stopped early.

    Raises:
        NoFileMessError: Input ended before every pair was answered.
    """
    done = {(item["path"], item["other"]): item for item in prior or []}
    decisions: list[dict] = []
    for pair in pairs:
        key = (pair["path"], pair["other"])
        if key in done:
            decisions.append(done[key])
            continue
        choice = _ask_pair(pair, reader, writer, opener)
        if choice == "stop":
            if keep is not None:
                keep(decisions, False)
            return decisions, True
        if choice == "b":
            decision = {"path": pair["path"], "other": pair["other"], "choice": "b", "action": "both"}
        else:
            action = "quarantine" if _same_files(pair) else _ask_other(pair, choice, reader, writer, opener)
            if action == "stop":
                if keep is not None:
                    keep(decisions, False)
                return decisions, True
            decision = {
                "path": pair["path"],
                "other": pair["other"],
                "choice": choice,
                "action": action,
            }
        decisions.append(decision)
        if keep is not None:
            keep(decisions, False)
    if keep is not None:
        keep(decisions, True)
    return decisions, False


def folder_merge_plan(
    files: list[FileRecord],
    decisions: list[dict],
    volume: str,
    root: Path | None = None,
) -> dict:
    """Turn folder choices into quarantine and move operations.

    A file that already exists in the kept folder is quarantined. A file
    that exists only in the other folder is moved when the choice is
    ``merge``, keeping the path it had inside that folder. A different
    file already using the destination name is left in place and noted.

    Args:
        files: Every file on the volume.
        decisions: Choices from :func:`ask_folder_merges`.
        volume: Volume name copied into the plan.
        root: Where the volume is mounted, used to hash a file the
            catalog stored without a digest.

    Returns:
        A plan of kind ``folder-merge``.
    """
    operations: list[dict] = []
    for decision in decisions:
        if decision.get("action") == "both":
            continue
        keeper, loser = _kept_and_other(decision)
        keeper_ids = {_content_id(record) for record in _files_under(files, keeper)}
        keeper_by_rel = {
            record.rel_path[len(keeper) + 1 :]: record for record in _files_under(files, keeper)
        }
        for record in _files_under(files, loser):
            digest = _digest(record, root)
            relative = record.rel_path[len(loser) + 1 :]
            duplicate = _content_id(record) in keeper_ids
            if decision["action"] == "quarantine" or duplicate:
                if not digest:
                    operations.append(_unread_note(record.rel_path))
                    continue
                operations.append(
                    {
                        "op": "quarantine",
                        "path": record.rel_path,
                        "folder": loser,
                        "keep": keeper,
                        "digest": digest,
                        "detail": "identical copy" if duplicate else "quarantine folder",
                    }
                )
                continue
            taken = keeper_by_rel.get(relative)
            destination = f"{keeper}/{relative}"
            if taken is not None and taken.digest != digest:
                operations.append(
                    {
                        "op": "note",
                        "path": record.rel_path,
                        "detail": f"left in place; {destination} is a different file",
                    }
                )
                continue
            if not digest:
                operations.append(_unread_note(record.rel_path))
                continue
            operations.append(
                {
                    "op": "move",
                    "path": record.rel_path,
                    "to": destination,
                    "folder": loser,
                    "digest": digest,
                    "keep": keeper,
                    "detail": "merge",
                }
            )
    return {
        "version": 1,
        "kind": "folder-merge",
        "volume": volume,
        "status": "ready",
        "operations": operations,
    }


def merge_lines(plan: dict) -> list[str]:
    """Summarize a folder-merge plan for the review before apply."""
    quarantine: dict[str, int] = {}
    moved: dict[str, int] = {}
    keep_of: dict[str, str] = {}
    notes: list[str] = []
    for operation in plan.get("operations", []):
        if operation.get("op") == "note":
            notes.append(f"  leave {operation['path']}")
            continue
        folder = str(operation.get("folder") or operation["path"])
        keep_of[folder] = str(operation.get("keep", ""))
        if operation.get("op") == "quarantine":
            quarantine[folder] = quarantine.get(folder, 0) + 1
        elif operation.get("op") == "move":
            moved[folder] = moved.get(folder, 0) + 1
    lines = ["review"]
    for folder in sorted(set(quarantine) | set(moved)):
        copies = quarantine.get(folder, 0)
        shifts = moved.get(folder, 0)
        if shifts:
            lines.append(
                f"merge {folder} into {keep_of[folder]}  {shifts} moved, {copies} identical quarantined"
            )
        else:
            lines.append(f"quarantine {folder}  {copies} files")
    lines.extend(notes)
    return lines


def confirm_merge(plan: dict, reader, writer) -> bool:
    """Ask for an explicit ``y`` or ``n`` before merging or quarantining.

    Enter does not accept the plan. A plan that would change nothing is
    refused without a question.

    Args:
        plan: Plan from :func:`folder_merge_plan`.
        reader: Called with the confirmation prompt.
        writer: Called with each review line.

    Returns:
        ``True`` only when the answer is ``y``.

    Raises:
        NoFileMessError: Input ended before the plan was confirmed.
    """
    lines = merge_lines(plan)
    if len(lines) == 1:
        writer("nothing to merge or quarantine")
        return False
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


def _ask_pair(pair: dict, reader, writer, opener) -> str:
    _show_pair(pair, writer)
    while True:
        try:
            answer = reader(_PAIR_PROMPT).strip().lower()
        except EOFError as exc:
            raise NoFileMessError("stopped before every folder was answered") from exc
        if answer in {"1", "2", "b", "x"}:
            return "stop" if answer == "x" else answer
        if answer == "h":
            writer(_PAIR_HELP)
            continue
        if answer == "o":
            _open(pair, writer, opener)
            continue
        writer("press 1, 2, or b")


def _ask_other(pair: dict, choice: str, reader, writer, opener) -> str:
    _keeper, loser = _kept_and_other({"path": pair["path"], "other": pair["other"], "choice": choice})
    only = pair["left_files"] - pair["shared"] if choice == "2" else pair["right_files"] - pair["shared"]
    writer(f"other folder is {loser}  {only} files are only there")
    while True:
        try:
            answer = reader(_OTHER_PROMPT).strip().lower()
        except EOFError as exc:
            raise NoFileMessError("stopped before every folder was answered") from exc
        if answer == "x":
            return "stop"
        if answer in {"q", "m"}:
            return "quarantine" if answer == "q" else "merge"
        if answer == "h":
            writer(_OTHER_HELP)
            continue
        if answer == "o":
            _open(pair, writer, opener)
            continue
        writer("press q or m")


def _show_pair(pair: dict, writer) -> None:
    writer(f"1  {pair['path']}  {pair['left_files']} files, {pair['shared']} identical")
    writer(f"2  {pair['other']}  {pair['right_files']} files, {pair['shared']} identical")
    if "left_newer" in pair:
        writer(
            f"   1 newer: {pair['left_newer']}  "
            f"2 newer: {pair['right_newer']}  "
            f"same time: {pair['same_time']}"
        )


def _open(pair: dict, writer, opener) -> None:
    opened = opener(pair["path"], pair["other"])
    writer("opened " + ", ".join(opened) if opened else "folder is not on a mounted disk")


def _same_files(pair: dict) -> bool:
    """True when every file on each side is one of the shared copies."""
    return pair["shared"] == pair["left_files"] == pair["right_files"]


def _kept_and_other(decision: dict) -> tuple[str, str]:
    if decision["choice"] == "1":
        return decision["path"], decision["other"]
    return decision["other"], decision["path"]


def _files_under(files: list[FileRecord], folder: str) -> list[FileRecord]:
    prefix = folder + "/"
    return [record for record in files if record.rel_path.startswith(prefix)]


def _digest(record: FileRecord, root: Path | None) -> str:
    if record.digest:
        return record.digest
    if root is None:
        return ""
    try:
        return hash_file(from_rel(root, record.rel_path))
    except OSError:
        return ""


def _unread_note(path: str) -> dict:
    return {"op": "note", "path": path, "detail": "left in place; the file could not be read"}


def _content_id(record: FileRecord) -> str:
    """Hash, or a per-file token when the bytes were not read.

    A file of a unique size is not hashed. It still counts as one image
    in its folder, and the token never matches another file.
    """
    if record.digest:
        return record.digest
    return "unhashed:" + record.rel_path


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
        if not record.digest:
            continue
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


def _under(path: str, parent: str) -> bool:
    return path.startswith(parent + "/")


def _same_shape(left: str, right: str, other_left: str, other_right: str) -> bool:
    """Whether ``left``/``right`` sit inside another reported pair."""
    return (_under(left, other_left) and _under(right, other_right)) or (
        _under(left, other_right) and _under(right, other_left)
    )


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
