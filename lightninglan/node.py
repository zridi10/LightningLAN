"""A running LightningLAN peer: ties the index, discovery, peer polling and transfers together."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path

from . import __version__
from .auth import Authenticator, derive_key
from .common import merge_common_space
from .config import MAX_NAME_LENGTH, ConfigError, Settings, prepare_folder, update_config_file
from .desktop import folder_picker_available
from .discovery import Discovery
from .indexer import Indexer
from .logs import log_dir
from .names import random_name
from .netutil import lan_addresses
from .peers import PeerTable
from .play import TITLES as GAME_TITLES, Arcade
from .pieces import PieceInfo
from .serving import UploadLimiter
from .transfers import PAUSED, DownloadManager

log = logging.getLogger(__name__)

MAX_PASSPHRASE_LENGTH = 256
STOPPED, CLOSED = "stopped", "closed"  # why a peer is offline: the Stop button, or the web UI was closed
UI_CLOSE_GRACE = 10.0  # seconds with no web UI open before it counts as closed (a reload is not a close)
RESUME_WAIT = 15.0  # after going online, how long paused downloads wait for a peer with the file


class SettingsError(Exception):
    """A settings change was rejected (message is user-facing)."""


class Node:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.auth = Authenticator(settings.passphrase, settings.peer_id)
        self.indexer = Indexer(
            settings.share_dir,
            cache_path=settings.state_dir / "hash_cache.json",
            interval=max(1.0, min(5.0, settings.refresh_interval * 2)),
        )
        self.peers = PeerTable(settings.peer_id, self.auth, settings.refresh_interval)
        self.peers.on_polled = self._check_name
        self.downloads = DownloadManager(settings.downloads_dir, self.auth, self.peers)
        self.arcade = Arcade(settings.peer_id, lambda: self.settings.name, self.auth, self.peers,
                             settings.refresh_interval)
        self.uploads = UploadLimiter(settings.max_upload_rate)
        self.discovery: Discovery | None = None
        if settings.discovery:
            self.discovery = Discovery(
                peer_id=settings.peer_id,
                name=settings.name,
                port=settings.port,
                auth_enabled=self.auth.enabled,
                on_found=self.peers.service_found,
                on_lost=self.peers.service_lost,
                interfaces=settings.interfaces,
            )
        self._files_cache: tuple[tuple[int, int], bytes, str] | None = None
        self._common_cache: tuple[tuple[int, int], bytes, str] | None = None
        # Online: sharing and downloading. Offline: hidden from peers, transfers stopped.
        self.online = True
        self.offline_reason: str | None = None  # STOPPED or CLOSED while offline
        self.state_version = 0  # bumps on every online/offline change
        self._switching = asyncio.Lock()
        self._closing = False
        self._paused_by_stop: list[str] = []
        self._ui_sessions = 0
        self._ui_timer: asyncio.Task | None = None
        self.phones = 0  # phone pages open right now
        self._phone_key_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._renaming = False

    @property
    def peer_id(self) -> str:
        return self.settings.peer_id

    @property
    def name(self) -> str:
        return self.settings.name

    async def start(self) -> None:
        self.indexer.start()
        await self.peers.start()
        await self.arcade.start()
        if self.discovery is not None:
            await self.discovery.start()

    async def stop(self) -> None:
        self._closing = True
        for task in list(self._tasks):
            task.cancel()
        async with self._switching:  # let a Stop or Start in progress finish first
            if self.discovery is not None:
                try:
                    await self.discovery.stop()  # say goodbye before anything else goes away
                except Exception:
                    log.exception("stopping discovery failed")
            self.downloads.shutdown()
            await self.arcade.stop()
            await self.peers.stop()
            self.indexer.stop()

    # ----- going offline and back (the Stop and Start buttons, closing the web UI) ----------

    async def go_offline(self, reason: str) -> None:
        """Stop sharing: leave the network, refuse other peers, cut off uploads, pause downloads."""
        async with self._switching:
            if self._closing:
                return
            if not self.online:
                if reason == STOPPED and self.offline_reason != STOPPED:
                    self.offline_reason = STOPPED  # now reopening the web UI no longer starts it again
                    self.state_version += 1
                return
            self.online = False
            self.offline_reason = reason
            self.state_version += 1
            self.uploads.cut_off()
            if self.discovery is not None:
                try:
                    await self.discovery.stop()
                except Exception:
                    log.exception("stopping discovery failed")
            self._paused_by_stop = await asyncio.to_thread(self.downloads.pause_all)
            await self.arcade.stop()
            await self.peers.stop()
            self.peers.clear()
            log.info("stopped sharing (%s)", "Stop was pressed" if reason == STOPPED else "the web UI was closed")

    async def go_online(self) -> None:
        """Share again: rejoin the network and resume the downloads that going offline paused."""
        async with self._switching:
            if self.online or self._closing:
                return
            self.uploads.reopen()
            await self.peers.start()
            await self.arcade.start()
            if self.discovery is not None:
                try:
                    await self.discovery.start()
                except Exception:
                    log.exception("could not rejoin the network")
            self.online = True
            self.offline_reason = None
            self.state_version += 1
            paused, self._paused_by_stop = self._paused_by_stop, []
            if paused:
                self._spawn(self._resume_when_found(paused))
            log.info("sharing again")

    async def _resume_when_found(self, download_ids: list[str]) -> None:
        # Peers take a moment to reappear: resume each download once a peer has its file again
        # (or after RESUME_WAIT, so it can say that nobody has it).
        deadline = time.monotonic() + RESUME_WAIT
        waiting = list(download_ids)
        while waiting and self.online:
            give_up = time.monotonic() >= deadline
            for download_id in list(waiting):
                d = self.downloads.find(download_id)
                if d is None or d.status != PAUSED:
                    waiting.remove(download_id)  # removed, or resumed by hand
                elif give_up or self.downloads.sources(d.file_id):
                    waiting.remove(download_id)
                    await asyncio.to_thread(self.downloads.resume_all, [download_id])
            await asyncio.sleep(0.5)

    def ui_opened(self) -> None:
        """A web UI tab connected. If closing the UI took this peer offline, it comes back."""
        self._ui_sessions += 1
        log.debug("web UI session opened (%d open)", self._ui_sessions)
        if self._ui_timer is not None:
            self._ui_timer.cancel()
            self._ui_timer = None
        if not self.online and self.offline_reason == CLOSED:
            self._spawn(self.go_online())

    def ui_closed(self) -> None:
        self._ui_sessions -= 1
        log.debug("web UI session closed (%d open)", self._ui_sessions)
        if self._ui_sessions == 0 and self.online and not self._closing:
            self._ui_timer = self._spawn(self._offline_unless_reopened())

    async def _offline_unless_reopened(self) -> None:
        await asyncio.sleep(UI_CLOSE_GRACE)
        self._ui_timer = None  # from here on, reopening the UI brings it back online instead
        if self._ui_sessions == 0:
            await self.go_offline(CLOSED)

    # ----- phones (the phone page, opened from the QR code in the web UI) --------------------

    def phone_opened(self) -> None:
        """A phone page connected. It keeps this peer online like an open web UI tab does."""
        self.phones += 1
        log.info("a phone connected (%d connected)", self.phones)
        self.ui_opened()

    def phone_closed(self) -> None:
        self.phones -= 1
        log.info("a phone disconnected (%d connected)", self.phones)
        self.ui_closed()

    def phone_key_matches(self, key: str | None) -> bool:
        stored = self.settings.phone_key
        if not stored or not key:
            return False
        return hmac.compare_digest(stored.encode("utf-8"), key.encode("utf-8"))

    async def ensure_phone_key(self, new: bool = False) -> str:
        """The secret in the phone link: made the first time it is asked for and kept in the config
        file, so phones stay connected. new=True replaces it, which disconnects every phone."""
        async with self._phone_key_lock:
            if self.settings.phone_key and not new:
                return self.settings.phone_key
            key = secrets.token_urlsafe(18)
            try:
                await asyncio.to_thread(update_config_file, self.settings.config_path, {"phone_key": key})
            except ConfigError as exc:
                raise SettingsError(str(exc)) from exc
            self.settings.phone_key = key
            log.info("new phone key%s", ": phones connected before must scan the QR code again" if new else "")
            return key

    async def phone_addresses(self) -> list[str]:
        """This computer's addresses that a phone on the same network can reach."""
        found = await asyncio.to_thread(lan_addresses, self.settings.interfaces)
        return [a for a in found if not a.startswith("127.")]

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def files_payload(self) -> tuple[bytes, str]:
        """The JSON body and ETag for GET /api/files, cached per index and swarm version."""
        snap = self.indexer.snapshot
        key = (snap.version, self.downloads.swarm_version)
        cached = self._files_cache
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
        body = json.dumps(
            {
                "peer_id": self.peer_id,
                "name": self.name,
                "version": __version__,
                "files": [entry.to_dict() for entry in snap.entries],
                "indexing": {"pending": snap.pending},
                # Its presence says we serve piece lists; the ids are files we can pass pieces of.
                "swarm": self.downloads.swarm_files(),
                "play": 1,  # serves GET /api/games: can play head-to-head games (LightningLAN 1.2+)
                "games": list(GAME_TITLES),  # which ones
                "name_auto": self.settings.name_generated,  # made-up names give way when two peers share one
            },
            separators=(",", ":"),
        ).encode("utf-8")
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        self._files_cache = (key, body, etag)
        return body, etag

    def piece_info(self, file_id: str) -> PieceInfo | None:
        """Piece hashes for a file we share, or one we downloaded whole and still list."""
        return self.indexer.pieces_info(file_id) or self.downloads.piece_info_for(file_id)

    def common_payload(self) -> tuple[bytes, str]:
        """The merged ThunderCLOUD as JSON plus an ETag, recomputed only when something changed."""
        key = (self.indexer.snapshot.version, self.peers.version)
        cached = self._common_cache
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
        online = self.peers.online()
        files = merge_common_space(self.peer_id, self.name, self.indexer.snapshot.entries, online)
        body = json.dumps(
            {
                "self": {"peer_id": self.peer_id, "name": self.name},
                "peers_online": len(online),
                "indexing_pending": self.indexer.snapshot.pending,
                "files": files,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        self._common_cache = (key, body, etag)
        return body, etag

    def status(self) -> dict:
        snap = self.indexer.snapshot
        return {
            "peer_id": self.peer_id,
            "name": self.name,
            "version": __version__,
            "port": self.settings.port,
            "addresses": self.discovery.addresses if self.discovery else [],
            "share_dir": str(self.settings.share_dir),
            "downloads_dir": str(self.settings.downloads_dir),
            "passphrase_enabled": self.auth.enabled,
            "shared_files": len(snap.entries),
            "indexing_pending": snap.pending,
            "peers_online": len(self.peers.online()),
            "uploaded_bytes": self.uploads.sent,
            "online": self.online,
            "offline_reason": self.offline_reason,
            "first_run": self.settings.first_run,
            "phones": self.phones,
        }

    def peers_payload(self) -> dict:
        return {
            "peers": [p.to_dict() for p in self.peers.online()],
            "ignored": [p.to_dict() for p in self.peers.ignored()],
        }

    # ----- settings (the web UI's Settings panel) ----------------------------------------

    def settings_payload(self) -> dict:
        s = self.settings
        return {
            "name": s.name,
            "share_dir": str(s.share_dir),
            "downloads_dir": str(s.downloads_dir),
            "passphrase": s.passphrase or "",
            "port": s.port,
            "url": f"http://localhost:{s.port}",
            "config_path": str(s.config_path) if s.config_path else None,
            "log_dir": str(log_dir(s.state_dir)),
            "can_pick_folder": folder_picker_available(),
        }

    async def update_settings(
        self,
        name: str | None = None,
        share_dir: str | None = None,
        downloads_dir: str | None = None,
        passphrase: str | None = None,
    ) -> list[str]:
        """Validate, save to the config file, then apply. Returns the names of the settings that changed.

        None means "leave unchanged"; an empty passphrase turns the passphrase off.
        """
        s = self.settings
        changes: dict[str, object] = {}
        if name is not None:
            cleaned = "".join(c for c in name if c.isprintable()).strip()
            if not cleaned:
                raise SettingsError("Enter a name for this computer.")
            if len(cleaned) > MAX_NAME_LENGTH:
                raise SettingsError(f"The name can be at most {MAX_NAME_LENGTH} characters long.")
            if cleaned != s.name:
                changes["name"] = cleaned
        for key, label, value in (("share_dir", "shared", share_dir), ("downloads_dir", "downloads", downloads_dir)):
            if value is None:
                continue
            folder = await asyncio.to_thread(_checked_folder, value, label)
            if folder != getattr(s, key):
                changes[key] = folder
        if passphrase is not None:
            if len(passphrase) > MAX_PASSPHRASE_LENGTH:
                raise SettingsError(f"The passphrase can be at most {MAX_PASSPHRASE_LENGTH} characters long.")
            if (passphrase or None) != s.passphrase:
                changes["passphrase"] = passphrase or None
        if not changes:
            return []

        stored = {k: (str(v) if isinstance(v, Path) else v) for k, v in changes.items()}
        if "name" in changes:
            stored["name_generated"] = False  # picked by the person: kept even if another peer has it too
        try:
            await asyncio.to_thread(update_config_file, s.config_path, stored)
        except ConfigError as exc:
            raise SettingsError(str(exc)) from exc

        if "name" in changes:
            s.name = changes["name"]  # type: ignore[assignment]
            s.name_generated = False
            self._files_cache = self._common_cache = None
        if "share_dir" in changes:
            s.share_dir = changes["share_dir"]  # type: ignore[assignment]
            self.indexer.set_root(s.share_dir)
        if "downloads_dir" in changes:
            s.downloads_dir = changes["downloads_dir"]  # type: ignore[assignment]
            self.downloads.downloads_dir = s.downloads_dir  # downloads already started finish where they are
        if "passphrase" in changes:
            s.passphrase = changes["passphrase"]  # type: ignore[assignment]
            key = await asyncio.to_thread(derive_key, s.passphrase) if s.passphrase else None
            self.auth.set_key(key)
            self.peers.credentials_changed()
            self._common_cache = None
        if self.discovery is not None and ("name" in changes or "passphrase" in changes):
            await self.discovery.update(name=s.name, auth_enabled=self.auth.enabled)
        log.info("settings changed: %s", ", ".join(sorted(changes)))
        return sorted(changes)


    # ----- unique names ------------------------------------------------------------------------

    def _check_name(self) -> None:
        """A made-up name that another peer already has is swapped for a free one.

        When both names were made up, the peer with the larger ID gives way; a name someone picked
        (or one from LightningLAN before 1.3, which doesn't say) never changes, so this one does.
        """
        s = self.settings
        if not s.name_generated or self._renaming or self._closing or not self.online:
            return
        mine = s.name.casefold()
        for peer in self.peers.online():
            if peer.name.casefold() == mine and (not peer.name_auto or self.peer_id > peer.peer_id):
                self._renaming = True
                self._spawn(self._take_free_name(peer.name))
                return

    async def _take_free_name(self, clash: str) -> None:
        s = self.settings
        try:
            new = random_name([s.name] + [p.name for p in self.peers.known()])
            if s.config_path is not None:
                await asyncio.to_thread(update_config_file, s.config_path, {"name": new, "name_generated": True})
            s.name = new
            self._files_cache = self._common_cache = None
            if self.discovery is not None:
                await self.discovery.update(name=new, auth_enabled=self.auth.enabled)
            log.info("another peer is also called %s, so this one is %s now", clash, new)
        except ConfigError as exc:
            log.warning("could not change the name: %s", exc)
        finally:
            self._renaming = False


def _checked_folder(value: str, label: str) -> Path:
    raw = value.strip().strip('"')
    if not raw:
        raise SettingsError(f"Enter the full path of the {label} folder.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SettingsError(f"Enter the full path of the {label} folder, starting from the drive or /.")
    try:
        return prepare_folder(path, label)
    except ConfigError as exc:
        raise SettingsError(str(exc)[0].upper() + str(exc)[1:]) from exc
