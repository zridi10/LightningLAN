from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest

from lanshare.config import load_settings
from lanshare.node import Node
from lanshare.server import create_app

LOCAL_CLIENT = ("127.0.0.1", 50123)
LAN_CLIENT = ("192.168.1.50", 50123)


class InProcessPeer:
    """A peer whose app is called in-process (no sockets, no background threads)."""

    def __init__(self, root: Path, name: str = "Test Peer", port: int = 8765, **kwargs):
        self.root = root
        self.share = root / "share"
        self.downloads = root / "downloads"
        self.settings = load_settings(
            root / "config.json", share=self.share, downloads=self.downloads, port=port, name=name, **kwargs
        )
        self.settings.discovery = False
        self.settings.open_browser = False
        self.node = Node(self.settings)
        self.app = create_app(self.node)

    def scan(self) -> None:
        self.node.indexer.scan()

    def request(self, method: str, url: str, *, client=LOCAL_CLIENT, base_url: str | None = None, **kwargs):
        base = base_url or f"http://127.0.0.1:{self.settings.port}"

        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app, client=client)
            async with httpx.AsyncClient(transport=transport, base_url=base) as c:
                return await c.request(method, url, **kwargs)

        return asyncio.run(go())

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)


@pytest.fixture
def peer(tmp_path: Path) -> InProcessPeer:
    return InProcessPeer(tmp_path)


def can_symlink(tmp_path: Path) -> bool:
    try:
        os.symlink(tmp_path, tmp_path / "symlink-probe", target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    os.unlink(tmp_path / "symlink-probe")
    return True


def make_junction(target: Path, link: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
