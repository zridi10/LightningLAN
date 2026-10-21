"""Serving files to other peers: shared files (with HTTP Range), swarm pieces, and the upload limit."""

from __future__ import annotations

import os
import re
import threading
import time
from typing import TYPE_CHECKING, BinaryIO, Callable, Iterator
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .indexer import Indexer
from .pathsafe import UnsafePathError, safe_join

if TYPE_CHECKING:
    from .transfers import DownloadManager

STREAM_CHUNK = 256 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)
UNSATISFIABLE = "unsatisfiable"
_BURST = 0.25  # seconds of unused allowance a quiet limiter may save up


class UploadsCut(Exception):
    """Raised inside a transfer to another peer when this peer stops sharing: it ends right there."""


class UploadLimiter:
    """Counts the bytes sent to other peers and, if a limit is set, keeps their total under it.

    It also ends every transfer at once when this peer stops sharing (cut_off).
    """

    def __init__(self, rate: float | None = None):
        self.rate = rate  # bytes per second for all transfers together; None = unlimited
        self.sent = 0
        self.open = True
        self.generation = 0  # bumps on every cut_off; transfers started before it end
        self._clock = time.monotonic()
        self._lock = threading.Lock()

    def cut_off(self) -> None:
        """End the transfers in progress (at their next chunk) and refuse new ones until reopen()."""
        with self._lock:
            self.open = False
            self.generation += 1

    def reopen(self) -> None:
        self.open = True

    def allows(self, generation: int) -> bool:
        return self.open and self.generation == generation

    def chunk_size(self) -> int:
        return STREAM_CHUNK if not self.rate else max(16 * 1024, min(STREAM_CHUNK, int(self.rate / 10)))

    def consume(self, n: int) -> None:
        """Account for n bytes about to be sent, sleeping first if that would exceed the limit."""
        with self._lock:
            self.sent += n
            if not self.rate:
                return
            now = time.monotonic()
            self._clock = max(self._clock, now - _BURST) + n / self.rate
            delay = self._clock - now - _BURST
        if delay > 0:
            time.sleep(delay)


def parse_range(header: str, size: int) -> tuple[int, int] | str | None:
    """Parse a single-range "bytes=" header into an inclusive (start, end).

    Returns None when the header should be ignored (malformed or multi-range: the whole file
    is sent instead, as RFC 9110 allows) and UNSATISFIABLE when it asks for bytes past the end.
    """
    m = _RANGE_RE.match(header)
    if not m:
        return None
    first, last = m.groups()
    if not first and not last:
        return None
    if not first:  # suffix range: the last N bytes
        n = int(last)
        if n == 0 or size == 0:
            return UNSATISFIABLE
        return max(0, size - n), size - 1
    start = int(first)
    if start >= size:
        return UNSATISFIABLE
    end = int(last) if last else size - 1
    if end < start:
        return None
    return start, min(end, size - 1)


def _iter_file(f: BinaryIO, start: int, length: int, limiter: UploadLimiter,
               on_close: Callable[[], None] | None = None) -> Iterator[bytes]:
    """Read [start, start+length) in chunks; never holds more than one chunk in memory."""
    try:
        generation = limiter.generation
        f.seek(start)
        remaining = length
        chunk_size = limiter.chunk_size()
        while remaining > 0:
            if not limiter.allows(generation):
                raise UploadsCut()
            data = f.read(min(chunk_size, remaining))
            if not data:
                return  # file shrank: the client sees a short body and keeps what it got
            remaining -= len(data)
            limiter.consume(len(data))
            yield data
    finally:
        f.close()
        if on_close is not None:
            on_close()


def _error(status: int, error: str, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail}, status_code=status, headers=headers)


def serve_download(indexer: Indexer, file_id: str, request: Request, limiter: UploadLimiter) -> Response:
    if not _SHA256_RE.match(file_id):
        return _error(404, "not_found", "No such file.")
    entry = indexer.lookup(file_id)
    if entry is None:
        return _error(404, "not_found", "This peer does not share that file (any more).")
    try:
        # Re-check at serve time: a folder could have been swapped for a link since indexing.
        path = safe_join(indexer.root, entry.relpath)
        f = open(path, "rb")
    except (UnsafePathError, OSError):
        indexer.request_rescan()
        return _error(404, "not_found", "This peer does not share that file (any more).")
    st = os.fstat(f.fileno())
    if st.st_size != entry.size or st.st_mtime_ns != entry.mtime_ns:
        f.close()
        indexer.request_rescan()
        return _error(409, "changed", "The file changed since it was indexed; try again shortly.")

    size = entry.size
    etag = f'"{entry.sha256}"'
    start, end, status = 0, size - 1, 200
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if range_header and (if_range is None or if_range.strip() == etag):
        parsed = parse_range(range_header, size)
        if parsed == UNSATISFIABLE:
            f.close()
            return _error(416, "range_not_satisfiable", "Requested range is past the end of the file.",
                          headers={"Content-Range": f"bytes */{size}"})
        if parsed is not None:
            start, end = parsed  # type: ignore[misc]
            status = 206

    filename = entry.relpath.rsplit("/", 1)[-1]
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Length": str(max(0, end - start + 1)),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "no-store",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        f.close()
        return Response(status_code=status, headers=headers, media_type="application/octet-stream")
    return StreamingResponse(
        _iter_file(f, start, max(0, end - start + 1), limiter),
        status_code=status,
        headers=headers,
        media_type="application/octet-stream",
    )


def serve_piece(downloads: DownloadManager, file_id: str, index: int, limiter: UploadLimiter) -> Response:
    """One verified piece of a file this peer is downloading (or downloaded), for the swarm."""
    if not _SHA256_RE.match(file_id) or index < 0:
        return _error(404, "not_found", "No such piece.")
    located = downloads.locate_piece(file_id, index)
    if located is None:
        return _error(404, "not_found", "This peer doesn't have that piece.")
    state, path, start, length = located
    try:
        f = open(path, "rb")
    except OSError:
        state.release_reader()
        return _error(404, "not_found", "This peer doesn't have that piece.")
    return StreamingResponse(
        _iter_file(f, start, length, limiter, on_close=state.release_reader),
        headers={"Content-Length": str(length), "Cache-Control": "no-store"},
        media_type="application/octet-stream",
    )
