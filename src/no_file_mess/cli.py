"""Command line for the catalog, plans, and bundle.

Library calls do the work. This module parses arguments, prints short
reports, and turns expected failures into a message and a non-zero status.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from pathlib import Path

from no_file_mess import __version__
from no_file_mess.bundle import build_bundle
from no_file_mess.captured import confirm_changes, plan_dates, read_key
from no_file_mess.catalog import connect, files_for_volume, list_volumes, scan, search
from no_file_mess.duplicates import Labels, plan_duplicates
from no_file_mess.errors import NoFileMessError
from no_file_mess.names import FolderRules, plan_renames
from no_file_mess.plans import apply_plan, read_plan, restore, write_plan


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
        description="Catalog disks, plan identical-file cleanup and folder renames, and copy files another catalog is missing.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    scan_cmd = commands.add_parser("scan", help="hash a directory into a named volume")
    _add_db(scan_cmd)
    scan_cmd.add_argument("--volume", required=True, help="stable name for this disk")
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

    bundle = commands.add_parser("bundle", help="copy files missing from another volume")
    _add_db(bundle)
    bundle.add_argument("--have", required=True, help="volume that already has some of the files")
    bundle.add_argument("--from-volume", required=True, help="volume to copy from")
    bundle.add_argument("--source", type=Path, required=True, help="current directory of --from-volume")
    bundle.add_argument("--out", type=Path, required=True, help="new bundle directory")
    bundle.set_defaults(func=_bundle)
    return parser


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, required=True, help="SQLite catalog path")


def _scan(args: argparse.Namespace) -> None:
    with closing(connect(args.db)) as conn:
        stats = scan(conn, args.volume, args.root)
    print(
        f"scanned {args.volume}: {stats.files} files, "
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
