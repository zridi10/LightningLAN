"""Getting a Steam game from other peers, straight into a Steam library on this computer.

The game's file list comes from one peer that shares it. It grows while that peer hashes the
files, and the copy starts with the first ones. The files come from every online peer sharing the
same build, several at a time, and each is checked against its SHA-256 before it takes its place
in steamapps/common/<installdir>. Steam's manifest for the game (steam.portable_manifest) is
written last, so Steam never sees a half-copied game: from then on it is in the library the next
time Steam starts.

Files already in the game's folder that match (from a copy that was paused, or one Steam left
behind) are kept, so a copy carries on where it stopped. Cancel deletes what this copy put there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

from .auth import Authenticator
from .fsutil import format_size, free_space, long_path
from .pathsafe import UnsafePathError, display_name, safe_join, split_relative, validate_component
from .peers import Peer, PeerTable, RemoteGame, describe_http_error
from .steam import PAGE_SIZE, PART_SUFFIX, STEAM_OS, SteamLibrary, portable_manifest, same_folder, sha256_file
from .transfers import ACTIVE_STATES, CANCELED, COMPLETED, DOWNLOADING, FAILED, PAUSED, QUEUED, DownloadError

log = logging.getLogger(__name__)

WORKERS = 4  # files of one game copied at the same time
MAX_ACTIVE_GAMES = 2  # more wait their turn as "queued"
CHUNK = 512 * 1024
READ_TIMEOUT = 15.0
LIST_POLL = 1.0  # how often a file list that is still growing is read again
MAX_LIST_BYTES = 16 * 1024 * 1024
MAX_FILES = 1_000_000
SOURCE_RETRY = 10.0  # a peer that failed is asked again after this long
NO_SOURCE_WAIT = 30.0  # how long a copy waits for someone sharing the game to come back
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
DONE_NOTE = "Start Steam, or restart it if it's open, and the game is in your library."
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OS_NAMES = {"windows": "Windows", "linux": "Linux", "mac": "Mac"}


class _Stop(Exception):
    pass


class _Failed(Exception):
    """The copy can't go on (message is user-facing); what it has is kept for a resume."""


class _SourceGone(Exception):
    """This peer couldn't send what was asked; another one may."""


@dataclass
class GameDownload:
    id: str
    appid: int
    name: str
    size: int
    library: Path
    preferred_peer: str | None = None
    status: str = QUEUED
    received: int = 0
    speed: float = 0.0
    files_done: int = 0
    files_total: int | None = None
    error: str | None = None
    note: str | None = None
    path: str | None = None  # the game's folder, once known
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    source_peer_id: str | None = None  # where the file list comes from
    source_name: str | None = None
    buildid: str | None = None
    installdir: str | None = None
    contributors: dict[str, list] = field(default_factory=dict, repr=False)  # peer_id -> [name, bytes sent]
    done: dict[str, str] = field(default_factory=dict, repr=False)  # relpath -> SHA-256, files in place
    written: set[str] = field(default_factory=set, repr=False)  # the files this copy put there

    @property
    def folder(self) -> Path | None:
        return self.library / "steamapps" / "common" / self.installdir if self.installdir else None

    def to_dict(self) -> dict:
        peers = sorted(({"peer_id": k, "name": v[0], "bytes": v[1]} for k, v in list(self.contributors.items())),
                       key=lambda p: -p["bytes"])
        if peers and peers[0]["bytes"]:
            source = {"peer_id": peers[0]["peer_id"], "name": peers[0]["name"]}
        elif self.source_peer_id:
            source = {"peer_id": self.source_peer_id, "name": self.source_name}
        else:
            source = None
        return {
            "id": self.id,
            "kind": "steam",
            "appid": self.appid,
            "file_id": None,
            "name": display_name(self.name),
            "size": self.size,
            "received": self.received,
            "progress": (self.received / self.size) if self.size else (1.0 if self.status == COMPLETED else 0.0),
            "status": self.status,
            "resumed_from": 0,
            "source": source,
            "error": self.error,
            "note": self.note,
            "path": self.path,
            "library": str(self.library),
            "speed": round(self.speed),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "swarm": None,
            "files": {"done": self.files_done, "total": self.files_total},
            "peers": peers,
        }


