"""Adding files to the Common Space: the local UI streams a file into the shared folder."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .fsutil import format_size, free_space, move_without_overwrite
from .indexer import is_ignored_name
from .pathsafe import UnsafePathError, is_within, safe_join, split_relative

WRITE_BUFFER = 1024 * 1024
TEMP_PREFIX = ".lanshare-upload-"


async def receive_upload(request: Request, share_dir: Path, name: str, mtime_ms: float | None) -> dict:
    """Stream the request body into share_dir/name, renaming to "name (n).ext" on collision."""
    try:
        parts = split_relative(name)
        target = safe_join(share_dir, name)
    except UnsafePathError as exc:
        raise HTTPException(400, f"Invalid file name: {exc}.") from exc
    if any(is_ignored_name(p) for p in parts):
        raise HTTPException(400, "Hidden and temporary files (names starting with '.' or ending in .tmp/.part) are not shared.")

    root = share_dir.resolve()
    directory = target.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(409, f"Cannot create folder {'/'.join(parts[:-1])}: {exc.strerror or exc}.") from exc
    if not is_within(directory.resolve(), root):
        raise HTTPException(400, "Invalid file name: path escapes the shared folder.")

    expected = request.headers.get("content-length")
    expected_size = int(expected) if expected and expected.isdigit() else None
    free = free_space(directory)
    if expected_size is not None and free is not None and expected_size > free:
        raise HTTPException(507, f"Not enough disk space: {format_size(expected_size)} needed, {format_size(free)} free.")

    tmp = directory / f"{TEMP_PREFIX}{uuid.uuid4().hex}.tmp"
    size = 0
    try:
        with open(tmp, "xb") as f:
            buf = bytearray()
            async for chunk in request.stream():
                buf += chunk
                size += len(chunk)
                if len(buf) >= WRITE_BUFFER:
                    data, buf = bytes(buf), bytearray()
                    await run_in_threadpool(f.write, data)
            if buf:
                await run_in_threadpool(f.write, bytes(buf))
    except ClientDisconnect:
        _remove(tmp)
        raise
    except OSError as exc:
        _remove(tmp)
        raise HTTPException(507 if exc.errno == 28 else 500, f"Could not save the file: {exc.strerror or exc}.") from exc
    except BaseException:
        _remove(tmp)
        raise

    if expected_size is not None and size != expected_size:
        _remove(tmp)
        raise HTTPException(400, "The upload was incomplete.")
    if mtime_ms is not None and 0 < mtime_ms < 32503680000000:  # before year 3000
        try:
            os.utime(tmp, (time.time(), mtime_ms / 1000))
        except OSError:
            pass
    try:
        final = await run_in_threadpool(move_without_overwrite, tmp, directory, parts[-1])
    except OSError as exc:
        _remove(tmp)
        raise HTTPException(500, f"Could not save the file: {exc}.") from exc
    relpath = final.relative_to(root).as_posix()
    return {"name": relpath, "size": size, "renamed": final.name != parts[-1]}


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
