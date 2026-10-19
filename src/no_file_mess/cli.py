"""Command line for the catalog, plans, and bundle.

Library calls do the work. This module parses arguments, prints short
reports, and turns expected failures into a message and a non-zero status.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path

from no_file_mess import __version__
from no_file_mess.bundle import build_bundle
from no_file_mess.captured import confirm_changes, plan_dates, read_key
from no_file_mess.catalog import collect, connect, files_for_volume, inventory, list_volumes, scan, search
from no_file_mess.duplicates import (
    Labels,
    ask_folder_merges,
    confirm_merge,
    folder_merge_plan,
    plan_duplicates,
    plan_folder_duplicates,
)
from no_file_mess.errors import NoFileMessError
from no_file_mess.names import FolderRules, plan_renames
from no_file_mess.plans import apply_plan, read_plan, restore, write_plan
from no_file_mess.sync import (
    apply_copy,
    apply_diff,
    confirm_copy,
    files_from_progress,
    load_copy_plan,
    load_progress,
    plan_copy,
    save_progress,
)


def main(argv: list[str] | None = None) -> int:
    """Run one command.

    Args:
        argv: Arguments without the program name. Defaults to the process arguments.

    Returns:
        Process status. ``2`` means the command was refused. ``0`` means it finished.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except NoFileMessError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="no-file-mess",
        description="Catalog disks, plan identical-file cleanup and folder renames, and copy files a destination is missing.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    scan_cmd = commands.add_parser("scan", help="hash a directory into a named volume")
    _add_db(scan_cmd)
    scan_cmd.add_argument("--volume", required=True, help="stable name for this disk")
    scan_cmd.add_argument(
        "--duplicates",
        action="store_true",
        help="hash only files that share a size with another file",
    )
    scan_cmd.add_argument("root", type=Path, help="directory to walk")
    scan_cmd.set_defaults(func=_scan)

    volumes = commands.add_parser("volumes", help="list volumes in a catalog")
    _add_db(volumes)
    volumes.set_defaults(func=_volumes)

    search_cmd = commands.add_parser("search", help="find files by relative path")
    _add_db(search_cmd)
    search_cmd.add_argument("--volume", help="limit the search to one volume")
    search_cmd.add_argument(
        "--substring",
        action="store_true",
        help="match the query anywhere in the path",
    )
    search_cmd.add_argument("query")
    search_cmd.set_defaults(func=_search)

    folders = commands.add_parser(
        "plan-folder-dups",
        help="list folders that share many identical photos",
    )
    _add_db(folders, required=False)
    folders.add_argument("--volume", help="catalog volume to read")
    folders.add_argument("--root", type=Path, help="directory to hash instead of a volume")
    folders.add_argument(
        "--interactive",
        action="store_true",
        help="choose which folder to keep, review the plan, then apply on y",
    )
    folders.add_argument(
        "--refresh",
        action="store_true",
        help="ask again instead of continuing a saved session",
    )
    scope = folders.add_mutually_exclusive_group()
    scope.add_argument(
        "--leaf",
        action="store_true",
        help="compare files placed directly in each folder (the default)",
    )
    scope.add_argument(
        "--aggregate",
        action="store_true",
        help="count files in subfolders toward each parent folder",
    )
    folders.add_argument("--min-shared", type=int, default=10, help="fewest identical photos (default 10)")
    folders.add_argument(
        "--min-ratio",
        type=float,
        default=0.5,
        help="share of the smaller folder that must match (default 0.5)",
    )
    folders.add_argument("--out", type=Path, required=True)
    folders.set_defaults(func=_plan_folder_dups)

    dups = commands.add_parser("plan-dups", help="write a quarantine plan for identical files")
    _add_db(dups)
    dups.add_argument("--volume", required=True)
    dups.add_argument("--labels", type=Path, help="JSON curated and dump prefixes")
    dups.add_argument("--out", type=Path, required=True, help="plan file to write")
    dups.set_defaults(func=_plan_dups)

    names = commands.add_parser("plan-names", help="write a folder rename plan")
    names.add_argument("--root", type=Path, required=True)
    names.add_argument("--volume", help="name recorded in the plan")
    names.add_argument("--rules", type=Path, help="JSON folder template")
    names.add_argument("--out", type=Path, required=True)
    names.set_defaults(func=_plan_names)

    dates = commands.add_parser("plan-dates", help="propose folder months from capture dates")
    dates.add_argument("--root", type=Path, required=True)
    dates.add_argument("--volume", help="name recorded in the plan")
    dates.add_argument("--rules", type=Path, help="JSON folder template")
    dates.add_argument(
        "--slack",
        type=int,
        default=2,
        help="months after the earliest that still match that month (default 2)",
    )
    dates.add_argument(
        "--interactive",
        action="store_true",
        help="choose each folder, then confirm with y or n before any rename",
    )
    dates.add_argument("--out", type=Path, required=True)
    dates.set_defaults(func=_plan_dates)

    apply_cmd = commands.add_parser("apply", help="perform quarantine and rename operations")
    apply_cmd.add_argument("plan", type=Path)
    apply_cmd.add_argument("--root", type=Path, required=True)
    apply_cmd.set_defaults(func=_apply)

    restore_cmd = commands.add_parser("restore", help="undo the journal in a volume root")
    restore_cmd.add_argument("--root", type=Path, required=True)
    restore_cmd.set_defaults(func=_restore)

    missing = commands.add_parser("plan-copy", help="plan files that differ between two trees")
    _add_db(missing, required=False)
    missing.add_argument("--source", type=Path, help="source directory to list")
    missing.add_argument("--dest", type=Path, help="destination directory to list")
    missing.add_argument("--from-volume", help="catalog volume to copy from")
    missing.add_argument("--have", help="catalog volume that already has files")
    missing.add_argument(
        "--action",
        choices=["sync", "skip", "update", "merge", "remove", "identical"],
        help="action for every folder; with --interactive this is what Enter selects",
    )
    missing.add_argument(
        "--interactive",
        action="store_true",
        help="choose each differing folder, then confirm before copying",
    )
    missing.add_argument(
        "--semi-interactive",
        action="store_true",
        help="ask only when the dates do not say which copy is newer",
    )
    missing.add_argument(
        "--refresh",
        action="store_true",
        help="list both folders again instead of continuing a saved session",
    )
    missing.add_argument("--out", type=Path, required=True)
    missing.set_defaults(func=_plan_copy)

    copy_cmd = commands.add_parser("copy", help="copy files from a copy plan after confirmation")
    copy_cmd.add_argument("plan", nargs="?", type=Path)
    copy_cmd.add_argument("--source", type=Path, help="directory the plan paths come from")
    copy_cmd.add_argument("--from-diff", type=Path, help="diff folder to apply onto --dest")
    target = copy_cmd.add_mutually_exclusive_group()
    target.add_argument("--dest", type=Path, help="copy into this existing tree")
    target.add_argument("--diff", type=Path, help="stage copies in a new folder that can be carried")
    copy_cmd.set_defaults(func=_copy)

    bundle = commands.add_parser("bundle", help="copy files missing from another volume")
    _add_db(bundle)
    bundle.add_argument("--have", required=True, help="volume that already has some of the files")
    bundle.add_argument("--from-volume", required=True, help="volume to copy from")
    bundle.add_argument("--source", type=Path, required=True, help="current directory of --from-volume")
    bundle.add_argument("--out", type=Path, required=True, help="new bundle directory")
    bundle.set_defaults(func=_bundle)
    return parser


