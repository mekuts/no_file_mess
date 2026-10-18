"""SQLite catalog of files on named volumes.

A volume name is chosen by the person running the tool. The last root
path is stored only so a later command can remind them where the disk
was mounted. Identity of a file, for duplicate grouping and for the
disk diff, is the hash of its bytes.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from no_file_mess.errors import NoFileMessError
from no_file_mess.hashing import HASH_ALGO, hash_file, is_dataless
from no_file_mess.paths import to_rel

# Directory names that are filesystem bookkeeping rather than a collection.
_SKIP_DIRS = {
    ".spotlight-v100",
    ".trashes",
    ".fseventsd",
    ".temporaryitems",
    "$recycle.bin",
    "system volume information",
    ".no-file-mess-quarantine",
}
_SKIP_FILES = {".ds_store", "thumbs.db", "desktop.ini"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS volumes (
    name TEXT PRIMARY KEY,
    root TEXT NOT NULL,
    scanned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    volume TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    algo TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (volume, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_files_digest ON files(digest);
"""


@dataclass(frozen=True)
class FileRecord:
    """One cataloged file.

    Attributes:
        volume: Volume name supplied at scan time.
        rel_path: Path relative to the volume root, with forward slashes.
        size: Size in bytes.
        mtime: Modification time, seconds since the Unix epoch.
        algo: Hash algorithm name. ``sha256`` in this version.
        digest: Hex digest of the file contents.
    """

    volume: str
    rel_path: str
    size: int
    mtime: float
    algo: str
    digest: str


@dataclass(frozen=True)
class ScanStats:
    """Counts from one completed scan.

    Attributes:
        files: Files written into the catalog.
        skipped_dataless: Placeholders that were not opened.
        skipped_special: Symlinks and non-regular files.
        skipped_noise: Bookkeeping names such as ``.DS_Store``.
    """

    files: int
    skipped_dataless: int
    skipped_special: int
    skipped_noise: int


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the catalog, creating tables on first use.

    Args:
        db_path: SQLite file. Parent directories must already exist.

    Returns:
        A connection with rows accessible by column name.
    """
    if db_path.parent != Path() and not db_path.parent.exists():
        raise NoFileMessError(f"catalog directory does not exist: {db_path.parent}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _ensure_fts(conn)
    return conn


def scan(conn: sqlite3.Connection, volume: str, root: Path) -> ScanStats:
    """Replace one volume's file list with a fresh walk of ``root``.

    The previous rows for ``volume`` stay in place until the new list is
    ready, so a failed read does not wipe the catalog. The file list is
    held in memory for the length of the scan.

    Args:
        conn: Open catalog.
        volume: Stable name for this disk.
        root: Directory to walk. Must exist and be a directory.

    Returns:
        Counts of stored and skipped files.
    """
    if not volume or "/" in volume or "\\" in volume:
        raise NoFileMessError("volume name must be a single path segment")
    root = root.resolve()
    if not root.is_dir():
        raise NoFileMessError(f"not a directory: {root}")

    records: list[FileRecord] = []
    skipped_dataless = 0
    skipped_special = 0
    skipped_noise = 0

    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirpath = Path(current)
        dirnames[:] = [name for name in dirnames if name.casefold() not in _SKIP_DIRS]
        for name in filenames:
            path = dirpath / name
            if name.casefold() in _SKIP_FILES:
                skipped_noise += 1
                continue
            try:
                # lstat does not follow a symlink, so the link is not hashed as its target.
                info = path.lstat()
            except OSError:
                skipped_special += 1
                continue
            if not stat.S_ISREG(info.st_mode):
                skipped_special += 1
                continue
            if is_dataless(info):
                skipped_dataless += 1
                continue
            try:
                digest = hash_file(path)
            except OSError:
                skipped_special += 1
                continue
            records.append(
                FileRecord(
                    volume=volume,
                    rel_path=to_rel(root, path),
                    size=info.st_size,
                    mtime=info.st_mtime,
                    algo=HASH_ALGO,
                    digest=digest,
                )
            )

    scanned_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute(
            """
            INSERT INTO volumes (name, root, scanned_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET root = excluded.root,
                                            scanned_at = excluded.scanned_at
            """,
            (volume, str(root), scanned_at),
        )
        conn.execute("DELETE FROM files WHERE volume = ?", (volume,))
        conn.executemany(
            """
            INSERT INTO files (volume, rel_path, size, mtime, algo, digest)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (rec.volume, rec.rel_path, rec.size, rec.mtime, rec.algo, rec.digest)
                for rec in records
            ],
        )
        _replace_fts(conn, volume, [rec.rel_path for rec in records])
    return ScanStats(
        files=len(records),
        skipped_dataless=skipped_dataless,
        skipped_special=skipped_special,
        skipped_noise=skipped_noise,
    )


