"""Swarm downloads: fetching a file's pieces from every peer that has them, in parallel.

Peers that share a file serve byte ranges of it (GET /api/download/<id> with a Range header).
Peers that are downloading the same file, or downloaded it and still list the download, serve
the pieces they have verified (GET /api/piece/<id>/<index>) and say which ones they have
(GET /api/swarm/<id>). Every piece is checked against its SHA-256 from the piece list before it is
kept or passed on, so a misbehaving peer can waste bandwidth but never corrupt a download.

The sharer's upload is usually the bottleneck, so while some pieces exist only at the sharer, a
downloader asks it only for pieces that no fellow downloader has or is fetching right now (each
one publishes both); everything else comes from the fellow downloaders. Pieces are requested rarest first, and among equally rare pieces each
downloader starts at its own point in the file and works forwards, which spreads requests out and
keeps writes mostly sequential.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Protocol

import httpx

from .auth import Authenticator
from .peers import describe_http_error
from .pieces import PieceInfo, decode_bits, encode_bits

if TYPE_CHECKING:
    from .peers import Peer, PeerTable

log = logging.getLogger(__name__)

WORKERS = 8  # pieces in flight per download
PER_SOURCE = 2  # pieces in flight per peer
REFRESH_INTERVAL = 0.5  # how often fellow downloaders' have-maps are re-read
MAX_FAILURES = 3  # failed requests in a row before a peer is given a long rest
FAILURE_REST = 30.0
MAX_BAD_PIECES = 2  # pieces failing verification before a peer is dropped for good
BAD_PIECE_REST = 2.0
STUCK_TIMEOUT = 15.0  # how long to wait for someone to offer the missing pieces
READ_TIMEOUT = 15.0


# ----- errors (mapped to download states by the download manager) ------------------------------

class SwarmStopped(Exception):
    """Paused or canceled."""


class SwarmFailed(Exception):
    """The download can't continue right now (message is user-facing); verified pieces are kept."""


class SwarmUnusable(Exception):
    """Pieces kept failing verification against the piece list: fall back to a whole-file download."""


class _BadPiece(Exception):
    pass


class _Gone(Exception):
    """This peer can't serve this file at all (no longer has it, changed it, failed auth)."""


class _LocalError(Exception):
    """Writing to our own disk failed: no peer can fix that."""

    def __init__(self, error: OSError):
        super().__init__(str(error))
        self.error = error


# ----- what this peer holds ------------------------------------------------------------------

class SwarmState:
    """The pieces of one file this peer holds and can pass on: while downloading, and after."""

    def __init__(self, info: PieceInfo, path: Path, have: bytearray | None = None):
        self.info = info
        self.path = path
        self.have = have if have is not None else bytearray(info.count)
        self.pending = bytearray(info.count)  # pieces being fetched right now
        self.complete = False
        self._complete_stat: tuple[int, int] | None = None
        self._readers = 0
        self._paused = False
        self._lock = threading.Condition()

    @property
    def have_count(self) -> int:
        return sum(self.have)

    @property
    def have_bytes(self) -> int:
        return sum(self.info.span(i)[1] for i, h in enumerate(self.have) if h)

    def mark(self, index: int) -> None:
        with self._lock:
            self.have[index] = 1
            self.pending[index] = 0

    def set_pending(self, index: int, value: bool) -> None:
        with self._lock:
            self.pending[index] = 1 if value else 0

    def prefix_bytes(self) -> int:
        """Length of the run of verified pieces at the start of the file."""
        end = 0
        for i, h in enumerate(self.have):
            if not h:
                break
            end = sum(self.info.span(i))
        return end

    def status(self) -> dict:
        with self._lock:
            return {
                "pieces": self.info.count,
                "piece_size": self.info.piece_size,
                "have": encode_bits(self.have),
                "pending": encode_bits(self.pending),
                "complete": self.complete,
            }

    def locate(self, index: int) -> tuple[Path, int, int] | None:
        """Where piece `index` can be read, registering a reader; None if we don't have it."""
        with self._lock:
            if self._paused or not 0 <= index < self.info.count or not self.have[index]:
                return None
            if self.complete and not self._unchanged():
                return None
            self._readers += 1
            start, length = self.info.span(index)
            return self.path, start, length

    def release_reader(self) -> None:
        with self._lock:
            self._readers -= 1
            self._lock.notify_all()

    @contextmanager
    def exclusive(self, timeout: float = 10.0) -> Iterator[None]:
        """No piece reads while the file is moved or deleted (Windows can't move an open file)."""
        with self._lock:
            self._paused = True
            self._lock.wait_for(lambda: self._readers <= 0, timeout=timeout)
        try:
            yield
        finally:
            with self._lock:
                self._paused = False

    def set_complete(self, path: Path) -> None:
        with self._lock:
            self.path = path
            self.complete = True
            self.have = bytearray(b"\x01" * self.info.count)
            self.pending = bytearray(self.info.count)
            try:
                st = path.stat()
                self._complete_stat = (st.st_size, st.st_mtime_ns)
            except OSError:
                self._complete_stat = None

    def _unchanged(self) -> bool:
        """A finished download is only passed on while the file is exactly as we saved it."""
        try:
            st = self.path.stat()
        except OSError:
            return False
        return self._complete_stat == (st.st_size, st.st_mtime_ns)