def _add_db(parser: argparse.ArgumentParser, required: bool = True) -> None:
    parser.add_argument("--db", type=Path, required=required, help="SQLite catalog path")


def _scan(args: argparse.Namespace) -> None:
    if args.duplicates:
        print("listing files", flush=True)
    with closing(connect(args.db)) as conn:
        stats = scan(
            conn,
            args.volume,
            args.root,
            shared_only=args.duplicates,
            progress=_copy_progress if args.duplicates else None,
        )
    if args.duplicates:
        print()
    hashed = f", hashed {stats.hashed}" if args.duplicates else ""
    print(
        f"scanned {args.volume}: {stats.files} files{hashed}, "
        f"skipped {stats.skipped_dataless} dataless, "
        f"{stats.skipped_special} special, {stats.skipped_noise} noise"
    )


def _volumes(args: argparse.Namespace) -> None:
    with closing(connect(args.db)) as conn:
        rows = list_volumes(conn)
    if not rows:
        print("no volumes")
        return
    for row in rows:
        print(f"{row['name']}\t{row['scanned_at']}\t{row['root']}")


def _search(args: argparse.Namespace) -> None:
    with closing(connect(args.db)) as conn:
        rows = search(conn, args.query, volume=args.volume, substring=args.substring)
    for row in rows:
        print(f"{row.volume}\t{row.rel_path}\t{row.size}\t{row.algo}:{row.digest}")
    if not rows:
        print("no matches")


