"""Copy plans for folders that differ between a source and a destination.

Comparison is by relative path. A path only in the source is new. The
same path with different bytes, or a different size when the files were
not hashed, is mismatched. A path only in the destination is extra.
A missing album is one choice. A folder that exists on both sides is
one choice per differing subfolder. Nothing is copied or deleted until
the review is confirmed. A diff folder stages the copies and records
the deletions so they can be carried to the destination.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

from no_file_mess.catalog import FileRecord
from no_file_mess.errors import NoFileMessError
from no_file_mess.paths import from_rel
from no_file_mess.plans import read_plan

_ACTIONS = {
    "s": "skip",
    "u": "update",
    "m": "merge",
    "r": "remove",
    "i": "identical",
}
_ACTION_KEY = {name: key for key, name in _ACTIONS.items()}
_CHANGES = {"add", "update", "extra"}
_POLICY = "sync"
_HELP = """\
s  skip this folder
u  add new files and overwrite every mismatched file
m  add new files, and leave files already there as they are
r  delete destination files that are not in the source
i  make this folder match the source: add, overwrite, and delete
q  stop; the next run continues from this folder
d  choose each subfolder separately
o  open the folder so its files can be inspected
!  use the default for this folder and every folder after it
h  show this help
Enter  copy files that are missing or older in the destination"""
_TIE_HELP = """\
u  overwrite the destination with the source
s  keep the destination file
o  open the folder so its files can be inspected
!  keep this file and every later file whose date does not say which is newer
q  stop; the next run continues from this folder
h  show this help
Enter  does not choose"""
_TIE_PROMPT = "[u] overwrite  [s] keep  [o] open  [!] rest  [q] stop  [h] help "


def plan_copy(
    source: list[FileRecord],
    destination: list[FileRecord],
    *,
    source_name: str,
    dest_name: str,
    action: str = "sync",
    interactive: bool = False,
    semi: bool = False,
    reader=None,
    writer=None,
    source_root: Path | None = None,
    dest_root: Path | None = None,
    progress_path: Path | None = None,
    prior_actions: dict[str, str] | None = None,
    resume_at: str | None = None,
    assignment: dict[str, str] | None = None,
    choices: dict[str, str] | None = None,
) -> dict:
    """Plan the path differences between ``source`` and ``destination``.

    Args:
        source: Files to copy from.
        destination: Files already stored.
        source_name: Label stored in the plan.
        dest_name: Label stored in the plan.
        action: Action for every out-of-sync folder. ``sync`` copies a file
            when the destination is missing it or has an older copy.
            With ``interactive``, this is what Enter selects.
        interactive: Ask once per differing folder. A letter acts immediately.
            ``d`` splits the folder into its subfolders. ``o`` opens it.
        semi: Ask only when both copies exist and the dates do not say
            which is newer. Every other file follows ``sync``.
        reader: Called with the prompt. Defaults to a single key.
        writer: Called with each line shown before the prompt.
        source_root: Directory ``o`` opens on the source side.
        dest_root: Directory ``o`` opens on the destination side.
        progress_path: Where ``q`` stores the listing and the choices so far.
        prior_actions: Choices already made, loaded from ``progress_path``.
        resume_at: Folder to ask about next. ``None`` starts at the first one.
        assignment: Saved folder for each changed path, including a ``d`` split.
        choices: Saved per-file decisions from a semi-interactive session.

    Returns:
        A versioned plan. ``add``, ``update``, and ``extra`` name the
        out-of-sync files. ``actions`` maps each top folder to the
        chosen action. The folder is the missing album, or the subfolder
        when the parent exists on both sides. A ``note`` means those
        bytes already exist at another destination path.

    Raises:
        NoFileMessError: The two sides have the same name, or the action
            is not one of the five folder actions.
    """
    if source_name == dest_name:
        raise NoFileMessError("the source and the destination must be different")
    if action not in _ACTION_KEY and action != _POLICY:
        raise NoFileMessError("action must be sync, skip, update, merge, remove, or identical")

    by_source = {row.rel_path: row for row in source}
    by_dest = {row.rel_path: row for row in destination}
    source_dirs = _directories(source)
    dest_dirs = _directories(destination)
    elsewhere: dict[str, list[str]] = {}
    for row in destination:
        if row.digest:
            elsewhere.setdefault(row.digest, []).append(row.rel_path)

    operations: list[dict] = []
    for rel in sorted(set(by_source) | set(by_dest)):
        src = by_source.get(rel)
        dst = by_dest.get(rel)
        folder = _unit(rel, source_dirs, dest_dirs)
        if src is not None and dst is not None and _unchanged(src, dst):
            continue
        if src is not None and dst is None:
            operations.append(_file_op("add", src, folder))
            others = [path for path in elsewhere.get(src.digest, []) if path != rel] if src.digest else []
            if others:
                operations.append(
                    {
                        "op": "note",
                        "path": rel,
                        "folder": folder,
                        "status": "present",
                        "detail": "same bytes already at " + ", ".join(sorted(others)),
                    }
                )
            continue
        if src is not None and dst is not None:
            updated = _file_op("update", src, folder)
            updated["dest_size"] = dst.size
            updated["dest_mtime"] = dst.mtime
            operations.append(updated)
            continue
        if dst is not None:
            operations.append(_file_op("extra", dst, folder))

    if assignment:
        for operation in operations:
            chosen = assignment.get(operation["path"])
            if operation["op"] in _CHANGES and isinstance(chosen, str):
                operation["folder"] = chosen
    if choices:
        for operation in operations:
            picked = choices.get(operation["path"])
            if operation["op"] in _CHANGES and picked in {"copy", "leave", "delete"}:
                operation["choice"] = picked

    folders = sorted({_stored_folder(op) for op in operations if op["op"] in _CHANGES})
    stopped_at = None
    keep = _keep_progress(
        progress_path,
        source_name,
        dest_name,
        source,
        destination,
        operations,
        "semi" if semi else "interactive",
    )
    if semi:
        actions, stopped_at = _ask_semi(
            operations,
            folders,
            reader,
            writer,
            source_root,
            dest_root,
            dict(prior_actions or {}),
            resume_at,
            keep,
        )
    elif interactive:
        actions, stopped_at = _ask_folders(
            operations,
            folders,
            action,
            reader,
            writer,
            source_root,
            dest_root,
            dict(prior_actions or {}),
            resume_at,
            keep,
        )
    else:
        actions = {folder: action for folder in folders}
    return {
        "version": 1,
        "kind": "copy",
        "source": source_name,
        "destination": dest_name,
        "actions": actions,
        "status": "quit" if stopped_at is not None else "ready",
        "resume_at": stopped_at,
        "operations": operations,
    }


def apply_copy(plan: dict, source_root: Path, output: Path, *, diff: bool, progress=None) -> dict:
    """Carry out the folder actions in ``plan``.

    ``merge`` adds paths that are absent. ``update`` also overwrites
    mismatched paths. ``remove`` deletes extras. ``identical`` does all
    three. ``skip`` does nothing. In a diff folder, copies are staged
    under ``files/`` and deletions are written into the manifest instead
    of being performed.

    Args:
        plan: Plan from :func:`plan_copy`.
        source_root: Directory the source paths are relative to.
        output: Destination directory, or the new diff directory.
        diff: When true, ``output`` is created and must not already exist.
        progress: Called with the bytes copied so far, the total bytes
            to copy, and the folder, after each file has been written.

    Returns:
        Counts under ``copied``, ``overwritten``, ``deleted``, and
        ``unreadable``, plus the paths that were not copied.
    """
    if plan.get("kind") != "copy":
        raise NoFileMessError("not a copy plan")
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise NoFileMessError(f"not a directory: {source_root}")
    if diff:
        if output.exists():
            raise NoFileMessError(f"diff directory already exists: {output}")
        output.mkdir(parents=True)
        dest_root = output / "files"
    else:
        output = output.resolve()
        if not output.is_dir():
            raise NoFileMessError(f"not a directory: {output}")
        dest_root = output

    copied: list[dict] = []
    overwritten: list[str] = []
    deleted: list[str] = []
    unreadable: list[str] = []
    total_bytes = sum(_copy_size(operation) for operation in plan["operations"] if _will_copy(plan, operation))
    copied_bytes = 0
    for operation in plan["operations"]:
        kind = operation.get("op")
        if kind == "copy":
            kind = "add"
        if kind not in _CHANGES:
            continue
        chosen = _action(plan, operation)
        if kind == "extra":
            if not _wants_delete(chosen, operation):
                continue
            if diff:
                deleted.append(operation["path"])
                continue
            if _delete(dest_root, operation["path"]):
                deleted.append(operation["path"])
            continue
        if not _wants_copy(chosen, operation):
            continue
        src = from_rel(source_root, operation["path"])
        dest = from_rel(dest_root, operation["path"])
        if not src.is_file() or src.stat().st_size != operation.get("size"):
            unreadable.append(operation["path"])
            continue
        if dest.is_symlink():
            unreadable.append(operation["path"])
            continue
        replacing = dest.is_file()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied_bytes += _copy_size(operation)
        if progress is not None:
            progress(copied_bytes, total_bytes, _copy_folder(operation))
        if replacing:
            overwritten.append(operation["path"])
        copied.append(
            {
                "path": operation["path"],
                "size": operation["size"],
                "algo": operation.get("algo", ""),
                "digest": operation["digest"],
                "change": "update" if replacing or kind == "update" else "add",
            }
        )

    result = {
        "copied": len(copied),
        "overwritten": len(overwritten),
        "deleted": len(deleted),
        "unreadable": len(unreadable),
        "files": copied,
        "deleted_paths": deleted,
        "unreadable_paths": unreadable,
    }
    if diff:
        manifest = {
            "source": plan.get("source", ""),
            "destination": plan.get("destination", ""),
            "file_count": len(copied),
            "total_bytes": sum(item["size"] for item in copied),
            "files": copied,
            "delete": deleted,
            "unreadable": unreadable,
        }
        (output / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    return result


def apply_diff(diff_dir: Path, dest_root: Path) -> dict:
    """Copy a carried diff folder onto ``dest_root`` and delete listed extras.

    Args:
        diff_dir: Directory produced by :func:`apply_copy` with ``diff`` set.
        dest_root: Existing destination tree.

    Returns:
        Counts under ``copied``, ``overwritten``, ``deleted``, and ``unreadable``.

    Raises:
        NoFileMessError: The manifest is missing or a path escapes the destination.
    """
    manifest_path = diff_dir / "MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NoFileMessError(f"cannot read diff manifest: {manifest_path}") from exc
    dest_root = dest_root.resolve()
    if not dest_root.is_dir():
        raise NoFileMessError(f"not a directory: {dest_root}")
    files_root = diff_dir / "files"
    copied = 0
    overwritten: list[str] = []
    deleted: list[str] = []
    unreadable: list[str] = []
    for item in manifest.get("files", []):
        src = from_rel(files_root, item["path"])
        dest = from_rel(dest_root, item["path"])
        if not src.is_file() or dest.is_symlink():
            unreadable.append(item["path"])
            continue
        replacing = dest.is_file()
        if replacing and item.get("change") != "update":
            unreadable.append(item["path"])
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
        if replacing:
            overwritten.append(item["path"])
    for rel in manifest.get("delete", []):
        if _delete(dest_root, rel):
            deleted.append(rel)
    return {
        "copied": copied,
        "overwritten": len(overwritten),
        "deleted": len(deleted),
        "unreadable": len(unreadable),
    }


def copy_lines(plan: dict, *, diff: bool = False) -> list[str]:
    """Describe what applying ``plan`` will change.

    Args:
        plan: Plan from :func:`plan_copy`.
        diff: Say that copies are staged and deletions are recorded.

    Returns:
        One line per folder, then one line per file that will change
        or be left as it is.
    """
    grouped: dict[str, list[dict]] = {}
    for operation in plan["operations"]:
        if operation.get("op") not in _CHANGES:
            continue
        grouped.setdefault(_stored_folder(operation), []).append(operation)
    lines: list[str] = []
    for folder in sorted(plan.get("actions", {}), key=lambda name: (name != "", name)):
        lines.append(f"{folder or '(root)'}  {plan['actions'][folder]}")
        by_verb: dict[str, list[str]] = {}
        for operation in grouped.get(folder, []):
            verb = _verb(operation, plan["actions"][folder], diff)
            by_verb.setdefault(verb, []).append(operation["path"])
        for verb, paths in by_verb.items():
            if len(paths) > 12:
                lines.append(f"  {verb} {len(paths)} files")
                continue
            for path in paths:
                lines.append(f"  {verb} {path}")
    return lines


def confirm_copy(plan: dict, reader, writer, *, diff: bool = False) -> bool:
    """Ask for an explicit ``y`` or ``n`` before copying or deleting.

    Enter does not accept the plan. A plan that would change nothing is
    refused without a question.

    Args:
        plan: Plan from :func:`plan_copy`.
        reader: Called with the confirmation prompt.
        writer: Called with each review line.
        diff: Review a diff folder rather than the destination itself.

    Returns:
        ``True`` only when the answer is ``y``.
    """
    lines = copy_lines(plan, diff=diff)
    if not any(line.startswith(("  add ", "  overwrite ", "  delete ", "  stage ", "  record ")) for line in lines):
        writer("nothing to copy or delete")
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


def load_progress(path: Path, source: str, destination: str) -> dict | None:
    """Load a saved interactive listing when it belongs to these two folders.

    Args:
        path: Progress file written beside the plan.
        source: Resolved source directory.
        destination: Resolved destination directory.

    Returns:
        The saved session, or ``None`` when there is nothing to continue.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    if data.get("source") != source or data.get("destination") != destination:
        return None
    if not isinstance(data.get("source_files"), list) or not isinstance(data.get("dest_files"), list):
        return None
    return data


