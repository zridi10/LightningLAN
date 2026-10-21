"""The peer table: peers found by discovery, verified and kept fresh by polling their index.

Polling GET /api/files doubles as the health check. A peer is listed only after a successful
poll; three consecutive failures take it off the list. While its mDNS record still exists it
is re-probed at a slower pace and returns as soon as it answers again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .auth import REASONS, Authenticator
from .discovery import ServiceRecord
from .pathsafe import display_name

log = logging.getLogger(__name__)

MAX_FAILURES = 3
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_FILES_PER_PEER = 200_000
OFFLINE_RETRY_INTERVAL = 10.0
IGNORED_RETRY_INTERVAL = 30.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

PENDING, ONLINE, OFFLINE, IGNORED = "pending", "online", "offline", "ignored"


class PollError(Exception):
    pass


@dataclass(frozen=True)
class RemoteFile:
    sha256: str
    name: str  # relative path exactly as the peer reported it (untrusted)
    size: int
    mtime: float


@dataclass
class Peer:
    peer_id: str
    name: str
    port: int
    addresses: tuple[str, ...]
    service_names: set[str] = field(default_factory=set)
    state: str = PENDING
    failures: int = 0
    last_seen: float | None = None
    last_error: str | None = None
    ignore_reason: str | None = None
    address: str | None = None  # the address that last answered
    files: tuple[RemoteFile, ...] = ()
    indexing: int = 0
    etag: str | None = None
    next_poll: float = 0.0

    @property
    def online(self) -> bool:
        return self.state == ONLINE

    def candidate_addresses(self) -> list[str]:
        ordered = [self.address] if self.address in self.addresses else []
        return ordered + [a for a in self.addresses if a not in ordered]

    def base_url(self) -> str:
        return f"http://{self.address or self.addresses[0]}:{self.port}"

    def to_dict(self) -> dict:
        return {
            "peer_id": self.peer_id,
            "name": self.name,
            "address": self.address or (self.addresses[0] if self.addresses else None),
            "port": self.port,
            "state": self.state,
            "files": len(self.files),
            "total_size": sum(f.size for f in self.files),
            "indexing": self.indexing,
            "last_seen": self.last_seen,
            "last_error": self.last_error,
            "reason": self.ignore_reason,
        }


def parse_index(payload: object, expected_peer_id: str) -> tuple[str, tuple[RemoteFile, ...], int]:
    """Validate another peer's /api/files response. Malformed entries are dropped."""
    if not isinstance(payload, dict):
        raise PollError("index is not a JSON object")
    if payload.get("peer_id") != expected_peer_id:
        raise PollError("a different peer answered at this address")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise PollError("index has no file list")
    files = []
    for item in raw_files[:MAX_FILES_PER_PEER]:
        if not isinstance(item, dict):
            continue
        sha, name, size, mtime = item.get("sha256"), item.get("name"), item.get("size"), item.get("mtime", 0)
        if not (isinstance(sha, str) and _SHA256_RE.match(sha)):
            continue
        if not isinstance(name, str) or not name or len(name) > 1024:
            continue
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            continue
        if not isinstance(mtime, (int, float)) or isinstance(mtime, bool):
            mtime = 0
        files.append(RemoteFile(sha, name, size, float(mtime)))
    name = payload.get("name")
    clean = display_name(name).strip()[:64] if isinstance(name, str) else ""
    indexing = payload.get("indexing")
    pending = indexing.get("pending", 0) if isinstance(indexing, dict) else 0
    return clean, tuple(files), pending if isinstance(pending, int) else 0


