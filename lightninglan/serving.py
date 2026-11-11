"""Serving files to other peers: shared files and Steam game files (with HTTP Range), swarm pieces,
and the upload limit."""

from __future__ import annotations

import base64
import os
import re
import threading
import time
from collections import deque
from functools import partial
from typing import TYPE_CHECKING, BinaryIO, Callable, Iterator
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .compress import COMPRESSED_HEADER, ZSTD, Compressibility, compressed, wants_compressed
from .fsutil import long_path
from .indexer import Indexer
from .pathsafe import UnsafePathError, open_within
from .pieces import piece_count, piece_size_for, swarmable

if TYPE_CHECKING:
    from pathlib import Path

    from .transfers import DownloadManager

STREAM_CHUNK = 256 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PEER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)
UNSATISFIABLE = "unsatisfiable"
_BURST = 0.25  # seconds of unused allowance a quiet limiter may save up

# A swarm downloader asking a sharer for a piece (LightningLAN 1.7+) says who it is and which pieces it
# has or is fetching. The sharer may then send another piece than the one asked for (naming it), and
# names the other peers fetching the file from it, so they find each other at once.
PEER_HEADER = "X-LightningLAN-Peer"
HAVE_HEADER = "X-LightningLAN-Have"
PIECE_HEADER = "X-LightningLAN-Piece"
FELLOWS_HEADER = "X-LightningLAN-Fellows"
GOT_HEADER = "X-LightningLAN-Got"  # the pieces it has in full (HAVE_HEADER also counts those it's fetching)
BUSY_HEADER = "X-LightningLAN-Busy"  # with a 503: take pieces from the others for now
RELAY_SAMPLES = 16  # recent times taken by a peer's fresh pieces to reach another, kept per peer
MIN_RELAY_SAMPLES = 4  # needed from the other peers before anyone counts as slow
SLOW_FACTOR = 4.0  # a peer holding a fresh piece this many times longer than usual is slow to pass it on
SLOW_MIN = 0.5  # seconds, at least
FELLOW_WINDOW = 30.0  # peers that asked for a file this recently are named to each other
MAX_FELLOWS = 32
LEDGER_IDLE = 600.0  # what was sent of a file nobody asked for in this long is forgotten


class UploadsCut(Exception):
    """Raised inside a transfer to another peer when this peer stops sharing: it ends right there."""


