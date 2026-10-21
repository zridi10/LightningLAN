"""Downloading files from other peers.

Each download runs in its own thread (at most MAX_ACTIVE_DOWNLOADS at a time) and writes into
downloads/.lightninglan-partial/<sha256>.part. Files of more than one piece are fetched as a swarm
(see swarm.py): pieces come in parallel from every peer that has them and are passed on to others
while the download runs. Peers without swarm support (LightningLAN 1.0) send the file whole, as
one stream that an HTTP Range request resumes.

Either way the SHA-256 of the whole file is checked before the .part file is renamed into place;
on a mismatch the data is discarded. If a peer disappears, the download carries on from the
others, or stops with the verified data kept so it can be resumed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .auth import Authenticator
from .fsutil import format_size, free_space, move_without_overwrite
from .pathsafe import display_name, safe_local_filename
from .peers import Peer, PeerTable, RemoteFile, describe_http_error
from .pieces import PieceInfo, swarmable
from .swarm import (
    Contributor,
    SwarmFailed,
    SwarmSession,
    SwarmState,
    SwarmStopped,
    SwarmUnusable,
    open_partial,
    scan_partial,
)

log = logging.getLogger(__name__)

PARTIAL_DIR_NAME = ".lightninglan-partial"
CHUNK = 512 * 1024
READ_TIMEOUT = 15.0
MAX_ACTIVE_DOWNLOADS = 3  # more wait their turn as "queued"
MAX_PIECE_LIST_BYTES = 2 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

QUEUED, DOWNLOADING, VERIFYING, COMPLETED, PAUSED, FAILED, CANCELED = (
    "queued", "downloading", "verifying", "completed", "paused", "failed", "canceled",
)
ACTIVE_STATES = {QUEUED, DOWNLOADING, VERIFYING}


class DownloadError(Exception):
    """A request to the download manager could not be carried out (message is user-facing)."""

    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.status = status  # HTTP status the API should answer with


class _Stop(Exception):
    pass


class _SourceFailed(Exception):
    """This peer could not deliver the file; another source may still work."""


class _Corrupt(Exception):
    pass


@dataclass
class Download:
    id: str
    file_id: str
    name: str
    size: int
    directory: Path  # the downloads folder when this download started; it finishes there
    preferred_peer: str | None = None
    status: str = QUEUED
    received: int = 0
    resumed_from: int = 0
    source_peer_id: str | None = None
    source_name: str | None = None
    error: str | None = None
    note: str | None = None
    path: str | None = None
    speed: float = 0.0
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    piece_info: PieceInfo | None = field(default=None, repr=False)
    swarm_state: SwarmState | None = field(default=None, repr=False)
    contributors: dict[str, Contributor] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "file_id": self.file_id,
            "name": display_name(self.name),
            "size": self.size,
            "received": self.received,
            "progress": (self.received / self.size) if self.size else (1.0 if self.status == COMPLETED else 0.0),
            "status": self.status,
            "resumed_from": self.resumed_from,
            "source": self._source(),
            "error": self.error,
            "note": self.note,
            "path": self.path,
            "speed": round(self.speed),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "swarm": self._swarm(),
        }

    def _peers(self) -> list[Contributor]:
        return sorted(list(self.contributors.values()), key=lambda c: -c.bytes)

    def _source(self) -> dict | None:
        peers = self._peers()
        if peers:  # a swarm download: the peer that sent the most
            return {"peer_id": peers[0].peer_id, "name": peers[0].name}
        if self.source_peer_id:
            return {"peer_id": self.source_peer_id, "name": self.source_name}
        return None

    def _swarm(self) -> dict | None:
        state = self.swarm_state
        if state is None:
            return None
        return {
            "pieces": state.info.count,
            "have": state.have_count,
            "peers": [{"peer_id": c.peer_id, "name": c.name, "bytes": c.bytes} for c in self._peers()],
        }


class DownloadManager:
    def __init__(self, downloads_dir: Path, auth: Authenticator, peers: PeerTable):
        self.downloads_dir = downloads_dir  # where new downloads go
        self.auth = auth
        self.peers = peers
        self.swarm_version = 0  # bumps when the set of files we can pass pieces of changes
        self._downloads: dict[str, Download] = {}
        self._stops: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._swarm: dict[str, SwarmState] = {}
        self._slots = threading.Semaphore(MAX_ACTIVE_DOWNLOADS)
        self._lock = threading.Lock()

    # ----- public API (called from the web server) --------------------------------------

    def list(self) -> list[dict]:
        with self._lock:
            items = list(self._downloads.values())
        return [d.to_dict() for d in sorted(items, key=lambda d: d.created_at, reverse=True)]

    def get(self, download_id: str) -> Download:
        d = self._downloads.get(download_id)
        if d is None:
            raise DownloadError("no such download")
        return d

    def start(self, file_id: str, peer_id: str | None = None) -> Download:
        if not isinstance(file_id, str) or not _SHA256_RE.match(file_id):
            raise DownloadError("invalid file id", 400)
        with self._lock:
            same_file = [d for d in self._downloads.values() if d.file_id == file_id]
            for d in same_file:
                if d.status in ACTIVE_STATES:
                    return d
            sources = self.sources(file_id)
            if not sources:
                raise DownloadError("No online peer has this file.")
            for d in same_file:
                # Downloading a paused or failed file again resumes that entry and its partial data.
                if d.status in (PAUSED, FAILED) and not self._is_running(d.id):
                    d.error = d.note = None
                    d.status = QUEUED
                    d.preferred_peer = peer_id or d.preferred_peer
                    self._launch(d)
                    return d
            remote = next((f for p, f in sources if p.peer_id == peer_id), sources[0][1])
            d = Download(uuid.uuid4().hex[:12], file_id, remote.name, remote.size, self.downloads_dir,
                         preferred_peer=peer_id)
            self._downloads[d.id] = d
            self._launch(d)
        return d

    def pause(self, download_id: str) -> Download:
        d = self.get(download_id)
        if d.status in ACTIVE_STATES:
            self._stop_thread(d.id, PAUSED)
        return d

    def resume(self, download_id: str) -> Download:
        d = self.get(download_id)
        if self._is_running(download_id):
            raise DownloadError("the download is still stopping; try again in a moment", 409)
        with self._lock:
            if any(o.id != d.id and o.file_id == d.file_id and o.status in ACTIVE_STATES
                   for o in self._downloads.values()):
                raise DownloadError("this file is already being downloaded", 409)
            if d.status in (PAUSED, FAILED):
                d.error = d.note = None
                d.status = QUEUED
                self._launch(d)
        return d

    def cancel(self, download_id: str) -> Download:
        d = self.get(download_id)
        if d.status in ACTIVE_STATES:
            self._stop_thread(d.id, CANCELED)
        if d.status in (PAUSED, FAILED):
            d.status = CANCELED
            self._discard_partial(d)
        return d

    def remove(self, download_id: str) -> None:
        d = self.get(download_id)
        if d.status in ACTIVE_STATES:
            raise DownloadError("cancel or pause the download first", 409)
        if d.status in (PAUSED, FAILED):
            self._discard_partial(d)
        self._stop_passing_on(d)
        with self._lock:
            self._downloads.pop(download_id, None)

    def clear_finished(self) -> None:
        with self._lock:
            finished = [d for d in self._downloads.values() if d.status in (COMPLETED, CANCELED)]
            for d in finished:
                del self._downloads[d.id]
        for d in finished:
            self._stop_passing_on(d)

    def pause_all(self) -> list[str]:
        """Pause every queued or running download. Returns their ids."""
        with self._lock:
            active = [d for d in self._downloads.values() if d.status in ACTIVE_STATES]
        for d in active:  # tell them all first, then wait for each
            stop = self._stops.get(d.id)
            if stop is not None:
                stop.reason = PAUSED  # type: ignore[attr-defined]
                stop.set()
        for d in active:
            self._stop_thread(d.id, PAUSED)
        return [d.id for d in active]

    def resume_all(self, download_ids: list[str] | None = None) -> None:
        """Resume the paused downloads (all of them, or those listed) that can be resumed now."""
        with self._lock:
            paused = [d.id for d in self._downloads.values() if d.status == PAUSED]
        for download_id in paused:
            if download_ids is None or download_id in download_ids:
                try:
                    self.resume(download_id)
                except DownloadError:
                    pass  # still stopping, or the same file is downloading under another entry

    def find(self, download_id: str) -> Download | None:
        return self._downloads.get(download_id)

    # ----- swarm: what we can pass on to other peers --------------------------------------

    def swarm_files(self) -> list[str]:
        """Files we can serve pieces of: downloading now, or downloaded and still listed."""
        with self._lock:
            return sorted(self._swarm)

    def swarm_status(self, file_id: str) -> dict | None:
        state = self._swarm.get(file_id)
        return state.status() if state is not None else None

    def locate_piece(self, file_id: str, index: int) -> tuple[SwarmState, Path, int, int] | None:
        """Where to read a piece we hold, with a reader registered on its state (release it after)."""
        state = self._swarm.get(file_id)
        if state is None:
            return None
        found = state.locate(index)
        return (state, *found) if found is not None else None

    def piece_info_for(self, file_id: str) -> PieceInfo | None:
        """The piece list of a file we finished downloading (proven correct by its SHA-256)."""
        state = self._swarm.get(file_id)
        return state.info if state is not None and state.complete else None

    def _register(self, state: SwarmState) -> None:
        with self._lock:
            current = self._swarm.get(state.info.file_id)
            if current is not None and current is not state and current.complete and not state.complete:
                return  # keep passing on the finished copy
            self._swarm[state.info.file_id] = state
            self.swarm_version += 1

    def _unregister(self, state: SwarmState) -> None:
        with self._lock:
            if self._swarm.get(state.info.file_id) is state:
                del self._swarm[state.info.file_id]
                self.swarm_version += 1

    def _stop_passing_on(self, d: Download) -> None:
        if d.swarm_state is not None:
            self._unregister(d.swarm_state)

    def shutdown(self) -> None:
        for stop in list(self._stops.values()):
            stop.set()
        for thread in list(self._threads.values()):
            thread.join(timeout=5)

    def sources(self, file_id: str) -> list[tuple[Peer, RemoteFile]]:
        result = []
        for peer in self.peers.online():
            match = next((f for f in peer.files if f.sha256 == file_id), None)
            if match is not None:
                result.append((peer, match))
        return result

    # ----- worker -----------------------------------------------------------------------

    def _is_running(self, download_id: str) -> bool:
        thread = self._threads.get(download_id)
        return thread is not None and thread.is_alive()

    def _launch(self, d: Download) -> None:
        stop = threading.Event()
        stop.reason = None  # type: ignore[attr-defined]
        self._stops[d.id] = stop
        thread = threading.Thread(target=self._run, args=(d, stop), name=f"download-{d.id}", daemon=True)
        self._threads[d.id] = thread
        thread.start()

    def _stop_thread(self, download_id: str, reason: str) -> None:
        stop = self._stops.get(download_id)
        thread = self._threads.get(download_id)
        if stop is not None:
            stop.reason = reason  # type: ignore[attr-defined]
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

    def _partial_path(self, d: Download) -> Path:
        return d.directory / PARTIAL_DIR_NAME / f"{d.file_id}.part"

    def _discard_partial(self, d: Download) -> None:
        with self._lock:
            others = list(self._downloads.values())
        if any(o.file_id == d.file_id and o.status in ACTIVE_STATES for o in others):
            return
        state, d.swarm_state = d.swarm_state, None
        if state is not None:
            self._unregister(state)
        try:
            if state is not None:
                with state.exclusive():
                    self._partial_path(d).unlink()
            else:
                self._partial_path(d).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete partial file: %s", exc)

    def _run(self, d: Download, stop: threading.Event) -> None:
        part = self._partial_path(d)
        has_slot = False
        try:
            has_slot = self._wait_for_slot(d, stop)
            part.parent.mkdir(parents=True, exist_ok=True)
            info = self._piece_info(d)
            if info is None or not self._swarm_download(d, info, part, stop):
                self._whole_file_download(d, part, stop)
            self._finish(d, part)
        except _Stop:
            reason = getattr(stop, "reason", None) or PAUSED
            d.status = reason
            d.speed = 0
            d.note = None
            if reason == CANCELED:
                self._discard_partial(d)
        except (_Corrupt, _SourceFailed) as exc:
            d.status, d.error, d.note, d.speed = FAILED, str(exc), None, 0
        except OSError as exc:
            log.warning("download %s failed: %s", d.name, exc)
            d.status, d.error, d.note, d.speed = FAILED, f"Could not write the file: {exc.strerror or exc}", None, 0
        except Exception as exc:
            log.exception("download %s failed", d.name)
            d.status, d.error, d.note, d.speed = FAILED, f"Unexpected error: {exc}", None, 0
        finally:
            if has_slot:
                self._slots.release()
            self._stops.pop(d.id, None)
            self._threads.pop(d.id, None)

    def _wait_for_slot(self, d: Download, stop: threading.Event) -> bool:
        if self._slots.acquire(blocking=False):
            return True
        d.status = QUEUED
        d.note = "Waiting for another download to finish…"
        while not self._slots.acquire(timeout=0.25):
            if stop.is_set():
                raise _Stop()
        d.note = None
        return True

    # ----- swarm download ---------------------------------------------------------------

    def _piece_info(self, d: Download) -> PieceInfo | None:
        """The file's piece list from a peer sharing it, or None to download it whole."""
        if d.piece_info is not None:
            return d.piece_info
        if not swarmable(d.size):
            return None
        sharers = [p for p, _ in self.sources(d.file_id) if p.swarm]
        sharers.sort(key=lambda p: p.peer_id != d.preferred_peer)
        path = f"/api/pieces/{d.file_id}"
        for peer in sharers:
            headers = self.auth.sign_request("GET", path)
            try:
                with httpx.stream("GET", f"{peer.base_url()}{path}", headers=headers, timeout=10.0,
                                  trust_env=False) as resp:
                    if resp.status_code != 200 or not self.auth.verify_response(headers, resp.headers):
                        continue
                    body = b""
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > MAX_PIECE_LIST_BYTES:
                            raise ValueError("piece list too large")
                d.piece_info = PieceInfo.from_json(json.loads(body), d.file_id, d.size)
                return d.piece_info
            except (httpx.HTTPError, ValueError) as exc:
                log.info("no piece list from %s: %s", peer.name, exc)
        return None

    def _swarm_download(self, d: Download, info: PieceInfo, part: Path, stop: threading.Event) -> bool:
        """Fetch all pieces and check the whole file. False: fall back to a whole-file download."""
        state = d.swarm_state
        open_partial(part, d.size)
        if state is None or state.info != info:
            if part.stat().st_size:
                d.status, d.note = VERIFYING, "Checking the partial file…"
                have = scan_partial(part, info, stop)
                d.note = None
            else:
                have = bytearray(info.count)
            state = d.swarm_state = SwarmState(info, part, have)
        self._register(state)
        needed = d.size - state.have_bytes
        free = free_space(d.directory)
        if free is not None and free < needed:
            raise _SourceFailed(f"Not enough disk space: {format_size(needed)} needed, {format_size(free)} free.")
        d.status = DOWNLOADING
        d.received = state.have_bytes
        if d.received:
            d.resumed_from = d.received  # verified data the download continues from
        d.speed = 0
        session = SwarmSession(
            state, part, self.peers.own_peer_id, self.peers, self.auth,
            lambda: [p for p, _ in self.sources(d.file_id)], d, stop,
        )
        try:
            session.run()
        except SwarmStopped:
            d.received = state.have_bytes
            raise _Stop() from None
        except SwarmFailed as exc:
            d.received = state.have_bytes
            raise _SourceFailed(str(exc)) from None
        except SwarmUnusable as exc:
            log.warning("swarm download of %s: %s; downloading the whole file instead", d.name, exc)
            self._unregister(state)
            with state.exclusive():
                with open(part, "r+b") as f:
                    f.truncate(state.prefix_bytes())  # keep the verified start for a Range resume
            d.swarm_state, d.piece_info = None, None
            d.note = "Some pieces failed their checks; downloading the file in one piece instead…"
            return False
        d.received = d.size
        d.speed = 0
        if not self._verify(d, part):
            self._unregister(state)
            d.swarm_state = None
            with state.exclusive():
                part.unlink(missing_ok=True)
            names = ", ".join(c.name for c in d._peers()) or "the other peers"
            raise _Corrupt(f"The file received from {names} failed SHA-256 verification and was discarded.")
        return True

    # ----- whole-file download (single source, resumed with Range) -------------------------

    def _whole_file_download(self, d: Download, part: Path, stop: threading.Event) -> None:
        tried: set[str] = set()
        corrupt_from: list[str] = []
        last_error = "No online peer has this file."
        while True:
            peer = self._pick_source(d, tried)
            if peer is None:
                if corrupt_from:
                    raise _Corrupt(
                        f"The file received from {', '.join(corrupt_from)} failed SHA-256 verification "
                        "and was discarded."
                    )
                raise _SourceFailed(last_error)
            d.source_peer_id, d.source_name = peer.peer_id, peer.name
            try:
                self._transfer_verified(d, peer, part, stop)
                return
            except _SourceFailed as exc:
                tried.add(peer.peer_id)
                last_error = str(exc)
                d.note = f"{last_error} Trying another peer…"
            except _Corrupt:
                tried.add(peer.peer_id)
                corrupt_from.append(peer.name)

    def _pick_source(self, d: Download, tried: set[str]) -> Peer | None:
        candidates = [p for p, _ in self.sources(d.file_id) if p.peer_id not in tried]
        for peer_id in (d.preferred_peer, d.source_peer_id):
            for p in candidates:
                if p.peer_id == peer_id:
                    return p
        return candidates[0] if candidates else None

    def _transfer_verified(self, d: Download, peer: Peer, part: Path, stop: threading.Event) -> None:
        """Transfer from one peer; if a resumed file fails verification, retry once from zero."""
        resumed = self._transfer(d, peer, part, stop)
        if self._verify(d, part):
            return
        part.unlink(missing_ok=True)
        if resumed:
            d.note = "The partial file was damaged; downloading again from the start…"
            self._transfer(d, peer, part, stop)
            if self._verify(d, part):
                return
            part.unlink(missing_ok=True)
        raise _Corrupt()

    def _transfer(self, d: Download, peer: Peer, part: Path, stop: threading.Event) -> bool:
        """Bring `part` up to d.size bytes from `peer`. Returns True if it resumed existing data."""
        offset = part.stat().st_size if part.exists() else 0
        if offset > d.size:
            part.unlink()
            offset = 0
        free = free_space(d.directory)
        if free is not None and free < d.size - offset:
            raise _SourceFailed(f"Not enough disk space: {format_size(d.size - offset)} needed, {format_size(free)} free.")
        d.status = DOWNLOADING
        d.received = offset
        if offset:
            d.resumed_from = offset  # where the most recent resume picked up
        if offset >= d.size:
            return offset > 0

        path = f"/api/download/{d.file_id}"
        url = f"{peer.base_url()}{path}"
        headers = self.auth.sign_request("GET", path)
        if offset:
            headers["Range"] = f"bytes={offset}-"
            headers["If-Range"] = f'"{d.file_id}"'
        timeout = httpx.Timeout(READ_TIMEOUT, connect=5.0)
        start_offset = offset
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                with client.stream("GET", url, headers=headers) as resp:
                    if not self.auth.verify_response(headers, resp.headers):
                        raise _SourceFailed(f"{peer.name} failed the passphrase check.")
                    if resp.status_code == 404:
                        raise _SourceFailed(f"{peer.name} no longer shares this file.")
                    if resp.status_code == 409:
                        raise _SourceFailed(f"The file changed on {peer.name}.")
                    if resp.status_code == 416:
                        part.unlink(missing_ok=True)
                        raise _SourceFailed(f"{peer.name} rejected the resume request.")
                    if resp.status_code == 206 and offset:
                        expected = f"bytes {offset}-{d.size - 1}/{d.size}"
                        if resp.headers.get("content-range") != expected:
                            raise _SourceFailed(f"{peer.name} sent an unexpected byte range.")
                    elif resp.status_code == 200:
                        offset = 0  # the peer ignored our Range header: start over
                        d.received = 0
                    else:
                        raise _SourceFailed(f"{peer.name} answered HTTP {resp.status_code}.")
                    self._receive(d, resp, part, offset, stop)
        except httpx.HTTPError as exc:
            done = f"{d.received / d.size:.0%}" if d.size else "0%"
            if d.received > start_offset:
                raise _SourceFailed(
                    f"{peer.name} went offline during the transfer ({done} done, {describe_http_error(exc)}). "
                    "The partial file was kept; press Resume to continue."
                ) from exc
            raise _SourceFailed(f"Could not reach {peer.name}: {describe_http_error(exc)}.") from exc
        if d.received != d.size:
            raise _SourceFailed(
                f"{peer.name} ended the transfer early ({format_size(d.received)} of {format_size(d.size)}). "
                "The partial file was kept; press Resume to continue."
            )
        return start_offset > 0 and offset > 0

    def _receive(self, d: Download, resp: httpx.Response, part: Path, offset: int, stop: threading.Event) -> None:
        window_start, window_bytes = time.monotonic(), 0
        with open(part, "r+b" if offset else "wb", buffering=CHUNK) as f:
            if offset:
                f.seek(offset)
                f.truncate()
            for chunk in resp.iter_raw():  # as it arrives: nothing is lost if the link drops
                if stop.is_set():
                    raise _Stop()
                if d.received + len(chunk) > d.size:
                    raise _SourceFailed(f"{d.source_name} sent more data than the file size.")
                f.write(chunk)
                d.received += len(chunk)
                window_bytes += len(chunk)
                elapsed = time.monotonic() - window_start
                if elapsed >= 0.5:
                    rate = window_bytes / elapsed
                    d.speed = rate if not d.speed else 0.6 * rate + 0.4 * d.speed
                    window_start, window_bytes = time.monotonic(), 0

    def _verify(self, d: Download, part: Path) -> bool:
        d.status = VERIFYING
        d.speed = 0
        digest = hashlib.sha256()
        with open(part, "rb") as f:
            while chunk := f.read(4 * CHUNK):
                digest.update(chunk)
        return digest.hexdigest() == d.file_id

    def _finish(self, d: Download, part: Path) -> None:
        state = d.swarm_state
        if state is not None:
            with state.exclusive():  # nobody may be reading a piece from the .part while it moves
                final = self._move_into_place(d, part)
            state.set_complete(final)  # and from now on, pieces are read from the saved file
            self._register(state)
        else:
            final = self._move_into_place(d, part)
        d.path = str(final)
        d.status = COMPLETED
        d.error = d.note = None
        d.received = d.size
        d.finished_at = time.time()
        source = d._source()
        log.info("downloaded %s from %s to %s", d.name, source["name"] if source else "?", final)

    def _move_into_place(self, d: Download, part: Path) -> Path:
        for attempt in range(20):
            try:
                return move_without_overwrite(part, d.directory, safe_local_filename(d.name))
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.25)  # Windows: a virus scanner or indexer may have the file open
        raise AssertionError("unreachable")
