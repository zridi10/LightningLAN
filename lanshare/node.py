"""A running LanShare peer: ties the index, discovery, peer polling and transfers together."""

from __future__ import annotations

import hashlib
import json
import logging

from . import __version__
from .auth import Authenticator
from .common import merge_common_space
from .config import Settings
from .discovery import Discovery
from .indexer import Indexer
from .peers import PeerTable

log = logging.getLogger(__name__)


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
        }

    def peers_payload(self) -> dict:
        return {
            "peers": [p.to_dict() for p in self.peers.online()],
            "ignored": [p.to_dict() for p in self.peers.ignored()],
        }