def list_volumes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return known volumes, most recently scanned first.

    Args:
        conn: Open catalog.

    Returns:
        Rows with ``name``, ``root``, and ``scanned_at``.
    """
    return list(conn.execute("SELECT name, root, scanned_at FROM volumes ORDER BY scanned_at DESC"))


def files_for_volume(conn: sqlite3.Connection, volume: str) -> list[FileRecord]:
    """Return every file recorded for ``volume``.

    Args:
        conn: Open catalog.
        volume: Volume name.

    Returns:
        File records ordered by relative path.

    Raises:
        NoFileMessError: The volume has never been scanned.
    """
    _require_volume(conn, volume)
    rows = conn.execute(
        """
        SELECT volume, rel_path, size, mtime, algo, digest
        FROM files WHERE volume = ? ORDER BY rel_path
        """,
        (volume,),
    )
    return [_record(row) for row in rows]


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    volume: str | None = None,
    substring: bool = False,
) -> list[FileRecord]:
    """Find files by relative path.

    The default match is a full-text query: each word must occur as a
    prefix of a path token, so ``fran`` matches ``France``. A substring
    search is available for a path fragment that is not a whole token.

    Args:
        conn: Open catalog.
        query: Text to look for. Must not be empty.
        volume: When set, only this volume is searched.
        substring: Match the raw query anywhere in the path.

    Returns:
        Matching file records.
    """
    text = query.strip()
    if not text:
        raise NoFileMessError("search query is empty")
    if volume is not None:
        _require_volume(conn, volume)
    if substring or not _fts_ready(conn):
        return _search_like(conn, text, volume)
    match = _fts_query(text)
    if match is None:
        return _search_like(conn, text, volume)
    sql = """
        SELECT f.volume, f.rel_path, f.size, f.mtime, f.algo, f.digest
        FROM files_fts
        JOIN files AS f
          ON f.volume = files_fts.volume AND f.rel_path = files_fts.rel_path
        WHERE files_fts MATCH ?
    """
    params: list[object] = [match]
    if volume is not None:
        sql += " AND f.volume = ?"
        params.append(volume)
    sql += " ORDER BY f.volume, f.rel_path"
    return [_record(row) for row in conn.execute(sql, params)]


def _require_volume(conn: sqlite3.Connection, volume: str) -> None:
    row = conn.execute("SELECT 1 FROM volumes WHERE name = ?", (volume,)).fetchone()
    if row is None:
        raise NoFileMessError(f"unknown volume: {volume}")


def _record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        volume=row["volume"],
        rel_path=row["rel_path"],
        size=row["size"],
        mtime=row["mtime"],
        algo=row["algo"],
        digest=row["digest"],
    )


def _ensure_fts(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(volume UNINDEXED, rel_path)"
        )
    except sqlite3.OperationalError:
        return


def _fts_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'files_fts'"
    ).fetchone()
    return row is not None


def _replace_fts(conn: sqlite3.Connection, volume: str, rel_paths: list[str]) -> None:
    if not _fts_ready(conn):
        return
    conn.execute("DELETE FROM files_fts WHERE volume = ?", (volume,))
    conn.executemany(
        "INSERT INTO files_fts (volume, rel_path) VALUES (?, ?)",
        [(volume, rel) for rel in rel_paths],
    )


def _fts_query(text: str) -> str | None:
    """Turn user text into an FTS5 prefix query, or None if nothing remains."""
    tokens: list[str] = []
    for raw in text.replace("-", " ").replace("_", " ").replace(".", " ").split():
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if cleaned:
            tokens.append('"{token}"*'.format(token=cleaned))
    if not tokens:
        return None
    return " ".join(tokens)


def _search_like(
    conn: sqlite3.Connection, text: str, volume: str | None
) -> list[FileRecord]:
    sql = """
        SELECT volume, rel_path, size, mtime, algo, digest
        FROM files
        WHERE instr(lower(rel_path), lower(?)) > 0
    """
    params: list[object] = [text]
    if volume is not None:
        sql += " AND volume = ?"
        params.append(volume)
    sql += " ORDER BY volume, rel_path"
    return [_record(row) for row in conn.execute(sql, params)]