class GameDownloads:
    def __init__(self, steam: SteamLibrary, auth: Authenticator, peers: PeerTable):
        self.steam = steam
        self.auth = auth
        self.peers = peers
        self._downloads: dict[str, GameDownload] = {}
        self._stops: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._slots = threading.Semaphore(MAX_ACTIVE_GAMES)
        self._lock = threading.Lock()

    # ----- public API (called from the web server) --------------------------------------

    def list(self) -> list[dict]:
        with self._lock:
            items = list(self._downloads.values())
        return [d.to_dict() for d in items]

    def owns(self, download_id: str) -> bool:
        return download_id in self._downloads

    def find(self, download_id: str) -> GameDownload | None:
        return self._downloads.get(download_id)

    def get(self, download_id: str) -> GameDownload:
        d = self._downloads.get(download_id)
        if d is None:
            raise DownloadError("no such download")
        return d

    def sources(self, appid: int, buildid: str | None = None) -> list[tuple[Peer, RemoteGame]]:
        """Online peers sharing the game (that build of it, if given) for this kind of computer."""
        found = []
        for peer in self.peers.online():
            if peer.steam_os != STEAM_OS:
                continue
            game = next((g for g in peer.steam_games if g.appid == appid), None)
            if game is not None and (buildid is None or game.buildid == buildid):
                found.append((peer, game))
        return found

    def start(self, appid: int, library: str | None = None, peer_id: str | None = None) -> GameDownload:
        if not self.steam.found:
            raise DownloadError("Steam isn't installed on this computer. Install Steam first, then get the game.", 409)
        with self._lock:
            same = [d for d in self._downloads.values() if d.appid == appid]
            for d in same:
                if d.status in ACTIVE_STATES:
                    return d
            installed = self.steam.installed.get(appid)
            if installed is not None:
                raise DownloadError(f"{installed.name} is already in your Steam library.", 409)
            sources = self.sources(appid)
            if not sources:
                raise DownloadError(f"Nobody online shares this game for {_OS_NAMES.get(STEAM_OS, STEAM_OS)}.")
            for d in same:
                # Getting a paused or stopped game again carries on with what it has, where it is.
                if d.status in (PAUSED, FAILED) and not self._is_running(d.id):
                    d.error = d.note = None
                    d.status = QUEUED
                    d.preferred_peer = peer_id or d.preferred_peer
                    self._launch(d)
                    return d
            target = self._library(library)
            remote = next((g for p, g in sources if p.peer_id == peer_id), None) or max(
                (g for _, g in sources), key=lambda g: (int(g.buildid), g.updated))
            free = free_space(target)
            if free is not None and free < remote.size:
                raise DownloadError(f"Not enough space in {target}: {format_size(remote.size)} needed, "
                                    f"{format_size(free)} free.", 409)
            d = GameDownload(uuid.uuid4().hex[:12], appid, remote.name, remote.size, target, preferred_peer=peer_id)
            self._downloads[d.id] = d
            self._launch(d)
        log.info("getting %s into %s", d.name, target)
        return d

    def _library(self, chosen: str | None) -> Path:
        libraries = self.steam.libraries
        if not libraries:
            raise DownloadError("No Steam library found on this computer.", 409)
        if not chosen:
            return max(libraries, key=lambda lib: free_space(lib) or 0)
        for library in libraries:
            if same_folder(library, Path(chosen)):
                return library
        raise DownloadError("That isn't one of this computer's Steam libraries.", 400)

    def pause(self, download_id: str) -> GameDownload:
        d = self.get(download_id)
        if d.status in ACTIVE_STATES:
            self._stop_thread(d.id, PAUSED)
        return d

    def resume(self, download_id: str) -> GameDownload:
        d = self.get(download_id)
        if self._is_running(download_id):
            raise DownloadError("the download is still stopping; try again in a moment", 409)
        with self._lock:
            if d.status in (PAUSED, FAILED):
                if self.steam.installed.get(d.appid) is not None:
                    raise DownloadError(f"{d.name} is in your Steam library now.", 409)
                d.error = d.note = None
                d.status = QUEUED
                self._launch(d)
        return d

    def cancel(self, download_id: str) -> GameDownload:
        d = self.get(download_id)
        if d.status in ACTIVE_STATES:
            self._stop_thread(d.id, CANCELED)
        if d.status in (PAUSED, FAILED):
            d.status = CANCELED
            self._discard(d)
        return d

    def remove(self, download_id: str) -> None:
        d = self.get(download_id)
        if d.status in ACTIVE_STATES:
            raise DownloadError("cancel or pause the download first", 409)
        if d.status in (PAUSED, FAILED):
            self._discard(d)
        with self._lock:
            self._downloads.pop(download_id, None)

    def clear_finished(self) -> None:
        with self._lock:
            for d in [d for d in self._downloads.values() if d.status in (COMPLETED, CANCELED)]:
                del self._downloads[d.id]

    def pause_all(self) -> list[str]:
        with self._lock:
            active = [d for d in self._downloads.values() if d.status in ACTIVE_STATES]
        for d in active:
            stop = self._stops.get(d.id)
            if stop is not None:
                stop.reason = PAUSED  # type: ignore[attr-defined]
                stop.set()
        for d in active:
            self._stop_thread(d.id, PAUSED)
        return [d.id for d in active]

    def resume_all(self, download_ids: list[str] | None = None) -> None:
        with self._lock:
            paused = [d.id for d in self._downloads.values() if d.status == PAUSED]
        for download_id in paused:
            if download_ids is None or download_id in download_ids:
                try:
                    self.resume(download_id)
                except DownloadError:
                    pass

    def shutdown(self) -> None:
        for stop in list(self._stops.values()):
            stop.set()
        for thread in list(self._threads.values()):
            thread.join(timeout=5)

    # ----- worker -----------------------------------------------------------------------

    def _is_running(self, download_id: str) -> bool:
        thread = self._threads.get(download_id)
        return thread is not None and thread.is_alive()

    def _launch(self, d: GameDownload) -> None:
        stop = threading.Event()
        stop.reason = None  # type: ignore[attr-defined]
        self._stops[d.id] = stop
        thread = threading.Thread(target=self._run, args=(d, stop), name=f"steam-get-{d.appid}", daemon=True)
        self._threads[d.id] = thread
        thread.start()

    def _stop_thread(self, download_id: str, reason: str) -> None:
        stop = self._stops.get(download_id)
        thread = self._threads.get(download_id)
        if stop is not None:
            stop.reason = reason  # type: ignore[attr-defined]
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=20)

    def _run(self, d: GameDownload, stop: threading.Event) -> None:
        has_slot = False
        try:
            has_slot = self._wait_for_slot(d, stop)
            d.status = DOWNLOADING
            _Copy(self, d, stop).run()
            d.status, d.error, d.note, d.speed = COMPLETED, None, DONE_NOTE, 0
            d.received = d.size
            d.finished_at = time.time()
            log.info("%s is in the Steam library %s now", d.name, d.library)
        except _Stop:
            reason = getattr(stop, "reason", None) or PAUSED
            d.status, d.speed, d.note = reason, 0, None
            if reason == CANCELED:
                self._discard(d)
        except _Failed as exc:
            d.status, d.error, d.note, d.speed = FAILED, str(exc), None, 0
        except Exception as exc:
            log.exception("getting %s failed", d.name)
            d.status, d.error, d.note, d.speed = FAILED, f"Unexpected error: {exc}", None, 0
        finally:
            if has_slot:
                self._slots.release()
            self._stops.pop(d.id, None)
            self._threads.pop(d.id, None)

    def _wait_for_slot(self, d: GameDownload, stop: threading.Event) -> bool:
        if self._slots.acquire(blocking=False):
            return True
        d.status = QUEUED
        d.note = "Waiting for another game to finish…"
        while not self._slots.acquire(timeout=0.25):
            if stop.is_set():
                raise _Stop()
        d.note = None
        return True

    def _discard(self, d: GameDownload) -> None:
        """Delete what this copy put in the game's folder, and the folders that leaves empty."""
        folder = d.folder
        if folder is None:
            return
        installed = self.steam.installed.get(d.appid)
        if installed is not None and same_folder(installed.folder, folder):
            return  # Steam has the game there now
        for rel in list(d.written):
            try:
                os.unlink(long_path(safe_join(folder, rel)))
            except (OSError, UnsafePathError):
                pass
        d.written.clear()
        d.done.clear()
        for directory, _dirs, names in os.walk(long_path(folder), topdown=False):
            for name in names:
                if name.endswith(PART_SUFFIX):
                    try:
                        os.unlink(os.path.join(directory, name))
                    except OSError:
                        pass
            try:
                os.rmdir(directory)  # only if it is empty now
            except OSError:
                pass


