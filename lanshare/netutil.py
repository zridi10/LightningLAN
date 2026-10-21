"""Network interface selection for mDNS advertising."""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass

import ifaddr

log = logging.getLogger(__name__)

# Adapter-name fragments of virtual networks that never reach other machines on the LAN:
# container bridges, VM host-only networks, WSL/Hyper-V internal switches, overlay VPNs.
_VIRTUAL_HINTS = (
    "docker",
    "br-",
    "veth",
    "virbr",
    "vboxnet",
    "virtualbox",
    "vmware",
    "vmnet",
    "hyper-v",
    "vethernet",
    "wsl",
    "tailscale",
    "zerotier",
    "loopback",
)


@dataclass(frozen=True)
class Interface:
    name: str
    ip: str


def list_ipv4_interfaces() -> list[Interface]:
    result = []
    for adapter in ifaddr.get_adapters():
        label = f"{adapter.nice_name} {adapter.name}"
        for ip in adapter.ips:
            if isinstance(ip.ip, str):  # IPv6 addresses are tuples
                result.append(Interface(label, ip.ip))
    return result


def is_virtual_adapter(name: str) -> bool:
    lower = name.lower()
    return any(hint in lower for hint in _VIRTUAL_HINTS)


def is_usable_ip(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified)


def default_route_ip() -> str | None:
    """The local IP the OS would use to reach the internet (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: routed via the default gateway
            return s.getsockname()[0]
    except OSError:
        return None


def lan_addresses(
    explicit: list[str] | None = None,
    interfaces: list[Interface] | None = None,
    primary: str | None = None,
) -> list[str]:
    """IPv4 addresses to advertise on.

    Skips loopback, link-local and virtual adapters (Docker, VMs, WSL). The address holding
    the default route is always kept, even on an adapter whose name looks virtual, because a
    Hyper-V external switch or a Linux bridge can carry the real LAN. Falls back to loopback
    when nothing else is available, so peers on the same machine can still find each other.
    """
    if explicit:
        return list(dict.fromkeys(explicit))
    if interfaces is None:
        interfaces = list_ipv4_interfaces()
    if primary is None:
        primary = default_route_ip()

    chosen = [i.ip for i in interfaces if is_usable_ip(i.ip) and not is_virtual_adapter(i.name)]
    if primary and is_usable_ip(primary) and any(i.ip == primary for i in interfaces):
        chosen.insert(0, primary)
    chosen = list(dict.fromkeys(chosen))
    if not chosen:
        log.warning("no LAN network interface found; only peers on this computer will be visible")
        return ["127.0.0.1"]
    return chosen
