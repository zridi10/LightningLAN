"""Swarm downloads: piece maths, the swarm endpoints, the download engine against scripted peers,
and real peers trading pieces."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lightninglan.auth import Authenticator
from lightninglan.indexer import Indexer
from lightninglan.peers import ONLINE, Peer, PeerTable, RemoteFile
from lightninglan.pieces import (
    MIN_PIECE_SIZE,
    PieceInfo,
    decode_bits,
    encode_bits,
    hash_file,
    piece_count,
    piece_size_for,
)
from lightninglan.serving import UploadLimiter
from lightninglan.transfers import COMPLETED, MAX_ACTIVE_DOWNLOADS, PARTIAL_DIR_NAME, DownloadManager

from .conftest import LAN_CLIENT
from .harness import make_random_file, sha256_of, wait_download, wait_until_listed

MB = 1024 * 1024
OWN_ID = "0" * 32


def pieces_of(data: bytes) -> bytes:
    size = piece_size_for(len(data))
    return b"".join(hashlib.sha256(data[i:i + size]).digest() for i in range(0, len(data), size))


# ----- piece maths -----------------------------------------------------------------------------

@pytest.mark.parametrize("size, expected", [
    (0, MB), (MB, MB), (2048 * MB, MB), (2048 * MB + 1, 2 * MB), (6 * 1024 * MB, 4 * MB),
    (10 ** 13, 16 * MB),
])
def test_piece_size(size, expected):
    assert piece_size_for(size) == expected


def test_hash_file_matches_hashlib(tmp_path):
    data = os.urandom(3 * MB + 12345)
    f = tmp_path / "f.bin"
    f.write_bytes(data)
    digest, pieces = hash_file(f)
    assert digest == hashlib.sha256(data).hexdigest()
    assert pieces == pieces_of(data)
    assert len(pieces) == 4 * 32

    small = tmp_path / "small.bin"
    small.write_bytes(b"x" * MIN_PIECE_SIZE)  # exactly one piece: not split
    assert hash_file(small) == (hashlib.sha256(b"x" * MIN_PIECE_SIZE).hexdigest(), b"")


def test_piece_info_round_trip_and_validation():
    data = os.urandom(2 * MB + 7)
    sha = hashlib.sha256(data).hexdigest()
    info = PieceInfo(sha, len(data), pieces_of(data))
    assert info.count == 3
    assert info.span(2) == (2 * MB, 7)
    assert PieceInfo.from_json(json.loads(json.dumps(info.to_json())), sha, len(data)) == info
    good = info.to_json()
    for bad in [{**good, "size": len(data) + 1}, {**good, "file_id": "f" * 64}, {**good, "piece_size": 2 * MB},
                {**good, "hashes": good["hashes"][:-8]}, {**good, "hashes": "not base64!"}, [1, 2]]:
        with pytest.raises(ValueError):
            PieceInfo.from_json(bad, sha, len(data))


@pytest.mark.parametrize("count", [1, 7, 8, 9, 2048])
def test_have_map_round_trip(count):
    have = bytearray(os.urandom(count))
    have = bytearray(b & 1 for b in have)
    assert decode_bits(encode_bits(have), count) == have
    with pytest.raises(ValueError):
        decode_bits(encode_bits(have), count + 8)


def test_upload_limit_is_shared_by_all_transfers():
    limiter = UploadLimiter(4 * MB)
    began = time.monotonic()
    threads = [threading.Thread(target=lambda: [limiter.consume(256 * 1024) for _ in range(12)]) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - began
    assert limiter.sent == 6 * MB
    assert 0.7 < elapsed < 3.0, elapsed  # 6 MB at 4 MB/s, after a short burst allowance
    unlimited = UploadLimiter(None)
    unlimited.consume(100 * MB)
    assert unlimited.sent == 100 * MB


# ----- piece hashes in the index -----------------------------------------------------------------

def test_index_keeps_piece_hashes_and_caches_them(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    data = os.urandom(3 * MB)
    (share / "big.bin").write_bytes(data)
    (share / "small.txt").write_text("tiny")
    cache = tmp_path / "hash_cache.json"
    idx = Indexer(share, cache_path=cache)
    idx.scan()
    sha = hashlib.sha256(data).hexdigest()
    assert idx.pieces_info(sha) == PieceInfo(sha, len(data), pieces_of(data))
    assert idx.pieces_info(hashlib.sha256(b"tiny").hexdigest()) is None

    again = Indexer(share, cache_path=cache)
    again.scan()
    assert again.hash_count == 0, "piece hashes come from the cache"
    assert again.pieces_info(sha) == idx.pieces_info(sha)


def test_old_cache_without_piece_hashes_is_upgraded(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    data = os.urandom(2 * MB + 1)
    f = share / "big.bin"
    f.write_bytes(data)
    st = f.stat()
    sha = hashlib.sha256(data).hexdigest()
    cache = tmp_path / "hash_cache.json"
    cache.write_text(json.dumps({"version": 1, "files": {str(f.resolve()): [st.st_size, st.st_mtime_ns, sha]}}))
    idx = Indexer(share, cache_path=cache)
    idx.scan()
    assert [e.sha256 for e in idx.snapshot.entries] == [sha]
    assert idx.pieces_info(sha) is not None
    saved = json.loads(cache.read_text())
    assert saved["version"] == 2 and len(saved["files"][str(f.resolve())]) == 4


# ----- the swarm endpoints ---------------------------------------------------------------------

def test_swarm_endpoints(peer):
    data = os.urandom(3 * MB)
    (peer.share / "big.bin").write_bytes(data)
    (peer.share / "small.txt").write_text("tiny")
    peer.scan()
    sha = hashlib.sha256(data).hexdigest()

    index = peer.get("/api/files", client=LAN_CLIENT).json()
    assert index["swarm"] == []  # present: this peer serves piece lists

    r = peer.get(f"/api/pieces/{sha}", client=LAN_CLIENT)
    assert r.status_code == 200
    assert PieceInfo.from_json(r.json(), sha, len(data)).hashes == pieces_of(data)
    assert peer.get(f"/api/pieces/{hashlib.sha256(b'tiny').hexdigest()}", client=LAN_CLIENT).status_code == 404
    assert peer.get(f"/api/swarm/{sha}", client=LAN_CLIENT).status_code == 404
    assert peer.get(f"/api/piece/{sha}/0", client=LAN_CLIENT).status_code == 404
    assert peer.get("/api/piece/not-a-hash/0", client=LAN_CLIENT).status_code == 404


def test_swarm_endpoints_need_the_passphrase(tmp_path):
    from .conftest import InProcessPeer

    p = InProcessPeer(tmp_path, passphrase="secret")
    data = os.urandom(2 * MB)
    (p.share / "big.bin").write_bytes(data)
    p.scan()
    sha = hashlib.sha256(data).hexdigest()
    for path in (f"/api/pieces/{sha}", f"/api/swarm/{sha}", f"/api/piece/{sha}/0"):
        assert p.get(path, client=LAN_CLIENT).status_code == 401, path
    other = Authenticator("secret", "a" * 32)
    r = p.get(f"/api/pieces/{sha}", client=LAN_CLIENT, headers=other.sign_request("GET", f"/api/pieces/{sha}"))
    assert r.status_code == 200


# ----- the engine, against scripted peers ----------------------------------------------------------

class FakePeer:
    """A scripted peer: shares a file (piece list + ranges) or holds pieces of it (swarm)."""

    def __init__(self, content: bytes, *, piece_list: bool = True, corrupt: bool = False,
                 wrong_list: bool = False, delay: float = 0.0):
        self.content = content
        self.sha = hashlib.sha256(content).hexdigest()
        self.piece_list = piece_list
        self.corrupt = corrupt
        self.wrong_list = wrong_list
        self.delay = delay
        self.requests: list[tuple[str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def send(self, status, body, headers=()):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Range")))
                if outer.delay:
                    time.sleep(outer.delay)
                content = outer.content
                if self.path == f"/api/pieces/{outer.sha}" and outer.piece_list:
                    hashes = pieces_of(content)
                    if outer.wrong_list:
                        hashes = bytes(32) * (len(hashes) // 32)
                    info = PieceInfo(outer.sha, len(content), hashes)
                    return self.send(200, json.dumps(info.to_json()).encode(), [("Content-Type", "application/json")])
                if self.path == f"/api/swarm/{outer.sha}":
                    status = {"pieces": piece_count(len(content)), "complete": True}
                    return self.send(200, json.dumps(status).encode())
                m = re.match(rf"^/api/piece/{outer.sha}/(\d+)$", self.path)
                if m:
                    size = piece_size_for(len(content))
                    data = content[int(m.group(1)) * size:(int(m.group(1)) + 1) * size]
                    if outer.corrupt:
                        data = bytes(b ^ 0x55 for b in data)
                    return self.send(200, data)
                if self.path == f"/api/download/{outer.sha}":
                    m = re.match(r"bytes=(\d+)-(\d*)$", self.headers.get("Range", ""))
                    if m:
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else len(content) - 1
                        return self.send(206, content[start:end + 1],
                                         [("Content-Range", f"bytes {start}-{end}/{len(content)}")])
                    return self.send(200, content)
                return self.send(404, b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def swarm(tmp_path):
    made: list[FakePeer] = []
    table = PeerTable(OWN_ID, Authenticator(None, OWN_ID), refresh_interval=1.0)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    mgr = DownloadManager(downloads, Authenticator(None, OWN_ID), table)

    def add(content: bytes, *, seed: bool = True, swarm: bool = True, **kwargs) -> FakePeer:
        fake = FakePeer(content, **kwargs)
        made.append(fake)
        peer_id = f"{len(made):032x}"
        peer = Peer(peer_id, f"Peer {len(made)}", fake.port, ("127.0.0.1",))
        peer.state = ONLINE
        peer.swarm = swarm
        if seed:
            peer.files = (RemoteFile(fake.sha, "file.bin", len(content), 0.0),)
        else:
            peer.swarm_files = frozenset({fake.sha})
        fake.peer_id = peer_id
        table._peers[peer_id] = peer
        return fake

    add.mgr, add.downloads = mgr, downloads
    yield add
    mgr.shutdown()
    for fake in made:
        fake.close()


def wait_done(mgr: DownloadManager, download_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = next(x for x in mgr.list() if x["id"] == download_id)
        if d["status"] in ("completed", "failed", "paused", "canceled"):
            return d
        time.sleep(0.02)
    raise AssertionError("download did not finish")


def test_pieces_come_as_ranges_from_a_sharer(swarm):
    content = os.urandom(5 * MB + 123)
    seed = swarm(content)
    d = wait_done(swarm.mgr, swarm.mgr.start(seed.sha).id)
    assert d["status"] == COMPLETED, d
    assert Path(d["path"]).read_bytes() == content
    assert d["swarm"]["pieces"] == 6 and d["swarm"]["have"] == 6
    ranges = [r for p, r in seed.requests if p.startswith("/api/download/")]
    assert len(ranges) == 6 and all(re.match(r"bytes=\d+-\d+$", r) for r in ranges)
    assert not (swarm.downloads / PARTIAL_DIR_NAME / f"{seed.sha}.part").exists()


def test_pieces_come_from_every_peer_that_has_them(swarm):
    content = os.urandom(12 * MB)
    seed = swarm(content, delay=0.05)
    helper = swarm(content, seed=False, delay=0.05)  # a peer that finished downloading it
    d = wait_done(swarm.mgr, swarm.mgr.start(seed.sha).id)
    assert d["status"] == COMPLETED, d
    sent = {p["peer_id"]: p["bytes"] for p in d["swarm"]["peers"]}
    assert sent[seed.peer_id] > 0 and sent[helper.peer_id] > 0
    assert sum(sent.values()) == len(content)
    assert any(p.startswith(f"/api/piece/{seed.sha}/") for p, _ in helper.requests)


def test_bad_pieces_are_rejected_and_the_peer_dropped(swarm):
    content = os.urandom(12 * MB)
    seed = swarm(content, delay=0.02)
    liar = swarm(content, seed=False, corrupt=True)
    d = wait_done(swarm.mgr, swarm.mgr.start(seed.sha).id)
    assert d["status"] == COMPLETED, d
    assert Path(d["path"]).read_bytes() == content
    pieces_asked = [p for p, _ in liar.requests if p.startswith("/api/piece/")]
    assert len(pieces_asked) == 2, "dropped after two bad pieces"
    assert all(p["peer_id"] != liar.peer_id for p in d["swarm"]["peers"])


def test_peer_without_swarm_support_sends_the_whole_file(swarm):
    content = os.urandom(3 * MB)
    old = swarm(content, swarm=False)  # LightningLAN 1.0: no piece lists
    d = wait_done(swarm.mgr, swarm.mgr.start(old.sha).id)
    assert d["status"] == COMPLETED, d
    assert d["swarm"] is None
    assert [r for p, r in old.requests] == [None]
    assert d["source"]["peer_id"] == old.peer_id


def test_wrong_piece_list_falls_back_to_a_whole_file_download(swarm):
    content = os.urandom(3 * MB)
    seed = swarm(content, wrong_list=True)
    d = wait_done(swarm.mgr, swarm.mgr.start(seed.sha).id)
    assert d["status"] == COMPLETED, d
    assert Path(d["path"]).read_bytes() == content
    assert (f"/api/download/{seed.sha}", None) in seed.requests, "the whole file was requested in the end"


def test_partial_file_is_checked_and_resumed(swarm):
    content = os.urandom(6 * MB)
    seed = swarm(content)
    part = swarm.downloads / PARTIAL_DIR_NAME / f"{seed.sha}.part"
    part.parent.mkdir()
    with open(part, "wb") as f:  # pieces 0 and 2 from an earlier run, rubbish in piece 1
        f.write(content[:MB])
        f.write(os.urandom(MB))
        f.write(content[2 * MB:3 * MB])
    d = wait_done(swarm.mgr, swarm.mgr.start(seed.sha).id)
    assert d["status"] == COMPLETED, d
    assert d["resumed_from"] == 2 * MB
    asked = sorted(int(r.split("=")[1].split("-")[0]) // MB for p, r in seed.requests if r)
    assert asked == [1, 3, 4, 5]
    assert Path(d["path"]).read_bytes() == content


def test_finished_download_keeps_passing_pieces_on(swarm):
    content = os.urandom(3 * MB)
    seed = swarm(content)
    mgr = swarm.mgr
    d = wait_done(mgr, mgr.start(seed.sha).id)
    assert mgr.swarm_files() == [seed.sha]
    assert mgr.swarm_status(seed.sha)["complete"] is True
    assert mgr.piece_info_for(seed.sha).hashes == pieces_of(content)
    state, path, start, length = mgr.locate_piece(seed.sha, 1)
    state.release_reader()
    assert Path(path) == Path(d["path"]) and (start, length) == (MB, MB)

    Path(d["path"]).write_bytes(b"edited")  # changed after saving: no longer passed on
    assert mgr.locate_piece(seed.sha, 1) is None
    mgr.remove(d["id"])
    assert mgr.swarm_files() == []


def test_only_a_few_downloads_run_at_once(swarm):
    seeds = [swarm(os.urandom(1000 + i), delay=0.6) for i in range(MAX_ACTIVE_DOWNLOADS + 2)]
    ids = [swarm.mgr.start(s.sha).id for s in seeds]
    time.sleep(0.3)
    states = {d["id"]: d for d in swarm.mgr.list()}
    queued = [i for i in ids if states[i]["status"] == "queued"]
    assert len(queued) == 2
    assert all(states[i]["note"] for i in queued)
    for i in ids:
        assert wait_done(swarm.mgr, i)["status"] == COMPLETED


# ----- real peers trading pieces ---------------------------------------------------------------

@pytest.mark.integration
def test_downloaders_trade_pieces_instead_of_all_asking_the_sharer(spawn):
    # The sharer can send 4 MB/s in total. Three downloaders of a 24 MB file would need 72 MB
    # from it without a swarm; with one, most pieces travel between the downloaders.
    size = 24 * MB
    a = spawn("Sharer", start=False, extra_args=("--max-upload-rate", "4"))
    file_id = make_random_file(a.share / "film.mkv", size)
    downloaders = spawn.many("B", "C", "D")
    a.start()
    for p in downloaders:
        wait_until_listed(p, file_id, a, timeout=60)

    began = time.monotonic()
    ids = {p.name: p.post("/api/downloads", json={"file_id": file_id}).json()["id"] for p in downloaders}
    results = {p.name: wait_download(p, ids[p.name], timeout=120) for p in downloaders}
    elapsed = time.monotonic() - began

    sent_by_sharer = a.get("/api/status").json()["uploaded_bytes"]
    for p in downloaders:
        d = results[p.name]
        assert d["status"] == "completed", d
        assert sha256_of(p.downloads / "film.mkv") == file_id
        from_others = sum(x["bytes"] for x in d["swarm"]["peers"] if x["peer_id"] != a.peer_id)
        assert from_others > 0, f"{p.name} got nothing from the other downloaders"
    assert sent_by_sharer < 2 * size, f"the sharer sent {sent_by_sharer / MB:.0f} MB for 3 downloads"
    print(f"3 downloads of {size // MB} MB from a 4 MB/s sharer: {elapsed:.1f}s, "
          f"sharer sent {sent_by_sharer / MB:.1f} MB")
