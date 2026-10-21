"""Serving shared files to other peers: streaming, HTTP Range and optional rate limiting."""

from __future__ import annotations

import os
import re
import time
from typing import BinaryIO, Iterator
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .indexer import Indexer
from .pathsafe import UnsafePathError, safe_join

STREAM_CHUNK = 256 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)
UNSATISFIABLE = "unsatisfiable"


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


def _iter_file(f: BinaryIO, start: int, length: int, rate: float | None) -> Iterator[bytes]:
    """Read [start, start+length) in chunks; never holds more than one chunk in memory."""
    try:
        f.seek(start)
        remaining = length
        chunk_size = STREAM_CHUNK if not rate else max(16 * 1024, min(STREAM_CHUNK, int(rate / 10)))
        began, sent = time.monotonic(), 0
        while remaining > 0:
            data = f.read(min(chunk_size, remaining))
            if not data:
                return  # file shrank: the client sees a short body and keeps what it got
            remaining -= len(data)
            sent += len(data)
            yield data
            if rate:
                ahead = sent / rate - (time.monotonic() - began)
                if ahead > 0:
                    time.sleep(ahead)
    finally:
        f.close()


def _error(status: int, error: str, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail}, status_code=status, headers=headers)


def serve_download(indexer: Indexer, file_id: str, request: Request, rate: float | None) -> Response:
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
        _iter_file(f, start, max(0, end - start + 1), rate),
        status_code=status,
        headers=headers,
        media_type="application/octet-stream",
    )
