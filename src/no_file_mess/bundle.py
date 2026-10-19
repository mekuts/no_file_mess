"""Copy files that one catalog has and another catalog lacks.

The copy is a folder of files plus a manifest. Paths that already share
a hash are described in the manifest and are not copied again. The
source tree is only read.
"""

import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

from no_file_mess.catalog import FileRecord, files_for_volume
from no_file_mess.errors import NoFileMessError
from no_file_mess.paths import from_rel


def build_bundle(
    conn: sqlite3.Connection,
    *,
    have: str,
    source_volume: str,
    source_root: Path,
    output: Path,
) -> dict:
    """Write the files ``source_volume`` has that ``have`` does not.

    Args:
        conn: Catalog containing both volumes.
        have: Volume that will not be mounted for this copy.
        source_volume: Volume whose disk is available at ``source_root``.
        source_root: Current mount of ``source_volume``.
        output: Directory to create. It must not already exist.

    Returns:
        The manifest that was written under ``output``.
    """
    if have == source_volume:
        raise NoFileMessError("the source volume and the other volume must be different")
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise NoFileMessError(f"not a directory: {source_root}")
    if output.exists():
        raise NoFileMessError(f"bundle directory already exists: {output}")

    source_rows = files_for_volume(conn, source_volume)
    have_rows = files_for_volume(conn, have)
    have_paths = _paths_by_digest(have_rows)
    source_groups: dict[str, list[FileRecord]] = {}
    missing: list[FileRecord] = []
    for row in source_rows:
        if not row.digest:
            missing.append(row)
            continue
        source_groups.setdefault(row.digest, []).append(row)

    same_bytes: list[dict] = []
    for digest, rows in source_groups.items():
        rows.sort(key=lambda item: item.rel_path)
        here = [item.rel_path for item in rows]
        if digest not in have_paths:
            missing.extend(rows)
            continue
        there = have_paths[digest]
        if here != there:
            same_bytes.append(
                {"digest": digest, "source_paths": here, "other_paths": there}
            )

    copied: list[dict] = []
    unreadable: list[dict] = []
    output.mkdir(parents=True)
    files_root = output / "files"
    for record in sorted(missing, key=lambda row: row.rel_path):
        src = from_rel(source_root, record.rel_path)
        if not src.is_file() or src.stat().st_size != record.size:
            unreadable.append({"path": record.rel_path, "digest": record.digest})
            continue
        dest = from_rel(files_root, record.rel_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(
            {
                "path": record.rel_path,
                "size": record.size,
                "mtime": record.mtime,
                "algo": record.algo,
                "digest": record.digest,
            }
        )

    manifest = {
        "source_volume": source_volume,
        "missing_from": have,
        "source_scanned_at": _scanned_at(conn, source_volume),
        "missing_catalog_scanned_at": _scanned_at(conn, have),
        "file_count": len(copied),
        "total_bytes": sum(item["size"] for item in copied),
        "folders": _folder_counts(copied),
        "files": copied,
        "same_bytes_different_path": same_bytes,
        "unreadable": unreadable,
    }
    (output / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (output / "README.txt").write_text(_readme(manifest), encoding="utf-8")
    return manifest


def _paths_by_digest(rows: list[FileRecord]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for row in rows:
        if not row.digest:
            continue
        grouped.setdefault(row.digest, []).append(row.rel_path)
    for paths in grouped.values():
        paths.sort()
    return grouped


def _scanned_at(conn: sqlite3.Connection, volume: str) -> str:
    row = conn.execute("SELECT scanned_at FROM volumes WHERE name = ?", (volume,)).fetchone()
    if row is None:
        raise NoFileMessError(f"unknown volume: {volume}")
    return str(row["scanned_at"])


def _folder_counts(files: list[dict]) -> list[dict]:
    counts: Counter[str] = Counter()
    for item in files:
        path = item["path"]
        folder = path.split("/", 1)[0] if "/" in path else "(root)"
        counts[folder] += 1
    return [{"folder": name, "files": count} for name, count in sorted(counts.items())]


def _readme(manifest: dict) -> str:
    lines = [
        f"Files from {manifest['source_volume']} that are absent from {manifest['missing_from']}.",
        f"Source catalog scanned at {manifest['source_scanned_at']}.",
        f"Other catalog scanned at {manifest['missing_catalog_scanned_at']}.",
        f"Copied {manifest['file_count']} files, {manifest['total_bytes']} bytes.",
        "",
        "Folders:",
    ]
    if manifest["folders"]:
        for folder in manifest["folders"]:
            lines.append(f"  {folder['folder']}: {folder['files']}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(
        f"Same bytes already on the other volume, at a different path: {len(manifest['same_bytes_different_path'])}."
    )
    lines.append(f"Unreadable source files: {len(manifest['unreadable'])}.")
    lines.append("File list: MANIFEST.json.")
    lines.append("")
    return "\n".join(lines)