def _plan_dups(args: argparse.Namespace) -> None:
    labels = Labels.from_path(args.labels)
    with closing(connect(args.db)) as conn:
        files = files_for_volume(conn, args.volume)
    plan = plan_duplicates(files, labels, args.volume)
    write_plan(args.out, plan)
    actions = sum(1 for op in plan["operations"] if op["op"] == "quarantine")
    print(f"wrote {args.out}: {actions} quarantine operations")


def _plan_names(args: argparse.Namespace) -> None:
    rules = FolderRules.from_path(args.rules)
    plan = plan_renames(args.root, rules, args.volume)
    write_plan(args.out, plan)
    renames = sum(1 for op in plan["operations"] if op["op"] == "rename_dir")
    notes = sum(1 for op in plan["operations"] if op["op"] == "note")
    print(f"wrote {args.out}: {renames} renames, {notes} notes")


def _plan_dates(args: argparse.Namespace) -> None:
    rules = FolderRules.from_path(args.rules)
    plan = plan_dates(args.root, rules, slack=args.slack, volume=args.volume, interactive=args.interactive)
    write_plan(args.out, plan)
    renames = sum(1 for op in plan["operations"] if op["op"] == "rename_dir")
    splits = sum(1 for op in plan["operations"] if op["op"] == "split_months")
    notes = sum(1 for op in plan["operations"] if op["op"] == "note")
    print(f"wrote {args.out}: {renames} renames, {splits} splits, {notes} notes")
    if not args.interactive or renames + splits == 0:
        return
    if confirm_changes(plan, read_key, print):
        done = apply_plan(plan, args.root)
        print(f"applied {done} operations")
        return
    print("left files unchanged")


def _apply(args: argparse.Namespace) -> None:
    plan = read_plan(args.plan)
    done = apply_plan(plan, args.root)
    print(f"applied {done} operations")


def _restore(args: argparse.Namespace) -> None:
    done = restore(args.root)
    print(f"restored {done} operations")


def _plan_copy(args: argparse.Namespace) -> None:
    if args.interactive and args.semi_interactive:
        raise NoFileMessError("use either --interactive or --semi-interactive")
    if args.semi_interactive and args.action:
        raise NoFileMessError("--semi-interactive decides clear files itself; leave out --action")
    progress = args.out.with_suffix(".progress.json")
    source_key = str(args.source.resolve()) if args.source is not None else None
    dest_key = str(args.dest.resolve()) if args.dest is not None else None
    asking = bool(args.interactive or args.semi_interactive)
    mode = "semi" if args.semi_interactive else "interactive"
    use_progress = bool(asking and source_key and dest_key)
    cached = None
    if use_progress and not args.refresh:
        cached = load_progress(progress, source_key, dest_key)
    prior = None
    resume_at = None
    assignment = None
    choices = None
    if cached is not None and cached.get("mode") == mode:
        _print_resume(cached)
        source_files = files_from_progress(cached["source_files"], "source")
        dest_files = files_from_progress(cached["dest_files"], "dest")
        source_name, dest_name = source_key, dest_key
        prior = cached.get("actions") if isinstance(cached.get("actions"), dict) else {}
        resume_at = cached.get("resume_at")
        assignment = cached.get("assignment") if isinstance(cached.get("assignment"), dict) else {}
        choices = cached.get("choices") if isinstance(cached.get("choices"), dict) else {}
    elif cached is not None:
        print("using the saved listing")
        source_files = files_from_progress(cached["source_files"], "source")
        dest_files = files_from_progress(cached["dest_files"], "dest")
        source_name, dest_name = source_key, dest_key
        assignment = cached.get("assignment") if isinstance(cached.get("assignment"), dict) else {}
    else:
        source_files, source_name, dest_files, dest_name = _copy_sides(args)
        if use_progress:
            source_name, dest_name = source_key, dest_key
            save_progress(
                progress,
                source=source_key,
                destination=dest_key,
                source_files=source_files,
                dest_files=dest_files,
                actions={},
                resume_at=None,
                operations=[],
                mode=mode,
            )
            print(f"saved the listing in {progress}")
    chosen = args.action or "sync"
    plan = plan_copy(
        source_files,
        dest_files,
        source_name=source_name,
        dest_name=dest_name,
        action=chosen,
        interactive=args.interactive,
        semi=args.semi_interactive,
        source_root=args.source,
        dest_root=args.dest,
        progress_path=progress if use_progress else None,
        prior_actions=prior,
        resume_at=resume_at if isinstance(resume_at, str) else None,
        assignment=assignment,
        choices=choices,
    )
    write_plan(args.out, plan)
    adds = sum(1 for op in plan["operations"] if op["op"] == "add")
    updates = sum(1 for op in plan["operations"] if op["op"] == "update")
    extras = sum(1 for op in plan["operations"] if op["op"] == "extra")
    print(f"wrote {args.out}: {adds} new, {updates} mismatched, {extras} extra")
    if plan.get("status") == "quit":
        shown = plan.get("resume_at") or "(root)"
        print(f"stopped at {shown}. Run the same command to continue.")
        return
    if not asking or args.source is None or args.dest is None or adds + updates + extras == 0:
        return
    if confirm_copy(plan, read_key, print):
        result = apply_copy(plan, args.source, args.dest, diff=False, progress=_copy_progress)
        print()
        _print_copy(result)
        progress.unlink(missing_ok=True)
        return
    print("left files unchanged")