def save_progress(
    path: Path,
    *,
    source: str,
    destination: str,
    source_files: list[FileRecord],
    dest_files: list[FileRecord],
    actions: dict[str, str],
    resume_at: str | None,
    operations: list[dict],
    mode: str = "interactive",
) -> None:
    """Store the listing and the choices made so far.

    Args:
        path: Destination. Parent directories are created.
        source: Resolved source directory.
        destination: Resolved destination directory.
        source_files: Source listing. Bytes are not included.
        dest_files: Destination listing.
        actions: Folder choices already made.
        resume_at: Folder to show next. ``None`` means the first folder.
        operations: Changed files, used to remember a ``d`` split.
        mode: ``interactive`` or ``semi``. A different mode reuses the listing only.
    """
    payload = {
        "version": 1,
        "source": source,
        "destination": destination,
        "actions": actions,
        "resume_at": resume_at,
        "mode": mode,
        "choices": {
            operation["path"]: operation["choice"]
            for operation in operations
            if operation.get("choice") in {"copy", "leave", "delete"}
        },
        "assignment": {
            operation["path"]: operation.get("folder", "")
            for operation in operations
            if operation.get("op") in _CHANGES
        },
        "source_files": [_compact(row) for row in source_files],
        "dest_files": [_compact(row) for row in dest_files],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def files_from_progress(rows: list[dict], volume: str) -> list[FileRecord]:
    """Turn a saved listing back into file records.

    Args:
        rows: ``source_files`` or ``dest_files`` from :func:`load_progress`.
        volume: Name stored on each record.

    Returns:
        Records with an empty digest. Comparison uses the file size.
    """
    return [
        FileRecord(volume, str(item["path"]), int(item["size"]), float(item["mtime"]), "", "")
        for item in rows
    ]


def _compact(row: FileRecord) -> dict:
    return {"path": row.rel_path, "size": row.size, "mtime": row.mtime}


def _keep_progress(path, source_name, dest_name, source, destination, operations, mode):
    """Return a saver, or ``None`` when this session is not being stored."""
    if path is None:
        return None

    def keep(actions: dict[str, str], resume_at: str | None) -> None:
        save_progress(
            path,
            source=source_name,
            destination=dest_name,
            source_files=source,
            dest_files=destination,
            actions=actions,
            resume_at=resume_at,
            operations=operations,
            mode=mode,
        )

    return keep


def load_copy_plan(path: Path) -> dict:
    """Read a copy plan.

    Args:
        path: JSON plan written by ``plan-copy``.

    Returns:
        The plan document.

    Raises:
        NoFileMessError: The file is not a copy plan.
    """
    plan = read_plan(path)
    if plan.get("kind") != "copy":
        raise NoFileMessError("not a copy plan")
    return plan


def _ask_folders(
    operations: list[dict],
    folders: list[str],
    action: str,
    reader,
    writer,
    source_root: Path | None,
    dest_root: Path | None,
    actions: dict[str, str],
    resume_at: str | None,
    keep,
) -> tuple[dict[str, str], str | None]:
    """Ask for one action per folder.

    Folders already present in ``actions`` are not asked again. ``q``
    leaves the current folder and every later folder unanswered.

    Returns:
        The choices, and the folder ``q`` stopped on. ``None`` means
        every folder was answered.
    """
    if reader is None:
        from no_file_mess.captured import read_key

        reader = read_key
    if writer is None:
        writer = print
    default_key = _ACTION_KEY.get(action, action)
    pending = [folder for folder in folders if folder not in actions]
    if resume_at in pending:
        start = pending.index(resume_at)
        pending = pending[start:] + pending[:start]
    accept_rest = False
    index = 0
    while index < len(pending):
        folder = pending[index]
        if accept_rest:
            actions[folder] = action
            index += 1
            continue
        if folder in actions and folder != resume_at:
            index += 1
            continue
        choice = _ask_folder(
            operations,
            folder,
            default_key,
            reader,
            writer,
            source_root,
            dest_root,
        )
        if choice == "d":
            children = _split_folder(operations, folder)
            if not children:
                writer("this folder has no subfolders to choose separately")
                continue
            pending[index : index + 1] = children
            continue
        if choice == "!":
            accept_rest = True
            choice = default_key
        if choice == "q":
            if keep is not None:
                keep(actions, folder)
            return actions, folder
        actions[folder] = _ACTIONS.get(choice, choice)
        index += 1
    if keep is not None:
        keep(actions, None)
    return actions, None


def _ask_semi(
    operations: list[dict],
    folders: list[str],
    reader,
    writer,
    source_root: Path | None,
    dest_root: Path | None,
    actions: dict[str, str],
    resume_at: str | None,
    keep,
) -> tuple[dict[str, str], str | None]:
    """Apply the default to clear files and ask about the rest.

    A file the destination is missing is copied. A file only in the
    destination is left there. A destination copy with an older date is
    overwritten, and one with a newer date is left. When the dates do
    not say which copy is newer, that file is asked about.

    Returns:
        The folder choices, and the folder ``q`` stopped on.
    """
    if reader is None:
        from no_file_mess.captured import read_key

        reader = read_key
    if writer is None:
        writer = print
    pending = [folder for folder in folders if folder not in actions]
    if resume_at in pending:
        start = pending.index(resume_at)
        pending = pending[start:] + pending[:start]
    accept_rest = False
    index = 0
    while index < len(pending):
        folder = pending[index]
        ambiguous = _ambiguous(operations, folder)
        if not ambiguous or accept_rest:
            if accept_rest:
                for operation in ambiguous:
                    operation["choice"] = "leave"
            actions[folder] = _POLICY
            index += 1
            continue
        _show_folder(operations, folder, writer, quiet=ambiguous)
        for operation in ambiguous:
            if accept_rest:
                operation["choice"] = "leave"
                continue
            decision = _ask_tie(operation, folder, reader, writer, source_root, dest_root)
            if decision == "quit":
                if keep is not None:
                    keep(actions, folder)
                return actions, folder
            if decision == "rest":
                accept_rest = True
                operation["choice"] = "leave"
                continue
            operation["choice"] = decision
        actions[folder] = _POLICY
        index += 1
    if keep is not None:
        keep(actions, None)
    return actions, None


def _ask_tie(operation, folder, reader, writer, source_root, dest_root) -> str:
    """Ask what to do with one file whose dates do not decide."""
    writer("    " + _file_line(operation, folder))
    while True:
        try:
            answer = reader(_TIE_PROMPT).strip().lower()
        except EOFError as exc:
            raise NoFileMessError("stopped before every file was answered") from exc
        if answer == "u":
            return "copy"
        if answer == "s":
            return "leave"
        if answer == "!":
            return "rest"
        if answer == "q":
            return "quit"
        if answer == "h":
            writer(_TIE_HELP)
            continue
        if answer == "o":
            opened = _reveal(source_root, folder) + _reveal(dest_root, folder)
            writer("opened " + ", ".join(opened) if opened else "folder is not on a mounted disk")
            continue
        writer("press u or s")


def _ask_folder(
    operations: list[dict],
    folder: str,
    default_key: str,
    reader,
    writer,
    source_root: Path | None,
    dest_root: Path | None,
) -> str:
    """Show one folder's files and read a key."""

    def show() -> None:
        _show_folder(operations, folder, writer)

    show()
    while True:
        try:
            answer = reader(_prompt(default_key)).strip().lower()
        except EOFError as exc:
            raise NoFileMessError("stopped before every folder was answered") from exc
        if answer == "":
            return default_key
        if answer == "h":
            writer(_HELP)
            show()
            continue
        if answer == "o":
            opened = _reveal(source_root, folder) + _reveal(dest_root, folder)
            writer("opened " + ", ".join(opened) if opened else "folder is not on a mounted disk")
            continue
        if answer in {"s", "u", "m", "r", "i", "q", "!", "d"}:
            return answer
        writer("choose s, u, m, r, i, d, o, q, !, or h")


def _show_folder(operations: list[dict], folder: str, writer, quiet: list | None = None) -> None:
    """Print each side of a folder under its own heading.

    ``quiet`` files are counted under their heading and printed by the
    question that follows, so a single tied file is not listed twice.
    """
    rows = [
        operation
        for operation in operations
        if operation.get("op") in _CHANGES and operation.get("folder") == folder
    ]
    writer(folder or "(root)")
    children: dict[str, int] = {}
    direct: list[dict] = []
    for operation in rows:
        rest = _rest(operation["path"], folder)
        if "/" in rest:
            children[rest.split("/", 1)[0]] = children.get(rest.split("/", 1)[0], 0) + 1
        else:
            direct.append(operation)
    for kind, label in (
        ("add", "only in source"),
        ("extra", "only in destination"),
        ("update", "mismatched"),
    ):
        group = [operation for operation in rows if operation["op"] == kind]
        if not group:
            continue
        writer(f"  {label}: {len(group)}")
        hidden = {id(operation) for operation in quiet or []}
        shown = direct if children else group
        shown = [
            operation
            for operation in shown
            if operation["op"] == kind and id(operation) not in hidden
        ]
        for operation in shown[:20]:
            writer("    " + _file_line(operation, folder))
        if len(shown) > 20:
            writer(f"    and {len(shown) - 20} more")
    if children:
        writer(f"  {len(children)} subfolders")
        for name, count in sorted(children.items(), key=lambda item: (-item[1], item[0]))[:20]:
            writer(f"    {name}  {count}")
        if len(children) > 20:
            writer(f"    and {len(children) - 20} more subfolders")
    for note in operations:
        if note.get("op") == "note" and note.get("folder") == folder:
            writer(f"  {note['path']}: {note['detail']}")


def _prompt(default_key: str) -> str:
    marked = "/".join(key.upper() if key == default_key else key for key in "s/u/m/r/i/q".split("/"))
    if default_key == _POLICY:
        return f"Enter: missing or older  [{marked}] [d] inside [o] open [!] rest [h] help "
    return f"[{marked}] [d] inside [o] open [!] rest [h] help "


def _file_line(operation: dict, folder: str) -> str:
    """Source on the left and destination on the right."""
    name = _rest(operation["path"], folder) or operation["path"]
    size = _format_size(int(operation.get("size") or 0))
    when = _format_mtime(operation.get("mtime"))
    if operation["op"] == "add":
        return f"{name}  {size}  {when} -> missing"
    if operation["op"] == "extra":
        return f"missing -> {name}  {size}  {when}"
    dest_size = _format_size(int(operation.get("dest_size") or 0))
    dest_when = _format_mtime(operation.get("dest_mtime"))
    return f"{name}  {size}  {when} -> {name}  {dest_size}  {dest_when}  {_relation_label(operation)}"


def _relation(operation: dict) -> str:
    """How the destination copy compares with the source.

    ``older`` and ``newer`` describe the destination. ``tie`` means the
    dates do not say which copy is newer.
    """
    src = operation.get("mtime")
    dst = operation.get("dest_mtime")
    if not isinstance(src, (int, float)) or not isinstance(dst, (int, float)):
        return "tie"
    if src <= 0 or dst <= 0:
        return "tie"
    if dst < src:
        return "older"
    if dst > src:
        return "newer"
    return "tie"


def _relation_label(operation: dict) -> str:
    relation = _relation(operation)
    if relation == "tie":
        return "not byte-identical"
    return relation


def _ambiguous(operations: list[dict], folder: str) -> list[dict]:
    return [
        operation
        for operation in operations
        if operation.get("op") == "update"
        and operation.get("folder") == folder
        and _relation(operation) == "tie"
        and operation.get("choice") not in {"copy", "leave", "delete"}
    ]


def _file_decision(operation: dict) -> str:
    """What ``sync`` does with one file."""
    explicit = operation.get("choice")
    if explicit in {"copy", "leave", "delete"}:
        return str(explicit)
    kind = operation.get("op")
    if kind == "add":
        return "copy"
    if kind == "extra":
        return "leave"
    if _relation(operation) == "older":
        return "copy"
    return "leave"


def _copy_size(operation: dict) -> int:
    size = operation.get("size")
    if isinstance(size, int) and size > 0:
        return size
    return 0


def _copy_folder(operation: dict) -> str:
    folder = operation.get("folder")
    if isinstance(folder, str) and folder:
        return folder
    path = str(operation.get("path") or "")
    if "/" not in path:
        return "(root)"
    return path.rsplit("/", 1)[0]


def _will_copy(plan: dict, operation: dict) -> bool:
    """Whether applying ``plan`` will write this file's bytes."""
    kind = operation.get("op")
    if kind == "copy":
        kind = "add"
    if kind not in {"add", "update"}:
        return False
    return _wants_copy(_action(plan, operation), operation)


def _will_transfer(plan: dict, operation: dict) -> bool:
    """Whether applying ``plan`` will copy or delete this operation."""
    kind = operation.get("op")
    if kind == "copy":
        kind = "add"
    if kind not in _CHANGES:
        return False
    chosen = _action(plan, operation)
    if kind == "extra":
        return _wants_delete(chosen, operation)
    return _wants_copy(chosen, operation)


def _wants_copy(action: str, operation: dict) -> bool:
    kind = operation.get("op")
    if action == _POLICY:
        return _file_decision(operation) == "copy"
    if kind == "add":
        return action in {"merge", "update", "identical"}
    if kind == "update":
        return action in {"update", "identical"}
    return False


def _wants_delete(action: str, operation: dict) -> bool:
    if action == _POLICY:
        return _file_decision(operation) == "delete"
    return action in {"remove", "identical"}


def _verb(operation: dict, action: str, diff: bool) -> str:
    kind = operation.get("op")
    if _wants_copy(action, operation):
        return "stage" if diff else ("overwrite" if kind == "update" else "add")
    if kind == "extra" and _wants_delete(action, operation):
        return "record delete" if diff else "delete"
    return "leave"


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MiB"
    return f"{size / 1024 / 1024 / 1024:.1f} GiB"


def _format_mtime(value: object) -> str:
    if not isinstance(value, (int, float)) or value <= 0:
        return "unknown date"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _file_op(kind: str, row: FileRecord, folder: str) -> dict:
    return {
        "op": kind,
        "path": row.rel_path,
        "folder": folder,
        "size": row.size,
        "mtime": row.mtime,
        "algo": row.algo,
        "digest": row.digest,
    }


def _unchanged(src: FileRecord, dst: FileRecord) -> bool:
    """Whether two records of the same path have the same contents.

    Hashes are compared when both sides have one. Otherwise the size is
    the signal, which is what a directory listing can know.
    """
    if src.digest and dst.digest:
        return src.digest == dst.digest
    return src.size == dst.size


def _directories(rows: list[FileRecord]) -> set[str]:
    found: set[str] = set()
    for row in rows:
        parent = str(PurePosixPath(row.rel_path).parent)
        while parent != ".":
            found.add(parent)
            if "/" not in parent:
                break
            parent = parent.rsplit("/", 1)[0]
    return found


def _unit(rel: str, source_dirs: set[str], dest_dirs: set[str]) -> str:
    """Folder a changed file belongs to.

    Ancestors that exist on both sides are skipped, so a missing album
    is one choice and a year present on both disks is one choice per
    differing subfolder.
    """
    chosen: list[str] = []
    for part in PurePosixPath(rel).parts[:-1]:
        chosen.append(part)
        prefix = "/".join(chosen)
        if prefix in source_dirs and prefix in dest_dirs:
            continue
        return prefix
    return "/".join(chosen)


def _stored_folder(operation: dict) -> str:
    folder = operation.get("folder")
    if isinstance(folder, str):
        return folder
    return _folder_of(operation["path"])


def _folder_of(rel: str) -> str:
    """Top folder of a relative path. Files at the root use an empty name."""
    if "/" not in rel:
        return ""
    return rel.split("/", 1)[0]


def _rest(path: str, folder: str) -> str:
    if not folder:
        return path
    prefix = folder + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _split_folder(operations: list[dict], folder: str) -> list[str]:
    """Move this folder's nested files into one choice per subfolder."""
    children: list[str] = []
    seen: set[str] = set()
    for operation in operations:
        if operation.get("op") not in _CHANGES or operation.get("folder") != folder:
            continue
        rest = _rest(operation["path"], folder)
        if "/" not in rest:
            continue
        child = f"{folder}/{rest.split('/', 1)[0]}" if folder else rest.split("/", 1)[0]
        operation["folder"] = child
        if child not in seen:
            seen.add(child)
            children.append(child)
    children.sort()
    if any(operation.get("op") in _CHANGES and operation.get("folder") == folder for operation in operations):
        children.append(folder)
    return children


def _reveal(root: Path | None, folder: str) -> list[str]:
    """Open ``folder`` under ``root`` in the file browser."""
    if root is None:
        return []
    path = root if not folder else root.joinpath(*folder.split("/"))
    if not path.exists():
        return []
    if sys.platform == "darwin":
        command = ["open", str(path)]
    elif sys.platform == "win32":
        command = ["explorer", str(path)]
    else:
        command = ["xdg-open", str(path)]
    subprocess.run(command, check=False)
    return [str(path)]


def _action(plan: dict, operation: dict) -> str:
    actions = plan.get("actions") or {}
    if not actions:
        return "merge"
    return str(actions.get(_stored_folder(operation), "skip"))


def _delete(root: Path, rel: str) -> bool:
    """Delete one file under ``root`` and remove directories left empty."""
    path = from_rel(root, rel)
    if path.is_symlink() or not path.is_file():
        return False
    resolved = path.resolve()
    if root not in resolved.parents:
        raise NoFileMessError(f"path escapes the destination: {rel}")
    path.unlink()
    parent = path.parent
    while parent != root and parent.is_dir():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return True
