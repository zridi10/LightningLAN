"""Filesystem helpers shared by uploads and downloads."""

from __future__ import annotations

import errno
import os
import shutil
import sys
from pathlib import Path

from .pathsafe import numbered_names


def move_without_overwrite(src: Path, directory: Path, filename: str, max_tries: int = 10_000) -> Path:
    """Move `src` into `directory` as `filename`, or "name (1).ext", "name (2).ext", ...

    Never replaces an existing file, even if one appears concurrently.
    """
    names = numbered_names(filename)
    for _ in range(max_tries):
        target = directory / next(names)
        if _try_move(src, target):
            return target
    raise FileExistsError(f"could not find a free name for {filename} in {directory}")


def _try_move(src: Path, target: Path) -> bool:
    if sys.platform == "win32":
        try:
            os.rename(src, target)  # fails if target exists
            return True
        except FileExistsError:
            return False
    try:
        os.link(src, target)  # atomic "create if absent"
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EXDEV, errno.EOPNOTSUPP, errno.EMLINK):
            raise
        if target.exists():  # filesystem without hard links (FAT, some network shares)
            return False
        os.rename(src, target)
        return True
    os.unlink(src)
    return True


def long_path(path: Path | str) -> str:
    r"""The path as Windows opens it even past 260 characters (the \\?\ form); unchanged elsewhere.

    Games nest their files deep inside the Steam folder, and Windows refuses longer paths unless
    long paths are turned on for the whole system.
    """
    text = str(path)
    if sys.platform != "win32" or text.startswith("\\\\?\\"):
        return text
    text = os.path.abspath(text)
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def free_space(directory: Path) -> int | None:
    try:
        return shutil.disk_usage(directory).free
    except OSError:
        return None


def format_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