def _print_resume(cached: dict) -> None:
    """Say whether this run is continuing a choice or only reusing the listing."""
    if isinstance(cached.get("resume_at"), str):
        print(f"continuing at {cached['resume_at'] or '(root)'}")
        return
    print("using the saved listing")


def _copy_sides(
    args: argparse.Namespace,
) -> tuple[list, str, list, str]:
    """Load the source and destination file lists from folders or volumes."""
    if (args.source is None) == (args.from_volume is None):
        raise NoFileMessError("give either --source or --from-volume")
    if (args.dest is None) == (args.have is None):
        raise NoFileMessError("give either --dest or --have")
    if (args.from_volume or args.have) and args.db is None:
        raise NoFileMessError("--db is required with a volume name")
    conn = connect(args.db) if args.db is not None and (args.from_volume or args.have) else None
    try:
        if args.source is not None:
            source_files, _stats = inventory(args.source)
            source_name = str(args.source)
        else:
            source_files = files_for_volume(conn, args.from_volume)
            source_name = args.from_volume
        if args.dest is not None:
            dest_files, _stats = inventory(args.dest)
            dest_name = str(args.dest)
        else:
            dest_files = files_for_volume(conn, args.have)
            dest_name = args.have
    finally:
        if conn is not None:
            conn.close()
    return source_files, source_name, dest_files, dest_name


def _copy(args: argparse.Namespace) -> None:
    if args.from_diff is not None:
        if args.dest is None:
            raise NoFileMessError("--from-diff needs --dest")
        manifest = _read_manifest(args.from_diff)
        if not _confirm_manifest(manifest, read_key, print):
            print("left files unchanged")
            return
        result = apply_diff(args.from_diff, args.dest)
        _print_copy(result)
        return
    if args.plan is None or args.source is None or (args.dest is None) == (args.diff is None):
        raise NoFileMessError("give a plan, --source, and either --dest or --diff")
    plan = load_copy_plan(args.plan)
    if plan.get("status") == "quit":
        raise NoFileMessError("this plan was stopped early; run plan-copy again to continue")
    if not confirm_copy(plan, read_key, print, diff=args.diff is not None):
        print("left files unchanged")
        return
    if args.diff is not None:
        result = apply_copy(plan, args.source, args.diff, diff=True, progress=_copy_progress)
    else:
        result = apply_copy(plan, args.source, args.dest, diff=False, progress=_copy_progress)
    print()
    _print_copy(result)


def _copy_progress(copied: int, total: int, folder: str) -> None:
    """Show one updating line: percent of bytes copied, and the folder."""
    percent = 100.0 if total <= 0 else min(100.0, copied * 100 / total)
    sys.stdout.write(f"\r{percent:5.1f}%  {folder or '(root)'}\033[K")
    sys.stdout.flush()


def _print_copy(result: dict) -> None:
    print(
        f"copied {result['copied']} files, "
        f"overwrote {result['overwritten']}, "
        f"deleted {result['deleted']}, "
        f"{result['unreadable']} unreadable"
    )
    for path in result.get("unreadable_paths", []):
        print(f"  unreadable {path}")


def _read_manifest(diff_dir: Path) -> dict:
    path = diff_dir / "MANIFEST.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NoFileMessError(f"cannot read diff manifest: {path}") from exc
    if not isinstance(data, dict):
        raise NoFileMessError("diff manifest must be a JSON object")
    return data


def _confirm_manifest(manifest: dict, reader, writer) -> bool:
    """Ask before applying a carried diff. Enter is not acceptance."""
    lines = ["from the diff folder"]
    for item in manifest.get("files", []):
        lines.append(f"  {item.get('change', 'add')} {item['path']}")
    for path in manifest.get("delete", []):
        lines.append(f"  delete {path}")
    if len(lines) == 1:
        writer("nothing to copy or delete")
        return False
    writer("review")
    for line in lines:
        writer(line)
    while True:
        answer = reader("apply these changes? [y/n] ").strip().lower()
        if answer in {"y", "n"}:
            return answer == "y"
        writer("press y or n")


