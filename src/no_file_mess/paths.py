"""Relative paths stored with forward slashes.

A catalog written on one machine has to stay readable on another, so
paths inside the database never use the local separator.
"""

from pathlib import Path, PurePosixPath

from no_file_mess.errors import NoFileMessError


def to_rel(root: Path, path: Path) -> str:
    """Return ``path`` relative to ``root`` using forward slashes.

    Args:
        root: Volume root the walk started at.
        path: File or directory under ``root``.

    Returns:
        The relative path.
    """
    return path.relative_to(root).as_posix()


def from_rel(root: Path, rel: str) -> Path:
    """Join a stored relative path onto a local root.

    Args:
        root: Current location of the volume.
        rel: Path as stored in the catalog.

    Returns:
        The local path.

    Raises:
        NoFileMessError: ``rel`` is absolute or contains ``..``.
    """
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        raise NoFileMessError(f"path escapes the volume: {rel}")
    return root.joinpath(*pure.parts)
