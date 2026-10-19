"""Editable plans and the only commands that move files.

``note`` operations are comments for the reader. ``apply`` does not act
on them. Quarantine moves a file under a folder in the volume root.
Rename changes a directory name. ``split_months`` moves files into one
directory per capture month. None of these commands unlink a file. The
journal can put the moves back. An empty source directory left by a
split is removed and recreated on restore.
"""

import json
import shutil
from pathlib import Path, PurePosixPath

from no_file_mess.errors import NoFileMessError
from no_file_mess.hashing import hash_file
from no_file_mess.paths import to_rel

_QUARANTINE = ".no-file-mess-quarantine"
_JOURNAL = "journal.jsonl"
_KNOWN = {"note", "rename_dir", "quarantine", "split_months", "copy", "add", "update", "extra", "move"}


def write_plan(path: Path, plan: dict) -> None:
    """Write ``plan`` as UTF-8 JSON.

    Args:
        path: Destination. Parent directories are created.
        plan: Plan document from a planner.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")


def read_plan(path: Path) -> dict:
    """Read a plan file.

    Args:
        path: JSON plan.

    Returns:
        The plan document.

    Raises:
        NoFileMessError: The file is unreadable, not a plan, or an unknown version.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise NoFileMessError(f"cannot read plan: {path}") from exc
    except json.JSONDecodeError as exc:
        raise NoFileMessError(f"plan is not valid JSON: {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("operations"), list):
        raise NoFileMessError("plan must be an object with an operations list")
    if data.get("version") != 1:
        raise NoFileMessError("unsupported plan version")
    for operation in data["operations"]:
        if not isinstance(operation, dict) or operation.get("op") not in _KNOWN:
            raise NoFileMessError("plan contains an unknown operation")
        if operation.get("op") == "split_months":
            _require_moves(operation)
        if operation.get("op") in {"copy", "add", "update", "extra"} and not isinstance(
            operation.get("path"), str
        ):
            raise NoFileMessError("copy operation is missing a path")
        if operation.get("op") in {"copy", "add", "update"} and not isinstance(operation.get("digest"), str):
            raise NoFileMessError("copy operation is missing a digest")
        if operation.get("op") == "move":
            _require_text(operation, "path")
            _require_text(operation, "to")
            _require_text(operation, "digest")
    return data


def apply_plan(plan: dict, root: Path) -> int:
    """Move files and directories described by ``plan``.

    Quarantines run before renames, and deeper directories are renamed
    before their parents, so each path in the plan still exists when it
    is used. A quarantine whose bytes no longer match the plan is refused
    before that file is moved.

    Args:
        plan: Plan document.
        root: Current location of the volume.

    Returns:
        How many operations changed the tree. Notes count as zero.

    Raises:
        NoFileMessError: A path escapes ``root`` or the tree is not as the plan expects.
    """
    root = root.resolve()
    if not root.is_dir():
        raise NoFileMessError(f"not a directory: {root}")
    if plan.get("status") == "quit":
        raise NoFileMessError("this plan was stopped before every folder was answered")
    if plan.get("kind") == "copy":
        raise NoFileMessError("a copy plan is applied with the copy command")
    if plan.get("kind") == "folder-duplicates":
        raise NoFileMessError("a folder duplicate plan is a report and is not applied")
    quarantines = [op for op in plan["operations"] if op["op"] == "quarantine"]
    moves = [op for op in plan["operations"] if op["op"] == "move"]
    splits = [op for op in plan["operations"] if op["op"] == "split_months"]
    renames = [op for op in plan["operations"] if op["op"] == "rename_dir"]
    renames.sort(key=lambda op: op["path"].count("/"), reverse=True)

    journal = _journal_path(root)
    done = 0
    touched: list[str] = []
    for operation in quarantines:
        _apply_quarantine(root, operation, journal)
        touched.append(operation["path"])
        done += 1
    for operation in moves:
        _apply_move(root, operation, journal)
        touched.append(operation["path"])
        done += 1
    _remove_empty(root, touched, journal)
    for operation in splits:
        _apply_split(root, operation, journal)
        done += 1
    for operation in renames:
        _apply_rename(root, operation, journal)
        done += 1
    return done


def restore(root: Path) -> int:
    """Undo journaled moves, last operation first.

    Args:
        root: Volume root that contains the quarantine folder.

    Returns:
        How many journal entries were undone.

    Raises:
        NoFileMessError: There is no journal, or a file is no longer where the journal says.
    """
    root = root.resolve()
    journal = _journal_path(root)
    if not journal.is_file():
        raise NoFileMessError("nothing to restore")
    entries = [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for entry in reversed(entries):
        if entry.get("op") == "rmdir":
            _inside(root, entry["src"]).mkdir(parents=True, exist_ok=True)
            continue
        source = _inside(root, entry["src"])
        dest = _inside(root, entry["dest"])
        if not dest.exists():
            raise NoFileMessError(f"cannot restore, missing {entry['dest']}")
        if source.exists():
            raise NoFileMessError(f"cannot restore, {entry['src']} already exists")
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dest), str(source))
        if entry.get("op") == "split_months":
            parent = dest.parent
            if parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
    journal.unlink()
    return len(entries)