def scan_partial(part: Path, info: PieceInfo, stop: threading.Event) -> bytearray:
    """Which pieces an existing partial file already holds, by hashing them (resume after a restart)."""
    have = bytearray(info.count)
    try:
        size = part.stat().st_size
        f = open(part, "rb")
    except OSError:
        return have
    with f:
        for i in range(info.count):
            if stop.is_set():
                raise SwarmStopped()
            start, length = info.span(i)
            if start + length > size:
                break
            f.seek(start)
            digest = hashlib.sha256()
            remaining = length
            while remaining:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if not remaining and digest.digest() == info.digest(i):
                have[i] = 1
    return have


def make_sparse(f) -> None:
    """Let Windows skip zero-filling the gaps when pieces are written out of order (best effort)."""
    if sys.platform != "win32":
        return  # Linux and macOS file systems leave unwritten ranges as holes already
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        fsctl_set_sparse = 0x900C4
        returned = wintypes.DWORD()
        ctypes.windll.kernel32.DeviceIoControl(
            wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno())), fsctl_set_sparse,
            None, 0, None, 0, ctypes.byref(returned), None,
        )
    except Exception:
        log.debug("could not mark the partial file sparse", exc_info=True)


# ----- the download session --------------------------------------------------------------------

@dataclass(eq=False)
class _Source:
    peer: Peer
    seed: bool  # shares the whole file: pieces come as byte ranges of /api/download
    have: bytearray | None = None  # None: has every piece
    pending: bytearray | None = None  # pieces it is fetching right now (fellow downloaders)
    inflight: int = 0
    failures: int = 0
    bad_pieces: int = 0
    retry_at: float = 0.0
    dropped: bool = False  # sent bad data or can't serve the file: not used again
    present: bool = True  # still online and offering the file
    bytes: int = 0
    last_error: str = ""

    def has(self, index: int) -> bool:
        return self.have is None or bool(self.have[index])

    def usable(self, now: float) -> bool:
        return self.present and not self.dropped and self.retry_at <= now and self.inflight < PER_SOURCE


@dataclass(eq=False)
class _Job:
    source: _Source
    index: int
    received: int = 0


@dataclass
class Contributor:
    peer_id: str
    name: str
    bytes: int = 0


class SwarmProgress(Protocol):
    """Where a session reports progress (the download manager's Download)."""

    received: int
    speed: float
    contributors: dict[str, Contributor]


