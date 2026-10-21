"""A running LanShare peer: ties the index, discovery, peer polling and transfers together."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from . import __version__
from .auth import Authenticator, derive_key
from .common import merge_common_space
from .config import MAX_NAME_LENGTH, ConfigError, Settings, prepare_folder, update_config_file
from .desktop import folder_picker_available
from .discovery import Discovery
from .indexer import Indexer
from .logs import log_dir
from .peers import PeerTable
from .transfers import DownloadManager

log = logging.getLogger(__name__)

MAX_PASSPHRASE_LENGTH = 256


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
        self.downloads = DownloadManager(settings.downloads_dir, self.auth, self.peers)
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
        self._files_cache: tuple[int, bytes, str] | None = None
        self._common_cache: tuple[tuple[int, int], bytes, str] | None = None

    @property
    def peer_id(self) -> str:
        return self.settings.peer_id

    @property
    def name(self) -> str:
        return self.settings.name

    async def start(self) -> None:
        self.indexer.start()
        await self.peers.start()
        if self.discovery is not None:
            await self.discovery.start()

    async def stop(self) -> None:
        if self.discovery is not None:
            try:
                await self.discovery.stop()  # say goodbye before anything else goes away
            except Exception:
                log.exception("stopping discovery failed")
        self.downloads.shutdown()
        await self.peers.stop()
        self.indexer.stop()

    def files_payload(self) -> tuple[bytes, str]:
        """The JSON body and ETag for GET /api/files, cached per index version."""
        snap = self.indexer.snapshot
        cached = self._files_cache
        if cached is not None and cached[0] == snap.version:
            return cached[1], cached[2]
        body = json.dumps(
            {
                "peer_id": self.peer_id,
                "name": self.name,
                "version": __version__,
                "files": [entry.to_dict() for entry in snap.entries],
                "indexing": {"pending": snap.pending},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        self._files_cache = (snap.version, body, etag)
        return body, etag

    def common_payload(self) -> tuple[bytes, str]:
        """The merged Common Space as JSON plus an ETag, recomputed only when something changed."""
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
            "first_run": self.settings.first_run,
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
        try:
            await asyncio.to_thread(update_config_file, s.config_path, stored)
        except ConfigError as exc:
            raise SettingsError(str(exc)) from exc

        if "name" in changes:
            s.name = changes["name"]  # type: ignore[assignment]
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