def _apply_quarantine(root: Path, operation: dict, journal: Path) -> None:
    rel = _require_text(operation, "path")
    digest = _require_text(operation, "digest")
    source = _inside(root, rel)
    if not source.is_file():
        raise NoFileMessError(f"file from the plan is missing: {rel}")
    if hash_file(source) != digest:
        raise NoFileMessError(f"file changed since the plan was written: {rel}")
    dest_rel = _unused(_QUARANTINE + "/files/" + rel, root)
    dest = _inside(root, dest_rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    _append_journal(journal, {"op": "quarantine", "src": rel, "dest": dest_rel})


def _apply_move(root: Path, operation: dict, journal: Path) -> None:
    src_rel = _require_text(operation, "path")
    dest_rel = _require_text(operation, "to")
    digest = _require_text(operation, "digest")
    source = _inside(root, src_rel)
    dest = _inside(root, dest_rel)
    if not source.is_file():
        raise NoFileMessError(f"file from the plan is missing: {src_rel}")
    if hash_file(source) != digest:
        raise NoFileMessError(f"file changed since the plan was written: {src_rel}")
    if dest.exists():
        raise NoFileMessError(f"merge destination already exists: {dest_rel}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    _append_journal(journal, {"op": "move", "src": src_rel, "dest": dest_rel})


def _remove_empty(root: Path, files: list[str], journal: Path) -> None:
    """Remove directories left empty by quarantines and merges."""
    parents: set[str] = set()
    for rel in files:
        parent = rel
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            parents.add(parent)
    for rel in sorted(parents, key=lambda item: item.count("/"), reverse=True):
        directory = _inside(root, rel)
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
            _append_journal(journal, {"op": "rmdir", "src": rel})


def _apply_split(root: Path, operation: dict, journal: Path) -> None:
    moves = _require_moves(operation)
    staged = []
    for move in moves:
        source = _inside(root, move["path"])
        dest = _inside(root, move["to"])
        if not source.is_file():
            raise NoFileMessError(f"file from the plan is missing: {move['path']}")
        if dest.exists():
            raise NoFileMessError(f"split destination already exists: {move['to']}")
        staged.append((source, dest, move["path"], move["to"]))
    emptied: list[Path] = []
    for source, dest, src_rel, dest_rel in staged:
        dest.parent.mkdir(parents=True, exist_ok=True)
        origin = source.parent
        shutil.move(str(source), str(dest))
        _append_journal(journal, {"op": "split_months", "src": src_rel, "dest": dest_rel})
        if origin != root and origin not in emptied and not any(origin.iterdir()):
            emptied.append(origin)
    for directory in emptied:
        rel = to_rel(root, directory)
        directory.rmdir()
        _append_journal(journal, {"op": "rmdir", "src": rel})


def _require_moves(operation: dict) -> list[dict]:
    moves = operation.get("moves")
    if not isinstance(moves, list) or not moves:
        raise NoFileMessError("split is missing moves")
    for move in moves:
        if not isinstance(move, dict):
            raise NoFileMessError("split move is not an object")
        _require_text(move, "path")
        _require_text(move, "to")
    return moves


def _apply_rename(root: Path, operation: dict, journal: Path) -> None:
    src_rel = _require_text(operation, "path")
    dest_rel = _require_text(operation, "to")
    source = _inside(root, src_rel)
    dest = _inside(root, dest_rel)
    if not source.is_dir():
        raise NoFileMessError(f"directory from the plan is missing: {src_rel}")
    if dest.exists():
        raise NoFileMessError(f"rename destination already exists: {dest_rel}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    source.rename(dest)
    _append_journal(journal, {"op": "rename_dir", "src": src_rel, "dest": dest_rel})


def _journal_path(root: Path) -> Path:
    return root / _QUARANTINE / _JOURNAL


def _append_journal(journal: Path, entry: dict) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _require_text(operation: dict, key: str) -> str:
    value = operation.get(key)
    if not isinstance(value, str) or not value:
        raise NoFileMessError(f"operation is missing {key}")
    return value


def _unused(rel: str, root: Path) -> str:
    """Return ``rel``, or ``rel`` with a numeric suffix if that path is taken."""
    if not _inside(root, rel).exists():
        return rel
    path = PurePosixPath(rel)
    for number in range(1, 1000):
        candidate = str(path.with_name(f"{path.name}.{number}"))
        if not _inside(root, candidate).exists():
            return candidate
    raise NoFileMessError(f"no free quarantine name for {rel}")


def _inside(root: Path, rel: str) -> Path:
    """Join ``rel`` onto ``root`` and refuse paths that climb out."""
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or "\\" in rel:
        raise NoFileMessError(f"path escapes the volume: {rel}")
    path = root.joinpath(*pure.parts)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise NoFileMessError(f"path escapes the volume: {rel}") from exc
    return path