class _Seeded:
    """What this sharer has sent of one file: the pieces sent in full or being sent (a bit per piece,
    first piece in the high bit, as pieces.pack_bits), who asked for it lately, and how fast the pieces
    it sent first to each peer reached another one."""

    def __init__(self, size: int, now: float):
        self.piece_size = piece_size_for(size)
        self.count = piece_count(size)
        self.width = -(-self.count // 8) * 8  # bits, as packed
        self.all = ((1 << self.count) - 1) << (self.width - self.count)
        self.sent = bytearray(self.count)  # times sent in full (at most 255)
        self.sending = bytearray(self.count)  # transfers in progress
        self.busy = 0  # sent or being sent
        self.askers: dict[str, float] = {}
        self.fresh: dict[int, tuple[str, float]] = {}  # piece -> (the one peer that has it, since when)
        self.relay: dict[str, deque[float]] = {}  # peer -> how long its fresh pieces took to reach another
        self.touched = now

    def bit(self, index: int) -> int:
        return 1 << (self.width - 1 - index)

    def bits(self, text: str | None) -> int | None:
        """A have-map header as an int of this width, or None if it's missing or doesn't fit."""
        if text is None:
            return None
        try:
            packed = base64.b64decode(text.encode("ascii"), validate=True)
        except ValueError:
            return None
        return int.from_bytes(packed, "big") if len(packed) * 8 == self.width else None


class SeedBusy(Exception):
    """The asker should take pieces from the other peers for now (sent as 503, with BUSY_HEADER)."""

    def __init__(self, headers: dict[str, str]):
        super().__init__("busy")
        self.headers = headers


class SeedLedger:
    """A sharer's record of what it has sent of each file, so that while some pieces haven't gone out
    yet it never sends one twice: the swarm passes those on (smartseed, MSR-TR-2005-03). It is only
    consulted for requests that opt in by sending HAVE_HEADER, as they accept another piece.

    A piece nobody else has yet is best sent to a peer that passes pieces on quickly. Each request
    also says which pieces the asker has received in full (GOT_HEADER), so the ledger sees how long
    the pieces it sent first to each peer took to reach a second one. A peer whose pieces stay with
    it far longer than everyone else's (on Wi-Fi, say, with a tenth of the upload) is told to take
    pieces from the others instead, until it has passed on what it holds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._files: dict[str, _Seeded] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def plan(self, key: str, size: int, start: int, end: int,
             request: Request) -> tuple[int, int, dict[str, str], bool]:
        """The (start, end) to send for a request of [start, end], the headers saying so, and whether
        done() must be called when the transfer ends. A request for one whole piece already sent (or
        being sent) gets the first piece that hasn't been, if the asker lacks it; every request also
        learns who else is fetching the file. Raises SeedBusy for a peer slow to pass pieces on."""
        peer_id = request.headers.get(PEER_HEADER, "")
        have_text = request.headers.get(HAVE_HEADER)
        if not swarmable(size) or not _PEER_ID_RE.match(peer_id) or have_text is None:
            return start, end, {}, False
        now = self._clock()
        with self._lock:
            entry = self._files.get(key)
            if entry is None:
                self._prune(now)
                entry = self._files[key] = _Seeded(size, now)
            entry.touched = now
            entry.askers[peer_id] = now
            fellows = [p for p, at in entry.askers.items() if p != peer_id and now - at < FELLOW_WINDOW]
            headers = {FELLOWS_HEADER: ",".join(fellows[-MAX_FELLOWS:])}  # even empty: it says we keep a ledger
            index, rem = divmod(start, entry.piece_size)
            if rem or end != min(start + entry.piece_size, size) - 1:
                return start, end, headers, False  # not one whole piece
            got = entry.bits(request.headers.get(GOT_HEADER))
            if got is not None:
                self._note_passed_on(entry, peer_id, got, now)
            have = entry.bits(have_text)
            unsent = entry.all & ~(entry.busy | (entry.all if have is None else have))
            requested_unsent = not entry.busy & entry.bit(index)
            if (requested_unsent or unsent) and fellows and self._slow(entry, peer_id, now):
                raise SeedBusy(headers)
            if not requested_unsent and unsent:
                index = entry.width - unsent.bit_length()  # the first one not sent yet
                start = index * entry.piece_size
                end = min(start + entry.piece_size, size) - 1
                headers[PIECE_HEADER] = str(index)
            entry.sending[index] = min(255, entry.sending[index] + 1)
            entry.busy |= entry.bit(index)
        return start, end, headers, True

    def done(self, key: str, size: int, start: int, peer_id: str, complete: bool) -> None:
        """A transfer planned by plan() ended, with all of it sent or not."""
        with self._lock:
            entry = self._files.get(key)
            if entry is None:
                return
            index = start // entry.piece_size
            if index >= entry.count or not entry.sending[index]:
                return
            entry.sending[index] -= 1
            if complete:
                if not entry.sent[index]:
                    entry.fresh[index] = (peer_id, self._clock())  # only that peer has it, for now
                entry.sent[index] = min(255, entry.sent[index] + 1)
            elif not entry.sent[index] and not entry.sending[index]:
                entry.busy &= ~entry.bit(index)  # it didn't get there: it's still to send

    def _note_passed_on(self, entry: _Seeded, asker: str, got: int, now: float) -> None:
        for index, (holder, since) in list(entry.fresh.items()):
            if holder != asker and got & entry.bit(index):
                entry.relay.setdefault(holder, deque(maxlen=RELAY_SAMPLES)).append(now - since)
                del entry.fresh[index]

    def _slow(self, entry: _Seeded, peer_id: str, now: float) -> bool:
        """Whether `peer_id` holds a piece only it has for far longer than pieces usually take to spread."""
        held = [since for holder, since in entry.fresh.values() if holder == peer_id]
        if not held:
            return False
        others = sorted(d for holder, delays in entry.relay.items() if holder != peer_id for d in delays)
        if len(others) < MIN_RELAY_SAMPLES:
            return False
        typical = others[len(others) // 2]
        return now - min(held) > max(SLOW_MIN, SLOW_FACTOR * typical)

    def _prune(self, now: float) -> None:
        for key in [k for k, e in self._files.items() if now - e.touched > LEDGER_IDLE]:
            del self._files[key]


class RateLimiter:
    """Counts bytes moved and, if a limit is set, keeps their total rate under it (by sleeping)."""

    def __init__(self, rate: float | None = None):
        self.rate = rate  # bytes per second for all transfers together; None = unlimited
        self.sent = 0
        self._clock = time.monotonic()
        self._lock = threading.Lock()

    def chunk_size(self) -> int:
        return STREAM_CHUNK if not self.rate else max(16 * 1024, min(STREAM_CHUNK, int(self.rate / 10)))

    def consume(self, n: int) -> None:
        """Account for n bytes about to be moved, sleeping first if that would exceed the limit."""
        with self._lock:
            self.sent += n
            if not self.rate:
                return
            now = time.monotonic()
            self._clock = max(self._clock, now - _BURST) + n / self.rate
            delay = self._clock - now - _BURST
        if delay > 0:
            time.sleep(delay)


class UploadLimiter(RateLimiter):
    """Counts the bytes sent to other peers and, if a limit is set, keeps their total under it.

    It also ends every transfer at once when this peer stops sharing (cut_off).
    """

    def __init__(self, rate: float | None = None):
        super().__init__(rate)
        self.open = True
        self.generation = 0  # bumps on every cut_off; transfers started before it end

    def cut_off(self) -> None:
        """End the transfers in progress (at their next chunk) and refuse new ones until reopen()."""
        with self._lock:
            self.open = False
            self.generation += 1

    def reopen(self) -> None:
        self.open = True

    def allows(self, generation: int) -> bool:
        return self.open and self.generation == generation


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
               on_close: Callable[[bool], None] | None = None, close: bool = True,
               count: bool = True) -> Iterator[bytes]:
    """Read [start, start+length) in chunks; never holds more than one chunk in memory. Closes `f` at the
    end unless told not to; on_close is told whether all of it was sent. The limiter counts the bytes
    unless they are compressed on the way (then compress.compressed counts what goes out)."""
    remaining = length
    try:
        generation = limiter.generation
        f.seek(start)
        chunk_size = limiter.chunk_size()
        while remaining > 0:
            if not limiter.allows(generation):
                raise UploadsCut()
            data = f.read(min(chunk_size, remaining))
            if not data:
                return  # file shrank: the client sees a short body and keeps what it got
            remaining -= len(data)
            if count:
                limiter.consume(len(data))
            yield data
    finally:
        if close:
            f.close()
        if on_close is not None:
            on_close(remaining <= 0)


def _error(status: int, error: str, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail}, status_code=status, headers=headers)


def serve_download(indexer: Indexer, file_id: str, request: Request, limiter: UploadLimiter,
                   ledger: SeedLedger | None = None, packer: Compressibility | None = None) -> Response:
    if not _SHA256_RE.match(file_id):
        return _error(404, "not_found", "No such file.")
    entry = indexer.lookup(file_id)
    if entry is None:
        return _error(404, "not_found", "This peer does not share that file (any more).")
    try:
        # Re-check at serve time: a folder could have been swapped for a link since indexing.
        f = open_within(indexer.root, entry.relpath)
    except (UnsafePathError, OSError):
        indexer.request_rescan()
        return _error(404, "not_found", "This peer does not share that file (any more).")
    st = os.fstat(f.fileno())
    if st.st_size != entry.size or st.st_mtime_ns != entry.mtime_ns:
        f.close()
        indexer.request_rescan()
        return _error(409, "changed", "The file changed since it was indexed; try again shortly.")
    return _send_file(f, entry.size, f'"{entry.sha256}"', entry.relpath.rsplit("/", 1)[-1], request, limiter,
                      ledger, entry.sha256, packer)


def serve_game_file(found: tuple[Path, int, int] | str, request: Request, limiter: UploadLimiter,
                    ledger: SeedLedger | None = None, key: str | None = None,
                    packer: Compressibility | None = None, pack_key: str | None = None) -> Response:
    """A file of a Steam game, found by SteamLibrary.open_file (a shared game) or GameDownloads.open_file
    (one being got here): (path, size, mtime_ns), "not_found" or "changed". `key` names it in `ledger`,
    `pack_key` its kind in `packer`: most game files are too small to tell by themselves."""
    if found == "changed":
        return _error(409, "changed", "The file changed since the game's file list was made; ask for it again.")
    if isinstance(found, str):
        return _error(404, "not_found", "This peer does not share that game file.")
    path, size, mtime_ns = found
    try:
        f = open(long_path(path), "rb")
    except OSError:
        return _error(404, "not_found", "This peer does not share that game file.")
    return _send_file(f, size, f'"{size:x}-{mtime_ns:x}"', path.name, request, limiter, ledger, key, packer, pack_key)


def _send_file(f: BinaryIO, size: int, etag: str, filename: str, request: Request, limiter: UploadLimiter,
               ledger: SeedLedger | None = None, key: str | None = None,
               packer: Compressibility | None = None, pack_key: str | None = None) -> Response:
    """Send an open file, or the part of it a Range header asks for (and close it when done). With a
    ledger, a swarm downloader asking for a piece may get another one (SeedLedger.plan)."""
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

    extra: dict[str, str] = {}
    on_close: Callable[[bool], None] | None = None
    if status == 206 and ledger is not None and key is not None and request.method != "HEAD":
        try:
            start, end, extra, tracked = ledger.plan(key, size, start, end, request)
        except SeedBusy as busy:
            f.close()
            return _error(503, "busy", "Take pieces of this file from the other peers for now.",
                          headers={**busy.headers, BUSY_HEADER: "1", "Retry-After": "1"})
        if tracked:  # told whether all of it was sent
            on_close = partial(ledger.done, key, size, start, request.headers.get(PEER_HEADER, ""))
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Length": str(max(0, end - start + 1)),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "no-store",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers.update(extra)
    if request.method == "HEAD":
        f.close()
        return Response(status_code=status, headers=headers, media_type="application/octet-stream")
    length = max(0, end - start + 1)
    pack_key = pack_key or key
    if packer is not None and pack_key and wants_compressed(request) and packer.worth_it(pack_key, filename):
        del headers["Content-Length"]  # of the bytes themselves, not of what is sent
        headers[COMPRESSED_HEADER] = ZSTD
        body = compressed(_iter_file(f, start, length, limiter, on_close, count=False), limiter.consume,
                          partial(packer.record, pack_key))
    else:
        body = _iter_file(f, start, length, limiter, on_close)
    return StreamingResponse(body, status_code=status, headers=headers, media_type="application/octet-stream")


def serve_piece(downloads: DownloadManager, file_id: str, index: int, limiter: UploadLimiter, request: Request,
                packer: Compressibility | None = None) -> Response:
    """One verified piece of a file this peer is downloading (or downloaded), for the swarm."""
    if not _SHA256_RE.match(file_id) or index < 0:
        return _error(404, "not_found", "No such piece.")
    located = downloads.locate_piece(file_id, index)
    if located is None:
        return _error(404, "not_found", "This peer doesn't have that piece.")
    state, path, start, length = located
    try:
        f = state.open_reader()
    except OSError:
        state.release_reader()
        return _error(404, "not_found", "This peer doesn't have that piece.")
    headers = {"Content-Length": str(length), "Cache-Control": "no-store"}
    if packer is not None and wants_compressed(request) and packer.worth_it(file_id, ""):
        del headers["Content-Length"]
        headers[COMPRESSED_HEADER] = ZSTD
        body = compressed(_iter_file(f, start, length, limiter, on_close=lambda complete: state.release_reader(f),
                                     close=False, count=False), limiter.consume, partial(packer.record, file_id))
    else:
        body = _iter_file(f, start, length, limiter, on_close=lambda complete: state.release_reader(f), close=False)
    return StreamingResponse(body, headers=headers, media_type="application/octet-stream")
