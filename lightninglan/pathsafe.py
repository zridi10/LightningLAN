"""Filesystem path safety.

Every path that comes from outside this process (an upload name from the browser, a file
name in another peer's index) goes through this module before it touches the disk. The rules
are deliberately strict and identical on every OS, so a name that is safe on one peer is safe
on all of them.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterator


class UnsafePathError(ValueError):
    """A path was rejected because it could escape the shared folder or is not portable."""


# Characters Windows refuses in file names, plus all ASCII control characters. ':' is in here
# because on NTFS "name:stream" writes to a hidden alternate data stream and "C:x" is a
# drive-relative path.
_INVALID_CHARS = frozenset('<>:"|?*') | frozenset(chr(i) for i in range(32)) | {"\x7f"}

# Device names Windows reserves regardless of extension ("CON.txt" opens the console).
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{i}" for i in "123456789¹²³"]
    + [f"LPT{i}" for i in "123456789¹²³"]
)

# Unicode controls that reorder or hide text. "invoice\u202Etxt.exe" displays as
# "invoiceexe.txt", so these are stripped from anything shown to a user.
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

_SEPARATORS = re.compile(r"[\\/]")
MAX_COMPONENT_BYTES = 255
MAX_PATH_CHARS = 1024


def split_relative(name: str) -> list[str]:
    """Validate a relative path supplied by a client and return its components.

    Accepts "file.txt" and "folder/file.txt" ('/' or '\\' as separator). Rejects absolute
    paths, Windows drive and UNC paths, '..', empty or '.' components, and names that are not
    valid on Windows. Raises UnsafePathError with a human-readable reason.
    """
    if not isinstance(name, str) or not name:
        raise UnsafePathError("empty file name")
    if len(name) > MAX_PATH_CHARS:
        raise UnsafePathError("file name is too long")
    if "\x00" in name:
        raise UnsafePathError("file name contains a NUL byte")
    if name[0] in "/\\":
        # Covers "/etc/passwd", "\\server\share", "//server/share" and "\\?\C:\...".
        raise UnsafePathError("absolute paths are not allowed")
    win = PureWindowsPath(name)
    if win.drive or win.root:
        raise UnsafePathError("drive letters and UNC paths are not allowed")
    if PurePosixPath(name).is_absolute():
        raise UnsafePathError("absolute paths are not allowed")

    parts = _SEPARATORS.split(name)
    for part in parts:
        if part == "..":
            raise UnsafePathError("'..' is not allowed in file names")
        if part in ("", "."):
            raise UnsafePathError("file name has an empty or '.' path component")
        validate_component(part)
    return parts


def validate_component(part: str) -> None:
    """Reject a single path component that is unsafe or not portable."""
    bad = sorted({c for c in part if c in _INVALID_CHARS})
    if bad:
        shown = " ".join(repr(c) for c in bad)
        raise UnsafePathError(f"file name contains characters that are not allowed: {shown}")
    if part.endswith((".", " ")):
        # Windows silently strips these, so "a.txt." and "a.txt" would be the same file.
        raise UnsafePathError("file names cannot end with a dot or a space")
    if part.split(".")[0].rstrip(" ").upper() in _WINDOWS_RESERVED:
        raise UnsafePathError(f"'{part}' is a reserved device name on Windows")
    if len(part.encode("utf-8")) > MAX_COMPONENT_BYTES:
        raise UnsafePathError("file name is too long")


def is_within(path: Path, root: Path) -> bool:
    """True if `path` is `root` or inside it. Both paths must already be resolved."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def safe_join(root: Path, relpath: str) -> Path:
    """Join an untrusted relative path onto `root`, refusing anything that lands outside it.

    The result is fully resolved, so a symlink or junction inside the folder that points
    elsewhere is caught here too.
    """
    parts = split_relative(relpath)
    root_resolved = root.resolve()
    target = root_resolved.joinpath(*parts).resolve()
    if target == root_resolved or not is_within(target, root_resolved):
        raise UnsafePathError("path escapes the shared folder")
    return target


def display_name(name: str) -> str:
    """Make a file name safe to show a user.

    Removes bidirectional-override characters and replaces control characters. HTML is not
    escaped here: the UI only ever inserts text with textContent, never as markup.
    """
    out = []
    for c in name:
        if c in _BIDI_CONTROLS:
            continue
        out.append("\ufffd" if (ord(c) < 32 or ord(c) == 0x7F) else c)
    return "".join(out)[:MAX_PATH_CHARS]


def safe_local_filename(name: str, fallback: str = "download") -> str:
    """Turn a (possibly hostile) remote file name into a single safe local file name.

    Unlike split_relative this never fails: directories are dropped, bad characters become
    '_', and reserved or empty names get a prefix or a fallback.
    """
    base = _SEPARATORS.split(name)[-1] if name else ""
    base = "".join(c for c in base if c not in _BIDI_CONTROLS)
    base = "".join("_" if c in _INVALID_CHARS else c for c in base)
    base = base.strip().rstrip(". ")
    if base.startswith("."):
        base = "_" + base[1:]
    if not base or set(base) <= {"_", "."}:
        base = fallback
    if base.split(".")[0].rstrip(" ").upper() in _WINDOWS_RESERVED:
        base = "_" + base
    return _truncate_keep_extension(base, 200)


def _truncate_keep_extension(name: str, max_bytes: int) -> str:
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    stem, ext = split_extension(name)
    if len(ext.encode("utf-8")) > 32:
        stem, ext = name, ""
    budget = max_bytes - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(". ")
    return (stem or "download") + ext


def split_extension(filename: str) -> tuple[str, str]:
    """Split "report.pdf" into ("report", ".pdf"); keeps ".tar.gz" style double extensions."""
    if filename.startswith(".") and filename.count(".") == 1:
        return filename, ""
    stem, dot, ext = filename.rpartition(".")
    if not dot or not stem:
        return filename, ""
    ext = "." + ext
    if stem.lower().endswith(".tar"):
        stem, ext = stem[:-4], stem[-4:] + ext
    return stem, ext


def numbered_names(filename: str) -> Iterator[str]:
    """Yield "a.txt", "a (1).txt", "a (2).txt", ... for collision handling."""
    yield filename
    stem, ext = split_extension(filename)
    for i in itertools.count(1):
        yield f"{stem} ({i}){ext}"
