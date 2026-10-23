"""ThunderCLOUD: one list of every file shared by this peer and all online peers.

Files are keyed by SHA-256, so identical content shared by several peers (even under
different names) is shown once with every source listed.
"""

from __future__ import annotations

from typing import Iterable

from .indexer import FileEntry
from .pathsafe import display_name
from .peers import Peer

MAX_ALIASES = 5


def merge_common_space(
    own_peer_id: str,
    own_name: str,
    own_entries: Iterable[FileEntry],
    peers: Iterable[Peer],
) -> list[dict]:
    items: dict[str, dict] = {}

    def add(sha: str, name: str, size: int, mtime: float, peer_id: str, peer_name: str, is_self: bool) -> None:
        shown = display_name(name)
        item = items.get(sha)
        if item is None:
            item = items[sha] = {
                "id": sha,
                "name": shown,
                "size": size,
                "mtime": mtime,
                "local": False,
                "sources": [],
                "aliases": [],
            }
        elif shown != item["name"] and shown not in item["aliases"] and len(item["aliases"]) < MAX_ALIASES:
            item["aliases"].append(shown)
        if not any(s["peer_id"] == peer_id for s in item["sources"]):
            item["sources"].append({"peer_id": peer_id, "name": peer_name, "file_name": shown, "self": is_self})
        item["mtime"] = max(item["mtime"], mtime)
        if is_self:
            item["local"] = True

    for entry in own_entries:
        add(entry.sha256, entry.relpath, entry.size, entry.mtime_ns / 1e9, own_peer_id, own_name, True)
    for peer in sorted(peers, key=lambda p: (p.name.lower(), p.peer_id)):
        if peer.peer_id == own_peer_id or not peer.online:
            continue
        for f in peer.files:
            add(f.sha256, f.name, f.size, f.mtime, peer.peer_id, peer.name, False)

    return sorted(items.values(), key=lambda i: (i["name"].lower(), i["id"]))
