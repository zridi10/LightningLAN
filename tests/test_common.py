"""Unit tests for merging indexes into the Common Space."""

import hashlib

import pytest

from lanshare.common import merge_common_space
from lanshare.indexer import FileEntry
from lanshare.peers import OFFLINE, ONLINE, Peer, PollError, RemoteFile, parse_index

from .conftest import LAN_CLIENT

OWN = "0" * 32


def h(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def make_peer(letter: str, name: str, files, state=ONLINE) -> Peer:
    p = Peer(letter * 32, name, 8765, ("10.0.0.2",))
    p.state = state
    p.files = tuple(RemoteFile(sha, fname, size, 1.0) for sha, fname, size in files)
    return p


def test_merges_own_and_peer_files():
    own = [FileEntry("mine.txt", 4, 2_000_000_000, h("mine"))]
    peers = [make_peer("a", "Alice", [(h("theirs"), "theirs.txt", 6)])]
    items = merge_common_space(OWN, "Me", own, peers)
    by_name = {i["name"]: i for i in items}
    assert set(by_name) == {"mine.txt", "theirs.txt"}
    assert by_name["mine.txt"]["local"] is True
    assert by_name["mine.txt"]["sources"] == [{"peer_id": OWN, "name": "Me", "file_name": "mine.txt", "self": True}]
    assert by_name["theirs.txt"]["local"] is False
    assert by_name["theirs.txt"]["sources"][0]["name"] == "Alice"
    assert by_name["theirs.txt"]["size"] == 6


def test_same_hash_on_several_peers_is_listed_once_with_all_sources():
    sha = h("same content")
    own = [FileEntry("copy.bin", 12, 0, sha)]
    peers = [
        make_peer("b", "Bob", [(sha, "copy.bin", 12)]),
        make_peer("a", "Alice", [(sha, "renamed.bin", 12)]),
    ]
    items = merge_common_space(OWN, "Me", own, peers)
    assert len(items) == 1
    item = items[0]
    assert [s["name"] for s in item["sources"]] == ["Me", "Alice", "Bob"]  # self first, then by name
    assert item["aliases"] == ["renamed.bin"]
    assert item["local"] is True


def test_duplicate_content_within_one_peer_lists_that_peer_once():
    sha = h("dup")
    peers = [make_peer("a", "Alice", [(sha, "one.txt", 3), (sha, "two.txt", 3)])]
    items = merge_common_space(OWN, "Me", [], peers)
    assert len(items) == 1 and len(items[0]["sources"]) == 1
    assert items[0]["aliases"] == ["two.txt"]


def test_offline_peers_are_excluded():
    peers = [make_peer("a", "Alice", [(h("x"), "x.txt", 1)], state=OFFLINE)]
    assert merge_common_space(OWN, "Me", [], peers) == []


def test_sorted_by_name_case_insensitively():
    peers = [make_peer("a", "Alice", [(h("1"), "beta.txt", 1), (h("2"), "Alpha.txt", 1), (h("3"), "gamma.txt", 1)])]
    assert [i["name"] for i in merge_common_space(OWN, "Me", [], peers)] == ["Alpha.txt", "beta.txt", "gamma.txt"]


def test_hostile_names_are_sanitised_for_display():
    peers = [
        make_peer(
            "a",
            "Alice",
            [
                (h("1"), "invoice‮txt.exe", 1),
                (h("2"), "<img src=x onerror=alert(1)>.txt", 1),
                (h("3"), "bell\x07.txt", 1),
            ],
        )
    ]
    names = {i["name"] for i in merge_common_space(OWN, "Me", [], peers)}
    assert "invoicetxt.exe" in names  # bidi override removed
    assert "bell�.txt" in names  # control character replaced
    # Markup stays inert text: the UI only ever renders names with textContent.
    assert "<img src=x onerror=alert(1)>.txt" in names


def test_parse_index_drops_malformed_entries():
    pid = "a" * 32
    payload = {
        "peer_id": pid,
        "name": "Alice‮",
        "files": [
            {"sha256": h("ok"), "name": "ok.txt", "size": 2, "mtime": 1.5},
            {"sha256": "not-a-hash", "name": "bad.txt", "size": 1},
            {"sha256": h("neg"), "name": "neg.txt", "size": -1},
            {"sha256": h("bool"), "name": "bool.txt", "size": True},
            {"sha256": h("noname"), "name": "", "size": 1},
            "garbage",
        ],
        "indexing": {"pending": 3},
    }
    name, files, pending = parse_index(payload, pid)
    assert name == "Alice"
    assert [f.name for f in files] == ["ok.txt"]
    assert pending == 3


def test_parse_index_rejects_a_different_peer():
    with pytest.raises(PollError):
        parse_index({"peer_id": "b" * 32, "files": []}, "a" * 32)


def test_common_endpoint_in_process(peer):
    (peer.share / "a.txt").write_bytes(b"hello")
    peer.scan()
    r = peer.get("/api/common")
    assert r.status_code == 200
    body = r.json()
    assert body["self"]["peer_id"] == peer.settings.peer_id
    assert [f["name"] for f in body["files"]] == ["a.txt"]
    assert body["files"][0]["local"] is True
    again = peer.get("/api/common", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304


def test_common_endpoint_is_not_reachable_from_the_lan(peer):
    assert peer.get("/api/common", client=LAN_CLIENT).status_code == 403
