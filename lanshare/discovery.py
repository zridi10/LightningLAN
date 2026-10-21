"""mDNS/DNS-SD advertising and browsing with zeroconf."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from dataclasses import dataclass
from typing import Callable

from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from . import SERVICE_TYPE, __version__
from .netutil import lan_addresses

log = logging.getLogger(__name__)

_PEER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
INTERFACE_CHECK_INTERVAL = 15.0
RESOLVE_TIMEOUT_MS = 3000


@dataclass(frozen=True)
class ServiceRecord:
    service_name: str
    peer_id: str
    name: str
    addresses: tuple[str, ...]
    port: int
    auth: bool


def _truncate_utf8(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def parse_service_info(info: AsyncServiceInfo) -> ServiceRecord | None:
    props = info.decoded_properties
    peer_id = props.get("id") or ""
    if not _PEER_ID_RE.match(peer_id) or not info.port:
        return None
    addresses = tuple(a for a in info.parsed_addresses(IPVersion.V4Only))
    if not addresses:
        return None
    return ServiceRecord(
        service_name=info.name,
        peer_id=peer_id,
        name=(props.get("name") or peer_id[:8])[:64],
        addresses=addresses,
        port=info.port,
        auth=props.get("auth") == "1",
    )


class Discovery:
    """Advertises this peer and reports other peers as they appear and disappear."""

    def __init__(
        self,
        peer_id: str,
        name: str,
        port: int,
        auth_enabled: bool,
        on_found: Callable[[ServiceRecord], None],
        on_lost: Callable[[str], None],
        interfaces: list[str] | None = None,
    ):
        self.peer_id = peer_id
        self.name = name
        self.port = port
        self.auth_enabled = auth_enabled
        self.on_found = on_found
        self.on_lost = on_lost
        self.explicit_interfaces = interfaces
        self.addresses: list[str] = []
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._info: AsyncServiceInfo | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task] = set()
        self._watcher: asyncio.Task | None = None
        self._registration: asyncio.Task | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.addresses = lan_addresses(self.explicit_interfaces)
        self._azc = AsyncZeroconf(interfaces=self.addresses, ip_version=IPVersion.V4Only)
        self._info = self._build_info()
        self._browser = AsyncServiceBrowser(
            self._azc.zeroconf, SERVICE_TYPE, handlers=[self._on_state_change]
        )
        # Registration probes the network for ~1.5 s; don't hold up the web server for it.
        self._registration = asyncio.create_task(self._register())
        if not self.explicit_interfaces:
            self._watcher = asyncio.create_task(self._watch_interfaces())

    async def _register(self) -> None:
        assert self._azc is not None and self._info is not None
        try:
            await self._azc.async_register_service(self._info, allow_name_change=True)
        except Exception:
            log.exception("could not advertise this peer via mDNS")
            return
        log.info("advertising %s on %s", self._info.name, ", ".join(self.addresses))

    async def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._azc is None:
            return
        if self._registration is not None and not self._registration.done():
            try:
                await asyncio.wait_for(self._registration, timeout=5)
            except Exception:
                pass
        try:
            if self._browser is not None:
                await self._browser.async_cancel()
            await self._azc.async_unregister_all_services()  # sends goodbye packets
        finally:
            await self._azc.async_close()
            self._azc = None

    # ----- internals --------------------------------------------------------------------

    def _build_info(self) -> AsyncServiceInfo:
        return AsyncServiceInfo(
            SERVICE_TYPE,
            f"LanShare-{self.peer_id}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip) for ip in self.addresses],
            port=self.port,
            properties={
                "id": self.peer_id,
                "name": _truncate_utf8(self.name, 200),
                "v": __version__,
                "auth": "1" if self.auth_enabled else "0",
            },
            server=f"lanshare-{self.peer_id[:12]}.local.",
        )

    def _on_state_change(
        self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        # zeroconf may call this from its own thread; hop onto our loop either way.
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._handle_change, name, state_change)

    def _handle_change(self, name: str, state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Removed:
            self.on_lost(name)
            return
        task = asyncio.ensure_future(self._resolve(name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve(self, name: str) -> None:
        if self._azc is None:
            return
        info = AsyncServiceInfo(SERVICE_TYPE, name)
        try:
            found = await info.async_request(self._azc.zeroconf, RESOLVE_TIMEOUT_MS)
        except Exception as exc:  # zeroconf raises a variety of errors while shutting down
            log.debug("resolving %s failed: %s", name, exc)
            return
        if not found:
            return
        record = parse_service_info(info)
        if record is None or record.peer_id == self.peer_id:
            return
        self.on_found(record)

    async def _watch_interfaces(self) -> None:
        """Follow network changes (Wi-Fi switch, DHCP renewal) without restarting."""
        while True:
            await asyncio.sleep(INTERFACE_CHECK_INTERVAL)
            try:
                current = await asyncio.to_thread(lan_addresses)
                if current == self.addresses or self._azc is None or self._info is None:
                    continue
                log.info("network changed: now advertising on %s", ", ".join(current))
                self.addresses = current
                await self._azc.async_update_interfaces(interfaces=current)
                self._info.addresses = [socket.inet_aton(ip) for ip in current]
                await self._azc.async_update_service(self._info)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("updating network interfaces failed")