class _Copy:
    """One run of a game download: until the game is in place, it is paused, or it can't go on."""

    def __init__(self, manager: GameDownloads, d: GameDownload, stop: threading.Event):
        self.m = manager
        self.d = d
        self.stop = stop
        self.folder: Path | None = None
        self.acf: str | None = None
        self.dirs: list[str] = []
        self.source: Peer | None = None  # where the file list comes from
        self.listed = 0  # entries of its list read so far
        self.complete = False
        self.seen: dict[str, str] = {}  # relpath -> SHA-256, every file the list has named
        self.queue: deque[tuple[str, int, str, bool]] = deque()
        self.busy = 0
        self.closing = False
        self.failure: Exception | None = None
        self.cond = threading.Condition()
        self.done_bytes = 0
        self.in_flight: dict[str, int] = {}  # relpath -> bytes of it received so far
        self.loads: dict[str, int] = {}  # peer_id -> files coming from it right now
        self.down_until: dict[str, float] = {}  # peer_id -> when to ask it again
        self.last_source = time.monotonic()
        self.space_checked = False
        self.net_bytes = 0
        self.window = (time.monotonic(), 0)
        d.files_done = 0
        d.received = 0

    def run(self) -> None:
        self._read_list()
        workers = [threading.Thread(target=self._work, name=f"steam-get-{self.d.appid}-{i}", daemon=True)
                   for i in range(WORKERS)]
        for worker in workers:
            worker.start()
        try:
            next_read = time.monotonic() + LIST_POLL
            while True:
                with self.cond:
                    if self.failure is not None or self.stop.is_set():
                        break
                    if self.complete and not self.queue and not self.busy:
                        break
                    self.cond.wait(0.25)
                if not self.complete and time.monotonic() >= next_read:
                    self._read_list()
                    next_read = time.monotonic() + LIST_POLL
                self._measure_speed()
        finally:
            with self.cond:
                self.closing = True
                self.cond.notify_all()
            for worker in workers:
                worker.join()
        if self.stop.is_set():
            raise _Stop()
        if self.failure is not None:
            raise self.failure
        self._finish()

    # ----- the file list ------------------------------------------------------------------

    def _read_list(self) -> None:
        """Read what the list has that this copy hasn't seen yet, and queue those files."""
        while True:
            peer, payload = self._fetch_list()
            files = self._take_list(peer, payload)
            if self.complete or len(files) < PAGE_SIZE:
                return  # caught up; the rest comes as it is hashed

    def _fetch_list(self) -> tuple[Peer, dict]:
        waiting_since = None
        while True:
            if self.stop.is_set():
                raise _Stop()
            peer = self._list_source()
            if peer is None:
                waiting_since = waiting_since or time.monotonic()
                if time.monotonic() - waiting_since > NO_SOURCE_WAIT:
                    raise _Failed(self._nobody_left())
                self.d.note = "Waiting for someone sharing it to come back…"
                self.stop.wait(1.0)
                continue
            path = f"/api/steam/{self.d.appid}"
            try:
                return peer, self._get_json(peer, path, {"since": self.listed})
            except _SourceGone as exc:
                log.info("no file list of %s from %s: %s", self.d.name, peer.name, exc)
                self.down_until[peer.peer_id] = time.monotonic() + SOURCE_RETRY
                self.source = None

    def _list_source(self) -> Peer | None:
        d = self.d
        now = time.monotonic()
        candidates = [p for p, _ in self.m.sources(d.appid, d.buildid) if self.down_until.get(p.peer_id, 0) <= now]
        current = self.source
        if current is not None and any(p.peer_id == current.peer_id for p in candidates):
            return current
        if not candidates:
            return None
        newest = {p.peer_id: g for p, g in self.m.sources(d.appid, d.buildid)}
        peer = next((p for p in candidates if p.peer_id == d.preferred_peer), None) or max(
            candidates, key=lambda p: (int(newest[p.peer_id].buildid), newest[p.peer_id].updated))
        if current is not None:
            log.info("file list of %s now comes from %s", d.name, peer.name)
        self.source, self.listed = peer, 0  # another peer's list is read from the start
        d.source_peer_id, d.source_name = peer.peer_id, peer.name
        return peer

    def _get_json(self, peer: Peer, path: str, params: dict) -> dict:
        headers = self.m.auth.sign_request("GET", path)
        try:
            with httpx.stream("GET", f"{peer.base_url()}{path}", params=params, headers=headers,
                              timeout=httpx.Timeout(READ_TIMEOUT, connect=5.0), trust_env=False) as resp:
                if not self.m.auth.verify_response(headers, resp.headers):
                    raise _SourceGone("it failed the passphrase check")
                if resp.status_code == 404:
                    raise _SourceGone("it doesn't share the game any more")
                if resp.status_code != 200:
                    raise _SourceGone(f"it answered HTTP {resp.status_code}")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_LIST_BYTES:
                        raise _SourceGone("its file list is too large")
        except httpx.HTTPError as exc:
            raise _SourceGone(describe_http_error(exc)) from exc
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise _SourceGone("its file list can't be read") from exc
        if not isinstance(payload, dict):
            raise _SourceGone("its file list can't be read")
        return payload

    def _take_list(self, peer: Peer, payload: dict) -> list:
        """Check a page of the file list from `peer` and queue its new files. Returns the page's files."""
        d = self.d
        error = payload.get("error")
        if isinstance(error, str) and error:
            self.down_until[peer.peer_id] = time.monotonic() + SOURCE_RETRY
            self.source = None
            raise _Failed(display_name(error)[:300])
        installdir, buildid, acf = payload.get("installdir"), payload.get("buildid"), payload.get("acf")
        files, dirs = payload.get("files"), payload.get("dirs")
        if (payload.get("appid") != d.appid or not isinstance(installdir, str) or not isinstance(buildid, str)
                or not buildid.isdigit() or not isinstance(acf, str) or not isinstance(files, list)
                or not isinstance(dirs, list) or payload.get("since") != self.listed):
            raise _Failed(f"{peer.name} sent a file list that doesn't make sense.")
        if d.installdir is not None and installdir != d.installdir:
            raise _Failed(f"{d.name} is in another folder on {peer.name} than it was. Remove this download and get "
                          "the game again.")
        if d.buildid is not None and buildid != d.buildid:
            raise _Failed(f"{d.name} was updated on {peer.name} since the copy started. Remove this download and get "
                          "the game again.")
        if self.acf is None:
            try:
                validate_component(installdir)
                self.acf = portable_manifest(acf, d.appid, installdir)
            except (UnsafePathError, ValueError) as exc:
                raise _Failed(f"{peer.name} sent a Steam manifest that can't be used: {exc}") from exc
            d.installdir, d.buildid = installdir, buildid
            self._open_folder()

        entries = []
        for item in files:
            entry = _file_entry(item)
            if entry is None:
                raise _Failed(f"{peer.name} sent a file list that doesn't make sense.")
            entries.append(entry)
        if len(self.seen) + len(entries) > MAX_FILES:
            raise _Failed(f"{d.name} has too many files.")
        self.listed += len(files)

        total_files, total_bytes = payload.get("total_files"), payload.get("total_bytes")
        if isinstance(total_files, int) and isinstance(total_bytes, int) and total_bytes >= 0:
            d.files_total = total_files
            d.size = max(total_bytes, d.received)
            self._check_space(total_bytes)
        complete = payload.get("complete") is True
        if complete:
            cleaned = []
            for rel in dirs[:MAX_FILES]:
                if not isinstance(rel, str):
                    raise _Failed(f"{peer.name} sent a file list that doesn't make sense.")
                try:
                    split_relative(rel)
                except UnsafePathError as exc:
                    raise _Failed(f"{peer.name} sent a file list that doesn't make sense: {exc}.") from exc
                cleaned.append(rel)
            self.dirs = cleaned
            d.note = None
        else:
            hashed = payload.get("hashed_bytes")
            if isinstance(hashed, int) and isinstance(total_bytes, int) and total_bytes > 0:
                pct = min(99, hashed * 100 // total_bytes)
                d.note = f"{peer.name} is checking the game's files ({pct}%); copying them as they're ready…"
            else:
                d.note = f"{peer.name} is listing the game's files…"

        with self.cond:
            for rel, size, sha, executable in entries:
                if self.seen.get(rel) == sha:
                    continue
                self.seen[rel] = sha
                self.queue.append((rel, size, sha, executable))
            self.complete = complete
            self.cond.notify_all()
        return files

    def _open_folder(self) -> None:
        d = self.d
        folder = d.folder
        assert folder is not None
        for other in self.m.steam.installed.values():
            if other.appid != d.appid and other.installdir.lower() == d.installdir.lower() \
                    and same_folder(other.library, d.library):
                raise _Failed(f"{other.name} is already in {folder}, the folder {d.name} goes in.")
        try:
            os.makedirs(long_path(folder), exist_ok=True)
        except OSError as exc:
            raise _Failed(f"Could not create {folder}: {exc.strerror or exc}") from exc
        self.folder = folder
        d.path = str(folder)

    def _check_space(self, total_bytes: int) -> None:
        if self.space_checked:
            return
        self.space_checked = True
        have = 0  # files this download put there before count as done
        for rel in self.d.done:
            try:
                have += os.stat(long_path(safe_join(self.folder, rel))).st_size
            except (OSError, UnsafePathError):
                pass
        needed = max(0, total_bytes - have)
        free = free_space(self.folder)
        if free is not None and free < needed:
            raise _Failed(f"Not enough space in {self.d.library}: {format_size(needed)} needed, "
                          f"{format_size(free)} free.")

    def _nobody_left(self) -> str:
        return (f"Nobody sharing {self.d.name} is online now. What's been copied is kept: press Resume to carry on "
                "when they're back.")

    # ----- copying the files ----------------------------------------------------------------

    def _work(self) -> None:
        client = httpx.Client(timeout=httpx.Timeout(READ_TIMEOUT, connect=5.0), trust_env=False)
        try:
            while True:
                with self.cond:
                    while not self.queue and not self.closing and not self.stop.is_set() and self.failure is None:
                        self.cond.wait(0.5)
                    if self.closing or self.stop.is_set() or self.failure is not None:
                        return
                    entry = self.queue.popleft()
                    self.busy += 1
                try:
                    self._copy_file(client, *entry)
                except _Stop:
                    return
                except Exception as exc:
                    if isinstance(exc, OSError):
                        exc = _Failed(f"Could not write {entry[0]}: {exc.strerror or exc}")
                    elif not isinstance(exc, _Failed):
                        log.exception("copying %s of %s failed", entry[0], self.d.name)
                        exc = _Failed(f"Unexpected error: {exc}")
                    with self.cond:
                        self.failure = self.failure or exc
                        self.cond.notify_all()
                    return
                finally:
                    with self.cond:
                        self.busy -= 1
                        self.cond.notify_all()
        finally:
            client.close()

    def _copy_file(self, client: httpx.Client, rel: str, size: int, sha: str, executable: bool) -> None:
        d = self.d
        try:
            target = safe_join(self.folder, rel)
        except UnsafePathError as exc:
            raise _Failed(f"The file list names a file that can't be saved ({rel}: {exc}).") from exc
        path = long_path(target)
        if self._already_there(rel, path, size, sha):
            self._file_done(rel, sha, size, wrote=False)
            return
        os.makedirs(long_path(target.parent), exist_ok=True)
        if size == 0:
            if sha != EMPTY_SHA256:
                raise _Failed(f"The file list says {rel} is empty, but its SHA-256 isn't that of an empty file.")
            open(path, "wb").close()
            self._file_done(rel, sha, size, wrote=True)
            return
        part = path + PART_SUFFIX
        mismatched: dict[str, str] = {}  # peer_id -> name, peers whose copy of it didn't match
        while True:
            if self.stop.is_set():
                raise _Stop()
            peer = self._pick_peer(mismatched)
            if peer is None:
                continue
            try:
                ok = self._transfer(client, peer, rel, size, sha, part)
            except _SourceGone as exc:
                log.info("%s of %s from %s: %s", rel, d.name, peer.name, exc)
                self.down_until[peer.peer_id] = time.monotonic() + SOURCE_RETRY
                continue
            if ok:
                break
            mismatched[peer.peer_id] = peer.name
        _replace(part, path)
        if executable and os.name != "nt":
            os.chmod(path, os.stat(path).st_mode | 0o111)
        self._file_done(rel, sha, size, wrote=True)

    def _already_there(self, rel: str, path: str, size: int, sha: str) -> bool:
        try:
            st = os.stat(path)
        except OSError:
            return False
        if st.st_size != size:
            return False
        if self.d.done.get(rel) == sha:
            return True  # put there by this download earlier
        try:
            return sha256_file(path, self.stop) == sha
        except InterruptedError:
            raise _Stop() from None
        except OSError:
            return False

    def _pick_peer(self, mismatched: dict[str, str]) -> Peer | None:
        """The least busy peer to copy a file from, or None after a short wait if there's none right now."""
        d = self.d
        now = time.monotonic()
        sharing = [p for p, _ in self.m.sources(d.appid, d.buildid)]
        ready = [p for p in sharing if p.peer_id not in mismatched and self.down_until.get(p.peer_id, 0) <= now]
        if ready:
            self.last_source = now
            source_id = self.source.peer_id if self.source is not None else None
            with self.cond:
                return min(ready, key=lambda p: (self.loads.get(p.peer_id, 0), p.peer_id != source_id))
        if sharing and all(p.peer_id in mismatched for p in sharing):
            names = ", ".join(mismatched.values())
            raise _Failed(f"The copy of {d.name} on {names} doesn't match its file list ({d.name}'s files may have "
                          "changed there). Press Resume to try again.")
        if now - self.last_source > NO_SOURCE_WAIT:
            raise _Failed(self._nobody_left())
        self.stop.wait(1.0)
        return None

    def _transfer(self, client: httpx.Client, peer: Peer, rel: str, size: int, sha: str, part: str) -> bool:
        """Bring `part` up to `size` bytes from `peer`. True if it then matches `sha`; a copy that doesn't
        is deleted. Raises _SourceGone if the peer couldn't send it (what arrived is kept)."""
        digest = hashlib.sha256()
        try:
            offset = os.path.getsize(part)
        except OSError:
            offset = 0
        if offset > size:
            os.unlink(part)
            offset = 0
        if offset:
            offset = _hash_prefix(part, offset, digest, self.stop)
        self._progress(rel, offset)

        path = f"/api/steam/{self.d.appid}/{rel}"
        headers = self.m.auth.sign_request("GET", path)  # signed as the server reads it: not percent-encoded
        if offset:
            headers["Range"] = f"bytes={offset}-"
        with self.cond:
            self.loads[peer.peer_id] = self.loads.get(peer.peer_id, 0) + 1
        try:
            with client.stream("GET", f"{peer.base_url()}{quote(path)}", headers=headers) as resp:
                if not self.m.auth.verify_response(headers, resp.headers):
                    raise _SourceGone("it failed the passphrase check")
                if resp.status_code == 404:
                    raise _SourceGone("it doesn't share that file")
                if resp.status_code == 409:
                    raise _SourceGone("the file changed there since it was listed")
                if resp.status_code == 416:
                    os.unlink(part)
                    raise _SourceGone("it refused to carry on from where the copy stopped")
                if resp.status_code == 206 and offset:
                    if resp.headers.get("content-range") != f"bytes {offset}-{size - 1}/{size}":
                        raise _SourceGone("it sent an unexpected part of the file")
                elif resp.status_code == 200:
                    offset = 0
                    digest = hashlib.sha256()
                else:
                    raise _SourceGone(f"it answered HTTP {resp.status_code}")
                received = offset
                with open(part, "r+b" if offset else "wb", buffering=CHUNK) as f:
                    if offset:
                        f.seek(offset)
                        f.truncate()
                    for chunk in resp.iter_raw():
                        if self.stop.is_set():
                            raise _Stop()
                        if received + len(chunk) > size:
                            raise _SourceGone("it sent more than the file's size")
                        f.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        self._progress(rel, received, peer, len(chunk))
        except httpx.HTTPError as exc:
            raise _SourceGone(describe_http_error(exc)) from exc
        finally:
            with self.cond:
                self.loads[peer.peer_id] -= 1
        if received != size:
            raise _SourceGone("the transfer ended early")
        if digest.hexdigest() != sha:
            os.unlink(part)
            self._progress(rel, 0)
            log.warning("%s of %s from %s failed its SHA-256 check", rel, self.d.name, peer.name)
            return False
        return True

    def _progress(self, rel: str, have: int, peer: Peer | None = None, new: int = 0) -> None:
        d = self.d
        with self.cond:
            self.in_flight[rel] = have
            d.received = self.done_bytes + sum(self.in_flight.values())
            if peer is not None:
                self.net_bytes += new
                entry = d.contributors.setdefault(peer.peer_id, [peer.name, 0])
                entry[0] = peer.name
                entry[1] += new

    def _file_done(self, rel: str, sha: str, size: int, wrote: bool) -> None:
        d = self.d
        with self.cond:
            self.in_flight.pop(rel, None)
            self.done_bytes += size
            d.done[rel] = sha
            if wrote:
                d.written.add(rel)
            d.files_done += 1
            d.received = self.done_bytes + sum(self.in_flight.values())

    def _measure_speed(self) -> None:
        start, at = self.window
        elapsed = time.monotonic() - start
        if elapsed >= 0.5:
            rate = (self.net_bytes - at) / elapsed
            self.d.speed = rate if not self.d.speed else 0.6 * rate + 0.4 * self.d.speed
            self.window = (time.monotonic(), self.net_bytes)

    # ----- done -------------------------------------------------------------------------

    def _finish(self) -> None:
        d = self.d
        assert self.folder is not None and self.acf is not None
        for rel in self.dirs:
            os.makedirs(long_path(safe_join(self.folder, rel)), exist_ok=True)
        manifest = d.library / "steamapps" / f"appmanifest_{d.appid}.acf"
        if not os.path.exists(manifest):
            tmp = manifest.with_name(manifest.name + ".lightninglan-tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.acf)
            _replace(str(tmp), str(manifest))
        self.m.steam.request_rescan()


def _file_entry(item: object) -> tuple[str, int, str, bool] | None:
    if not isinstance(item, dict):
        return None
    rel, size, sha = item.get("p"), item.get("s"), item.get("h")
    if not isinstance(rel, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        return None
    try:
        split_relative(rel)
    except UnsafePathError:
        return None
    if rel.endswith(PART_SUFFIX):
        return None
    return rel.replace("\\", "/"), size, sha, item.get("x") == 1


def _hash_prefix(part: str, length: int, digest, stop: threading.Event) -> int:
    """Feed the first `length` bytes of `part` to `digest`. Returns how many there were."""
    done = 0
    with open(part, "rb") as f:
        while done < length:
            if stop.is_set():
                raise _Stop()
            block = f.read(min(CHUNK, length - done))
            if not block:
                break
            digest.update(block)
            done += len(block)
    return done


def _replace(src: str, dst: str) -> None:
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.25)  # Windows: a virus scanner may have the file open