def _plan_folder_dups(args: argparse.Namespace) -> None:
    if (args.root is None) == (args.volume is None):
        raise NoFileMessError("give either --root or --volume")
    if args.volume is not None and args.db is None:
        raise NoFileMessError("--db is required with --volume")
    root = _folder_root(args)
    if args.interactive and root is None:
        raise NoFileMessError("give --root so the folders can be opened and the plan applied")
    if args.root is not None and args.volume is None:
        print("listing files", flush=True)
        files, _stats = collect(args.root, shared_only=True, progress=_copy_progress)
        print()
        volume = str(args.root)
    else:
        with closing(connect(args.db)) as conn:
            files = files_for_volume(conn, args.volume)
        volume = args.volume
    if not args.interactive:
        plan = plan_folder_duplicates(
            files,
            volume,
            min_shared=args.min_shared,
            min_ratio=args.min_ratio,
            aggregate=args.aggregate,
        )
        write_plan(args.out, plan)
        print(f"wrote {args.out}: {len(plan['operations'])} folder pairs")
        return
    report = plan_folder_duplicates(
        files,
        volume,
        min_shared=10,
        min_ratio=0.25,
        either=True,
        aggregate=args.aggregate,
    )
    pairs = [operation for operation in report["operations"] if operation["op"] == "note"]
    progress = args.out.with_suffix(".progress.json")
    prior, finished = _load_merge_progress(progress, volume, root, args.refresh, args.aggregate)
    if finished:
        decisions = prior
        stopped = False
    else:

        def remember(decisions: list[dict], done: bool) -> None:
            _save_merge_progress(progress, volume, root, decisions, done, args.aggregate)

        decisions, stopped = ask_folder_merges(
            pairs,
            reader=read_key,
            writer=print,
            opener=lambda left, right: _open_folders(root, left, right),
            prior=prior,
            keep=remember,
        )
    plan = folder_merge_plan(files, decisions, volume, root)
    if stopped:
        plan["status"] = "quit"
        write_plan(args.out, plan)
        print("stopped. The same command continues at this folder")
        return
    write_plan(args.out, plan)
    moved = sum(1 for op in plan["operations"] if op["op"] == "move")
    quarantined = sum(1 for op in plan["operations"] if op["op"] == "quarantine")
    print(f"wrote {args.out}: {quarantined} quarantined, {moved} moved")
    if confirm_merge(plan, read_key, print):
        done = apply_plan(plan, root)
        progress.unlink(missing_ok=True)
        print(f"applied {done} operations")
        return
    print("left files unchanged")


def _folder_root(args: argparse.Namespace) -> Path | None:
    if args.root is not None:
        return args.root.resolve()
    if args.db is None or args.volume is None:
        return None
    with closing(connect(args.db)) as conn:
        for row in list_volumes(conn):
            if row["name"] == args.volume:
                return Path(row["root"])
    return None


def _open_folders(root: Path | None, left: str, right: str) -> list[str]:
    from no_file_mess.sync import _reveal

    return _reveal(root, left) + _reveal(root, right)


def _load_merge_progress(
    path: Path,
    volume: str,
    root: Path | None,
    refresh: bool,
    aggregate: bool,
) -> tuple[list[dict], bool]:
    if refresh or root is None or not path.is_file():
        return [], False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], False
    if not isinstance(data, dict) or data.get("version") != 1:
        return [], False
    if data.get("volume") != volume or data.get("root") != str(root):
        return [], False
    # A session saved before this flag rolled every subfolder into its parent.
    saved_aggregate = True if data.get("aggregate") is None else bool(data.get("aggregate"))
    if saved_aggregate != aggregate:
        return [], False
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        return [], False
    return decisions, bool(data.get("done"))


def _save_merge_progress(
    path: Path,
    volume: str,
    root: Path | None,
    decisions: list[dict],
    done: bool,
    aggregate: bool,
) -> None:
    if root is None:
        return
    payload = {
        "version": 1,
        "volume": volume,
        "root": str(root),
        "aggregate": aggregate,
        "decisions": decisions,
        "done": done,
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _bundle(args: argparse.Namespace) -> None:
    with closing(connect(args.db)) as conn:
        manifest = build_bundle(
            conn,
            have=args.have,
            source_volume=args.from_volume,
            source_root=args.source,
            output=args.out,
        )
    print(
        f"wrote {args.out}: {manifest['file_count']} files, "
        f"{manifest['total_bytes']} bytes, "
        f"{len(manifest['unreadable'])} unreadable"
    )