class PeerTable:
    def __init__(self, own_peer_id: str, auth: Authenticator, refresh_interval: float):
        self.own_peer_id = own_peer_id
        self.auth = auth
        self.refresh_interval = refresh_interval
        self.version = 0  # bumps whenever the set of online peers or their files change
        self.listeners: list[Callable[[Peer], None]] = []  # called when a peer goes offline
        self._peers: dict[str, Peer] = {}
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    # ----- lifecycle --------------------------------------------------------------------

    async def start(self) -> None:
        self._wake = asyncio.Event()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=2.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            trust_env=False,  # never route LAN traffic through a configured HTTP proxy
        )
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client is not None:
            await self._client.aclose()

    # ----- queries ----------------------------------------------------------------------

    def get(self, peer_id: str) -> Peer | None:
        return self._peers.get(peer_id)

    def online(self) -> list[Peer]:
        return sorted((p for p in list(self._peers.values()) if p.online), key=lambda p: p.name.lower())

    def ignored(self) -> list[Peer]:
        return [p for p in list(self._peers.values()) if p.state == IGNORED]

    # ----- discovery callbacks ----------------------------------------------------------

    def service_found(self, record: ServiceRecord) -> None:
        if record.peer_id == self.own_peer_id:
            return
        peer = self._peers.get(record.peer_id)
        if peer is None:
            peer = Peer(record.peer_id, record.name, record.port, record.addresses)
            self._peers[record.peer_id] = peer
            log.info("discovered peer %s at %s:%s", record.name, record.addresses[0], record.port)
        else:
            if (peer.port, peer.addresses) != (record.port, record.addresses):
                peer.address = None
            peer.port = record.port
            peer.addresses = record.addresses
        peer.service_names.add(record.service_name)
        peer.next_poll = 0.0
        self.wake()

    def service_lost(self, service_name: str) -> None:
        for peer in list(self._peers.values()):
            if service_name in peer.service_names:
                peer.service_names.discard(service_name)
                if not peer.service_names:
                    del self._peers[peer.peer_id]
                    log.info("peer %s left the network", peer.name)
                    if peer.online:
                        self._went_offline(peer)

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    # ----- polling ----------------------------------------------------------------------

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            now = time.monotonic()
            due = [p for p in list(self._peers.values()) if p.next_poll <= now]
            if due:
                await asyncio.gather(*(self._poll(p) for p in due), return_exceptions=True)
            self._wake.clear()
            next_due = min((p.next_poll for p in self._peers.values()), default=now + self.refresh_interval)
            delay = min(max(next_due - time.monotonic(), 0.05), self.refresh_interval)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def poll_now(self, peer_id: str) -> None:
        peer = self._peers.get(peer_id)
        if peer is not None:
            await self._poll(peer)

    async def _poll(self, peer: Peer) -> None:
        error = "no address answered"
        for address in peer.candidate_addresses():
            try:
                await self._poll_address(peer, address)
                return
            except _Ignored as exc:
                self._ignore(peer, str(exc))
                return
            except (httpx.HTTPError, PollError, ValueError) as exc:
                error = describe_http_error(exc)
            except Exception as exc:  # never let one bad peer stall the poll loop
                log.exception("unexpected error polling %s", peer.name)
                error = str(exc) or exc.__class__.__name__
        self._failed(peer, error)

    async def _poll_address(self, peer: Peer, address: str) -> None:
        assert self._client is not None
        path = "/api/files"
        headers = self.auth.sign_request("GET", path)
        if peer.etag and peer.online:
            headers["If-None-Match"] = peer.etag
        url = f"http://{address}:{peer.port}{path}"
        async with self._client.stream("GET", url, headers=headers) as resp:
            if resp.status_code in (401, 403):
                body = await _read_limited(resp, 64 * 1024)
                raise _Ignored(_auth_reason(body, self.auth.enabled))
            if not self.auth.verify_response(headers, resp.headers):
                raise _Ignored(REASONS["no_proof"])
            if resp.status_code == 304:
                self._succeeded(peer, address)
                return
            if resp.status_code != 200:
                raise PollError(f"HTTP {resp.status_code}")
            body = await _read_limited(resp, MAX_INDEX_BYTES)
            etag = resp.headers.get("etag")
        name, files, pending = parse_index(json.loads(body), peer.peer_id)
        if name and name != peer.name:
            peer.name = name
        if files != peer.files or not peer.online:
            self.version += 1
        peer.files, peer.indexing, peer.etag = files, pending, etag
        self._succeeded(peer, address)

    def _succeeded(self, peer: Peer, address: str) -> None:
        if not peer.online:
            log.info("peer %s is online (%s:%s)", peer.name, address, peer.port)
            self.version += 1
        peer.state = ONLINE
        peer.address = address
        peer.failures = 0
        peer.last_error = None
        peer.ignore_reason = None
        peer.last_seen = time.time()
        peer.next_poll = time.monotonic() + self.refresh_interval

    def _failed(self, peer: Peer, error: str) -> None:
        peer.failures += 1
        peer.last_error = error
        if peer.state == ONLINE and peer.failures >= MAX_FAILURES:
            log.info("peer %s is offline after %d failed checks: %s", peer.name, peer.failures, error)
            peer.state = OFFLINE
            peer.etag = None
            self._went_offline(peer)
        elif peer.state == PENDING and peer.failures >= MAX_FAILURES:
            peer.state = OFFLINE
        retry = OFFLINE_RETRY_INTERVAL if peer.state == OFFLINE else self.refresh_interval
        peer.next_poll = time.monotonic() + retry

    def _ignore(self, peer: Peer, reason: str) -> None:
        if peer.state != IGNORED:
            log.warning("ignoring peer %s: %s", peer.name, reason)
            if peer.online:
                self._went_offline(peer)
        peer.state = IGNORED
        peer.ignore_reason = reason
        peer.files = ()
        peer.etag = None
        peer.next_poll = time.monotonic() + IGNORED_RETRY_INTERVAL

    def _went_offline(self, peer: Peer) -> None:
        self.version += 1
        for listener in self.listeners:
            try:
                listener(peer)
            except Exception:
                log.exception("peer-offline listener failed")


class _Ignored(Exception):
    pass


async def _read_limited(resp: httpx.Response, limit: int) -> bytes:
    chunks, total = [], 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise PollError("response too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _auth_reason(body: bytes, we_have_passphrase: bool) -> str:
    try:
        reason = json.loads(body).get("reason")
    except (ValueError, AttributeError):
        reason = None
    if reason == "missing" and we_have_passphrase:
        reason = "malformed"
    return REASONS.get(reason or "", "peer rejected our credentials")


def describe_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "connection refused or host unreachable"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return str(exc) or exc.__class__.__name__

