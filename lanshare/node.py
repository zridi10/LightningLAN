"""A running LanShare peer: ties the index, discovery, peer polling and transfers together."""

from __future__ import annotations

import hashlib
import json
import logging

from . import __version__
from .config import Settings
from .indexer import Indexer

log = logging.getLogger(__name__)


class Node:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.indexer = Indexer(
            settings.share_dir,
            cache_path=settings.state_dir / "hash_cache.json",
        )
        self._files_cache: tuple[int, bytes, str] | None = None

    @property
    def peer_id(self) -> str:
        return self.settings.peer_id

    @property
    def name(self) -> str:
        return self.settings.name

    async def start(self) -> None:
        self.indexer.start()

    async def stop(self) -> None:
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

    def status(self) -> dict:
        snap = self.indexer.snapshot
        return {
            "peer_id": self.peer_id,
            "name": self.name,
            "version": __version__,
            "port": self.settings.port,
            "share_dir": str(self.settings.share_dir),
            "downloads_dir": str(self.settings.downloads_dir),
            "passphrase_enabled": bool(self.settings.passphrase),
            "shared_files": len(snap.entries),
            "indexing_pending": snap.pending,
        }
