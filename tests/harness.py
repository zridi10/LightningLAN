"""Integration-test harness: real LanShare peers as separate processes on this machine.

Each peer gets its own port, config file, shared folder and downloads folder. Peers find each
other through real mDNS. Set LANSHARE_TEST_INTERFACE=127.0.0.1 to keep discovery on loopback
(useful on machines without a LAN connection).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_HEADERS = {"X-LanShare-Client": "test"}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def wait_for(predicate: Callable[[], object], timeout: float = 20.0, interval: float = 0.2, message: str = ""):
    """Poll until predicate() is truthy and return its value; fail with `message` on timeout."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            last_exc = exc
        time.sleep(interval)
    detail = f" (last error: {last_exc!r})" if last_exc else ""
    raise AssertionError(f"timed out after {timeout}s waiting for {message or predicate}{detail}")


class PeerProcess:
    def __init__(
        self,
        base: Path,
        name: str,
        *,
        port: int | None = None,
        passphrase: str | None = None,
        refresh_interval: float = 0.5,
        extra_args: tuple[str, ...] = (),
    ):
        self.name = name
        self.dir = base / name
        self.share = self.dir / "share"
        self.downloads = self.dir / "downloads"
        self.config = self.dir / "config.json"
        self.log_path = self.dir / "peer.log"
        self.port = port or free_port()
        self.passphrase = passphrase
        self.refresh_interval = refresh_interval
        self.extra_args = tuple(extra_args)
        self.proc: subprocess.Popen | None = None
        self.peer_id: str | None = None
        self.share.mkdir(parents=True, exist_ok=True)
        self.downloads.mkdir(parents=True, exist_ok=True)

    # ----- process control --------------------------------------------------------------

    def command(self) -> list[str]:
        cmd = [
            sys.executable, "-m", "lanshare",
            "--share", str(self.share),
            "--downloads", str(self.downloads),
            "--port", str(self.port),
            "--name", self.name,
            "--config", str(self.config),
            "--refresh-interval", str(self.refresh_interval),
            "--no-browser",
        ]
        if self.passphrase:
            cmd += ["--passphrase", self.passphrase]
        iface = os.environ.get("LANSHARE_TEST_INTERFACE")
        if iface:
            cmd += ["--interface", iface]
        return cmd + list(self.extra_args)

    def start(self, wait: bool = True) -> "PeerProcess":
        log = open(self.log_path, "ab")
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            self.command(), cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT, env=env
        )
        log.close()
        if wait:
            self.wait_ready()
        return self

    def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"peer {self.name} exited early:\n{self.log_text()}")
            try:
                r = httpx.get(f"{self.url}/api/status", timeout=1.0, trust_env=False)
                if r.status_code == 200:
                    self.peer_id = r.json()["peer_id"]
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"peer {self.name} did not start:\n{self.log_text()}")

    def stop(self, timeout: float = 15.0) -> None:
        """Graceful shutdown through the local API (sends mDNS goodbye packets)."""
        if not self.running:
            return
        try:
            self.post("/api/shutdown")
            self.proc.wait(timeout=timeout)
        except (httpx.HTTPError, subprocess.TimeoutExpired):
            self.kill()

    def kill(self) -> None:
        """Hard kill: no goodbye, no cleanup, open connections reset."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=15)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "<no log>"

    # ----- HTTP helpers -----------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self, **kwargs) -> httpx.Client:
        return httpx.Client(base_url=self.url, headers=CLIENT_HEADERS, timeout=30, trust_env=False, **kwargs)

    def get(self, path: str, **kwargs) -> httpx.Response:
        with self.client() as c:
            return c.get(path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        with self.client() as c:
            return c.post(path, **kwargs)

    def online_peer_ids(self) -> set[str]:
        return {p["peer_id"] for p in self.get("/api/peers").json()["peers"]}

    def ignored_peer_ids(self) -> set[str]:
        return {p["peer_id"] for p in self.get("/api/peers").json()["ignored"]}

    def __repr__(self) -> str:
        return f"<PeerProcess {self.name} :{self.port}>"


def dump_logs(peers: list[PeerProcess]) -> str:
    return "\n".join(f"===== {p.name} =====\n{p.log_text()}" for p in peers)


def pretty(obj: object) -> str:
    return json.dumps(obj, indent=2)


def start_together(*peers: PeerProcess) -> None:
    """Start several peers in parallel (process start-up dominates test time)."""
    for p in peers:
        p.start(wait=False)
    for p in peers:
        p.wait_ready()


def common_files(peer: PeerProcess) -> dict[str, dict]:
    """The peer's Common Space keyed by file name."""
    return {f["name"]: f for f in peer.get("/api/common").json()["files"]}


def source_ids(item: dict) -> set[str]:
    return {s["peer_id"] for s in item["sources"]}