class SwarmSession:
    def __init__(
        self,
        state: SwarmState,
        part: Path,
        own_peer_id: str,
        peers: PeerTable,
        auth: Authenticator,
        seeds: Callable[[], list[Peer]],
        progress: SwarmProgress,
        stop: threading.Event,
    ):
        self.state = state
        self.info = state.info
        self.part = part
        self.peers = peers
        self.auth = auth
        self.seeds = seeds
        self.progress = progress
        self.stop = stop
        self.started_with = state.have_bytes
        n = self.info.count
        seed_value = int.from_bytes(hashlib.sha256((own_peer_id + self.info.file_id).encode()).digest()[:8], "big")
        self._cursor = seed_value % n
        self._rng = random.Random(seed_value)
        self._sources: dict[str, _Source] = {}
        self._inflight: dict[int, _Job] = {}
        self._order: list[int] = list(range(n))
        self._elsewhere = bytearray(n)  # a fellow downloader has or is fetching it: don't ask a sharer
        self._has_fellows = False  # other downloaders of this file are online
        self._lock = threading.Lock()
        self._done = threading.Event()  # tells workers and the refresher to wind down
        self._client = httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT, connect=5.0),
            limits=httpx.Limits(max_connections=WORKERS * 2 + 8),
            trust_env=False,
        )
        self._speed_window = (time.monotonic(), 0)
        self._transferred = 0  # every byte received, kept or not (for the speed shown)
        self._fatal: OSError | None = None

    # ----- main loop ----------------------------------------------------------------------

    def run(self) -> None:
        try:
            self._refresh()
            if not any(s.present for s in self._sources.values()):
                raise SwarmFailed("No online peer has this file.")
            refresher = threading.Thread(target=self._refresh_loop, name="swarm-refresh", daemon=True)
            refresher.start()
            with ThreadPoolExecutor(WORKERS, thread_name_prefix="swarm") as pool:
                try:
                    self._loop(pool)
                finally:
                    self._done.set()  # in-flight requests stop at their next chunk
        finally:
            self._done.set()
            self._client.close()

    def _loop(self, pool: ThreadPoolExecutor) -> None:
        futures: dict[Future, _Job] = {}
        stuck_since: float | None = None
        n = self.info.count
        while True:
            if self.stop.is_set():
                raise SwarmStopped()
            if self._fatal is not None:
                raise self._fatal
            if self.state.have_count == n:
                return
            with self._lock:
                for job in self._assign(len(futures)):
                    futures[pool.submit(self._fetch, job)] = job
            if futures:
                stuck_since = None
                done, _ = wait(futures, timeout=0.25, return_when=FIRST_COMPLETED)
                for fut in done:
                    self._settle(futures.pop(fut), fut)
            else:
                now = time.monotonic()
                stuck_since = stuck_since or now
                self._check_stuck(now - stuck_since)
                time.sleep(0.1)
            self._report()

    # ----- choosing what to request from whom -------------------------------------------------

    def _assign(self, busy: int) -> list[_Job]:
        jobs: list[_Job] = []
        now = time.monotonic()
        have, elsewhere = self.state.have, self._elsewhere
        # While some pieces exist only at the sharers, their upload goes to those pieces alone.
        spare_sharers = any(not have[i] and not elsewhere[i] and i not in self._inflight
                            for i in range(self.info.count))
        while busy + len(jobs) < WORKERS:
            usable = [s for s in self._sources.values() if s.usable(now)]
            if not usable:
                break
            # Least busy first; spare the sharer when a fellow downloader has the piece too.
            usable.sort(key=lambda s: (s.inflight, s.seed, s.failures, self._rng.random()))
            job = None
            for source in usable:
                index = self._rarest_for(source, spare_sharers)
                if index is not None:
                    job = _Job(source, index)
                    break
            if job is None:
                break
            job.source.inflight += 1
            self._inflight[job.index] = job
            self.state.set_pending(job.index, True)
            jobs.append(job)
        return jobs

    def _rarest_for(self, source: _Source, spare_sharers: bool) -> int | None:
        have = self.state.have
        if source.seed and spare_sharers and self._has_fellows:
            # Pieces only the sharers have: pick one at random, so fellow downloaders asking
            # at the same moment rarely pick the same one.
            elsewhere = self._elsewhere
            choices = [i for i in range(self.info.count)
                       if not have[i] and not elsewhere[i] and i not in self._inflight]
            return self._rng.choice(choices) if choices else None
        for i in self._order:  # sorted rarest first, then from our starting point onwards
            if not have[i] and i not in self._inflight and source.has(i):
                return i
        return None

    def _reorder(self) -> None:
        n = self.info.count
        avail = [0] * n
        elsewhere = bytearray(n)
        for s in self._sources.values():
            if not s.present or s.dropped:
                continue
            if s.have is None:
                avail = [a + 1 for a in avail]
            else:
                avail = [a + b for a, b in zip(avail, s.have)]
            if not s.seed and s.failures < MAX_FAILURES:
                if s.have is None:
                    elsewhere = bytearray(b"\x01" * n)
                else:
                    pending = s.pending or bytearray(n)
                    elsewhere = bytearray(e | h | q for e, h, q in zip(elsewhere, s.have, pending))
        self._elsewhere = elsewhere
        self._has_fellows = any(not s.seed and s.present and not s.dropped for s in self._sources.values())
        cursor = self._cursor
        self._order = sorted(range(n), key=lambda i: (avail[i], (i - cursor) % n))

    # ----- fetching one piece -------------------------------------------------------------------

    def _fetch(self, job: _Job) -> None:
        source, index = job.source, job.index
        start, length = self.info.span(index)
        file_id = self.info.file_id
        if source.seed:
            path = f"/api/download/{file_id}"
            headers = self.auth.sign_request("GET", path)
            headers["Range"] = f"bytes={start}-{start + length - 1}"
            headers["If-Range"] = f'"{file_id}"'
        else:
            path = f"/api/piece/{file_id}/{index}"
            headers = self.auth.sign_request("GET", path)
        digest = hashlib.sha256()
        with self._client.stream("GET", f"{source.peer.base_url()}{path}", headers=headers) as resp:
            if not self.auth.verify_response(headers, resp.headers):
                raise _Gone("failed the passphrase check")
            if resp.status_code in (404, 409, 410):
                raise _Gone("no longer offers this file" if resp.status_code != 409 else "the file changed")
            if source.seed:
                expected = f"bytes {start}-{start + length - 1}/{self.info.size}"
                if resp.status_code != 206 or resp.headers.get("content-range") != expected:
                    raise _Gone(f"did not send the requested part (HTTP {resp.status_code})")
            elif resp.status_code != 200:
                raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
            try:
                f = open(self.part, "r+b")
            except OSError as exc:
                raise _LocalError(exc) from exc
            with f:
                f.seek(start)
                for chunk in resp.iter_raw():
                    if self._done.is_set() or self.stop.is_set():
                        raise SwarmStopped()
                    if job.received + len(chunk) > length:
                        raise _BadPiece("more data than the piece size")
                    try:
                        f.write(chunk)
                    except OSError as exc:
                        raise _LocalError(exc) from exc
                    digest.update(chunk)
                    job.received += len(chunk)
                    with self._lock:
                        self._transferred += len(chunk)
        if job.received != length:
            raise httpx.ReadError(f"short piece ({job.received} of {length} bytes)")
        if digest.digest() != self.info.digest(index):
            raise _BadPiece("piece failed SHA-256 verification")

    def _settle(self, job: _Job, fut: Future) -> None:
        source = job.source
        exc = fut.exception()
        with self._lock:
            source.inflight -= 1
            self._inflight.pop(job.index, None)
            if exc is not None:
                self.state.set_pending(job.index, False)
            if exc is None:
                self.state.mark(job.index)
                source.failures = 0
                source.bytes += job.received
                contributor = self.progress.contributors.setdefault(
                    source.peer.peer_id, Contributor(source.peer.peer_id, source.peer.name))
                contributor.name = source.peer.name
                contributor.bytes += job.received
                return
            if isinstance(exc, SwarmStopped):
                return
            if isinstance(exc, _LocalError):
                self._fatal = exc.error
                return
            if isinstance(exc, _BadPiece):
                source.bad_pieces += 1
                source.last_error = str(exc)
                source.retry_at = time.monotonic() + BAD_PIECE_REST  # no new requests while we see
                log.warning("piece %d of %s from %s: %s", job.index, self.info.file_id[:12], source.peer.name, exc)
                if source.bad_pieces >= MAX_BAD_PIECES:
                    source.dropped = True
                    self._reorder()
                return
            if isinstance(exc, _Gone):
                source.dropped = True
                source.last_error = str(exc)
                log.info("swarm: %s %s", source.peer.name, exc)
                self._reorder()
                return
            if not isinstance(exc, httpx.HTTPError):
                log.error("swarm request to %s failed", source.peer.name, exc_info=exc)
            source.failures += 1
            source.last_error = describe_http_error(exc) if isinstance(exc, Exception) else str(exc)
            rest = FAILURE_REST if source.failures >= MAX_FAILURES else min(8.0, 2.0 ** (source.failures - 1))
            source.retry_at = time.monotonic() + rest

    # ----- giving up -----------------------------------------------------------------------------

    def _check_stuck(self, stuck_for: float) -> None:
        """Nothing in flight and nothing to request: decide whether to wait or stop."""
        with self._lock:
            live = [s for s in self._sources.values() if s.present and not s.dropped]
            missing = [i for i in range(self.info.count) if not self.state.have[i]]
            available = [s for s in live if any(s.has(i) for i in missing)]
            reachable = [s for s in available if s.failures < MAX_FAILURES]
            if reachable:
                return  # resting briefly after an error; it will be asked again
            bad = [s for s in self._sources.values() if s.bad_pieces >= MAX_BAD_PIECES]
            failed = [s for s in self._sources.values() if s.failures >= MAX_FAILURES or not s.present]
        if bad and not available:
            raise SwarmUnusable(f"pieces from {_names(bad)} failed SHA-256 verification")
        if failed and not live:
            raise SwarmFailed(self._offline_message(failed))
        if available:  # every peer with the missing pieces is unreachable
            raise SwarmFailed(self._offline_message(available))
        if not live:
            raise SwarmFailed("No online peer has this file.")
        if stuck_for >= STUCK_TIMEOUT:
            raise SwarmFailed(
                f"The peers still online don't have the rest of this file ({self._percent()} done). "
                "The partial file was kept; press Resume to continue."
            )

    def _offline_message(self, sources: list[_Source]) -> str:
        error = next((s.last_error for s in sources if s.last_error), "no answer")
        if self.state.have_bytes > self.started_with:
            return (
                f"{_names(sources)} went offline during the transfer ({self._percent()} done, {error}). "
                "The partial file was kept; press Resume to continue."
            )
        return f"Could not reach {_names(sources)}: {error}."

    def _percent(self) -> str:
        return f"{self.state.have_bytes / self.info.size:.0%}" if self.info.size else "0%"

    # ----- knowing who has what -----------------------------------------------------------------

    def _refresh_loop(self) -> None:
        while not self._done.wait(REFRESH_INTERVAL):
            try:
                self._refresh()
            except Exception:
                log.exception("swarm refresh failed")

    def _refresh(self) -> None:
        file_id = self.info.file_id
        seeds = {p.peer_id: p for p in self.seeds()}
        others = [p for p in self.peers.online()
                  if p.swarm and file_id in p.swarm_files and p.peer_id not in seeds]
        haves: dict[str, tuple[bytearray | None, bytearray | None]] = {}
        for peer in others:
            found = self._fetch_have(peer)
            if found is not None:
                haves[peer.peer_id] = found
        with self._lock:
            for source in self._sources.values():
                source.present = source.peer.peer_id in seeds or source.peer.peer_id in haves
            for peer_id, peer in seeds.items():
                source = self._sources.get(peer_id)
                if source is None or not source.seed:
                    self._sources[peer_id] = _Source(peer, seed=True)
                else:
                    source.peer = peer
            for peer in others:
                if peer.peer_id not in haves:
                    continue
                source = self._sources.get(peer.peer_id)
                if source is None:
                    source = self._sources[peer.peer_id] = _Source(peer, seed=False)
                source.peer = peer
                source.have, source.pending = haves[peer.peer_id]
            self._reorder()

    def _fetch_have(self, peer: Peer) -> tuple[bytearray | None, bytearray | None] | None:
        """A fellow downloader's (have, pending) maps; have is None if it has everything.

        None if it can't tell us right now.
        """
        path = f"/api/swarm/{self.info.file_id}"
        headers = self.auth.sign_request("GET", path)
        try:
            resp = self._client.get(f"{peer.base_url()}{path}", headers=headers, timeout=3.0)
            if resp.status_code != 200 or not self.auth.verify_response(headers, resp.headers):
                return None
            data = resp.json()
            if data.get("pieces") != self.info.count:
                return None
            if data.get("complete") is True:
                return None, None
            pending = data.get("pending")
            return (decode_bits(data["have"], self.info.count),
                    decode_bits(pending, self.info.count) if isinstance(pending, str) else None)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return None

    # ----- progress -------------------------------------------------------------------------------

    def _report(self) -> None:
        with self._lock:
            in_flight = sum(j.received for j in self._inflight.values())
            transferred = self._transferred
        self.progress.received = self.state.have_bytes + in_flight
        began, count = self._speed_window
        elapsed = time.monotonic() - began
        if elapsed >= 0.5:
            rate = (transferred - count) / elapsed
            self.progress.speed = rate if not self.progress.speed else 0.6 * rate + 0.4 * self.progress.speed
            self._speed_window = (time.monotonic(), transferred)


def _names(sources: list[_Source]) -> str:
    names = sorted({s.peer.name for s in sources})
    if len(names) <= 1:
        return names[0] if names else "the other peers"
    return ", ".join(names[:-1]) + " and " + names[-1]


def open_partial(part: Path, size: int) -> None:
    """Create the partial file (sparse where supported) or trim one that is too long."""
    part.parent.mkdir(parents=True, exist_ok=True)
    with open(part, "ab") as f:
        make_sparse(f)
        if os.fstat(f.fileno()).st_size > size:
            f.truncate(size)
