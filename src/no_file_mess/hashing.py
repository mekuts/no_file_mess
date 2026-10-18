"""Content identity for catalog rows.

Every cataloged file gets a full SHA-256. A partial hash would not be
enough to decide that two disks hold the same bytes, and the offline
diff depends on that decision. SHA-256 comes from the standard library
so the tool has nothing to compile or install.
"""

import hashlib
import os
from pathlib import Path

HASH_ALGO = "sha256"
# macOS marks an iCloud file that is not stored on this machine.
_DATALESS_FLAG = 0x40000000
_CHUNK = 1024 * 1024


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of ``path``.

    Args:
        path: Regular file to read.

    Returns:
        Lowercase hex digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_dataless(stat_result: os.stat_result) -> bool:
    """Return whether ``stat_result`` is an undownloaded cloud placeholder.

    Opening such a file would start a download. Systems without the flag
    are treated as local.

    Args:
        stat_result: Result of ``os.stat`` or ``Path.stat``.

    Returns:
        True when the file should be left unread.
    """
    flags = getattr(stat_result, "st_flags", 0) or 0
    return bool(flags & _DATALESS_FLAG)
