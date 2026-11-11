"""What makes transfers fast: the sharer's ledger, compression, the download limit, hashing a file
while it downloads, and the bitmaps peers swap (in-process, no sockets)."""

from __future__ import annotations

import base64
import hashlib
import os
import random
import threading
import time

import pytest
import zstandard

from lightninglan.cli import build_parser, settings_from_args
from lightninglan.compress import (
    COMPRESS_HEADER,
    COMPRESSED_HEADER,
    MIN_SAVING,
    TRIAL_BYTES,
    ZSTD,
    Body,
    Compressibility,
    compressed,
    never_compress,
)
from lightninglan.pieces import PieceInfo, decode_bits, encode_bits, hash_file, pack_bits
from lightninglan.serving import (
    FELLOWS_HEADER,
    GOT_HEADER,
    HAVE_HEADER,
    PEER_HEADER,
    PIECE_HEADER,
    RateLimiter,
    SeedBusy,
    SeedLedger,
)
from lightninglan.swarm import InOrderHasher, SwarmState

from .conftest import LAN_CLIENT

MB = 1024 * 1024
ASKER, OTHER = "a" * 32, "b" * 32


def piece_request(sha: str, index: int, peer_id: str, have: bytearray | None, size: int) -> dict:
    start = index * MB
    headers = {"Range": f"bytes={start}-{min(start + MB, size) - 1}", PEER_HEADER: peer_id}
    if have is not None:
        headers[HAVE_HEADER] = encode_bits(have)
    return headers


# ----- the sharer's ledger ----------------------------------------------------------------------

@pytest.fixture
def big(peer):
    data = os.urandom(3 * MB + 5)  # four pieces of 1 MiB
    (peer.share / "big.bin").write_bytes(data)
    peer.scan()
    return peer, data, hashlib.sha256(data).hexdigest()


def test_sharer_sends_a_piece_nobody_has_had_instead_of_a_second_copy(big):
    peer, data, sha = big
    first = peer.get(f"/api/download/{sha}", client=LAN_CLIENT,
                     headers=piece_request(sha, 0, ASKER, bytearray(4), len(data)))
    assert first.status_code == 206 and first.content == data[:MB] and PIECE_HEADER not in first.headers
    assert first.headers[FELLOWS_HEADER] == ""  # it keeps a ledger, and nobody else asked yet

    # Another peer asks for piece 0 too: it gets piece 1, the first one not sent yet.
    second = peer.get(f"/api/download/{sha}", client=LAN_CLIENT,
                      headers=piece_request(sha, 0, OTHER, bytearray(4), len(data)))
    assert second.headers[PIECE_HEADER] == "1"
    assert second.headers["content-range"] == f"bytes {MB}-{2 * MB - 1}/{len(data)}"
    assert second.content == data[MB:2 * MB]
    assert second.headers[FELLOWS_HEADER] == ASKER  # and learns who else is fetching it

    # A piece the asker has already is never sent instead: piece 2 is had, so piece 3 comes.
    have = bytearray([1, 1, 1, 0])
    third = peer.get(f"/api/download/{sha}", client=LAN_CLIENT, headers=piece_request(sha, 0, OTHER, have, len(data)))
    assert third.headers[PIECE_HEADER] == "3" and third.content == data[3 * MB:]


def test_requests_without_a_have_map_get_what_they_ask_for(big):
    peer, data, sha = big
    for _ in range(2):  # LightningLAN 1.6 and the Android app: no ledger, no other piece
        r = peer.get(f"/api/download/{sha}", client=LAN_CLIENT,
                     headers=piece_request(sha, 0, ASKER, None, len(data)))
        assert r.status_code == 206 and r.content == data[:MB]
        assert PIECE_HEADER not in r.headers and FELLOWS_HEADER not in r.headers


def test_once_every_piece_went_out_pieces_may_be_sent_again(big):
    peer, data, sha = big
    for i in range(4):
        peer.get(f"/api/download/{sha}", client=LAN_CLIENT,
                 headers=piece_request(sha, i, ASKER, bytearray(4), len(data)))
    again = peer.get(f"/api/download/{sha}", client=LAN_CLIENT,
                     headers=piece_request(sha, 2, OTHER, bytearray(4), len(data)))
    assert PIECE_HEADER not in again.headers and again.content == data[2 * MB:3 * MB]


class FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers


def test_a_peer_slow_to_pass_pieces_on_is_told_to_take_them_from_the_others():
    now = [0.0]
    ledger = SeedLedger(clock=lambda: now[0])
    size, count = 40 * MB, 40
    fast, slow, observer = [c * 32 for c in "abcde"], "f" * 32, "9" * 32

    def ask(peer: str, index: int, got=()):
        bits = bytearray(count)
        for i in got:
            bits[i] = 1
        headers = {PEER_HEADER: peer, HAVE_HEADER: encode_bits(bits), GOT_HEADER: encode_bits(bits)}
        start, end, extra, tracked = ledger.plan("k", size, index * MB, (index + 1) * MB - 1, FakeRequest(headers))
        ledger.done("k", size, start, peer, True)
        return start // MB

    for i, peer in enumerate(fast + [slow]):  # each gets a piece nobody has yet
        assert ask(peer, i) == i
    now[0] = 0.2  # the fast peers' pieces reach another peer at once; the slow peer's doesn't
    ask(observer, 6, got=range(5))
    now[0] = 3.0
    with pytest.raises(SeedBusy):
        ask(slow, 7)
    assert ask(fast[0], 7) == 7  # the others go on as before
    now[0] = 3.1  # once its piece has spread, the slow peer gets new ones again
    assert ask(fast[1], 8, got=[5, 6]) == 8
    assert ask(slow, 9) == 9


def test_nobody_is_slow_without_others_to_compare_with():
    now = [0.0]
    ledger = SeedLedger(clock=lambda: now[0])
    headers = {PEER_HEADER: ASKER, HAVE_HEADER: encode_bits(bytearray(10)), GOT_HEADER: encode_bits(bytearray(10))}
    for i in range(10):
        now[0] = i * 10.0  # its pieces never reach anyone: there is nobody else
        start, _, _, _ = ledger.plan("k", 10 * MB, i * MB, (i + 1) * MB - 1, FakeRequest(headers))
        ledger.done("k", 10 * MB, start, ASKER, True)


# ----- compression ---------------------------------------------------------------------------------

def test_compressed_answer_only_when_asked(peer):
    text = b"the same few words over and over, as game data often is. " * 40000
    (peer.share / "notes.txt").write_bytes(text)
    peer.scan()
    sha = hashlib.sha256(text).hexdigest()
    plain = peer.get(f"/api/download/{sha}", client=LAN_CLIENT)
    assert COMPRESSED_HEADER not in plain.headers and plain.content == text

    sent_before = peer.node.uploads.sent
    r = peer.get(f"/api/download/{sha}", client=LAN_CLIENT, headers={COMPRESS_HEADER: ZSTD, "Range": "bytes=100-"})
    assert r.status_code == 206 and r.headers[COMPRESSED_HEADER] == ZSTD
    assert r.headers["content-range"] == f"bytes 100-{len(text) - 1}/{len(text)}"  # counts the bytes themselves
    assert len(r.content) < len(text) / 10
    assert b"".join(Body(iter([r.content]), True)) == text[100:]
    assert peer.node.uploads.sent - sent_before == len(r.content)  # what went out over the network


def test_files_that_dont_compress_stop_being_tried():
    seen = Compressibility()
    assert seen.worth_it("k", "data.bin")
    seen.record("k", TRIAL_BYTES, TRIAL_BYTES)  # saved nothing
    assert not seen.worth_it("k", "data.bin")
    seen.record("text", TRIAL_BYTES, int(TRIAL_BYTES * (1 - MIN_SAVING * 3)))
    assert seen.worth_it("text", "notes.txt")
    assert never_compress("Movie.MKV") and never_compress("x.zip") and not never_compress("pak0.vpk")
    assert not seen.worth_it("new", "trailer.mp4")


def test_compressed_stream_round_trips_and_counts_what_is_sent():
    chunks = [os.urandom(1000) + b"\0" * 300_000 for _ in range(5)]
    sent, totals = [], []
    out = b"".join(compressed(iter(chunks), sent.append, lambda raw, packed: totals.append((raw, packed))))
    assert sum(sent) == len(out) and totals == [(sum(map(len, chunks)), len(out))]
    body = Body(iter([out[:7], out[7:]]), True)
    pieces = list(body)
    assert b"".join(pieces) == b"".join(chunks)
    assert max(map(len, pieces)) <= 256 * 1024 and body.raw == len(out)


def test_a_small_body_cant_unpack_into_a_huge_one_at_once():
    bomb = zstandard.ZstdCompressor().compress(b"\0" * (64 * MB))
    assert len(bomb) < 10_000
    first = next(iter(Body(iter([bomb]), True)))
    assert len(first) <= 256 * 1024  # the reader stops a piece early, before holding it all


def test_broken_compressed_data_is_an_error():
    with pytest.raises(ValueError):
        list(Body(iter([b"not zstd at all, just bytes"]), True))


# ----- the download limit --------------------------------------------------------------------------

def test_download_limit_keeps_the_rate():
    limiter = RateLimiter(4 * MB)
    began = time.monotonic()
    for _ in range(12):
        limiter.consume(MB // 2)  # 6 MiB at 4 MiB/s, after a quarter second's allowance
    assert 1.0 <= time.monotonic() - began <= 2.5
    assert limiter.sent == 6 * MB


def test_download_rate_option(tmp_path):
    def settings(*extra):
        args = build_parser().parse_args(["--share", str(tmp_path / "s"), "--downloads", str(tmp_path / "d"),
                                          "--config", str(tmp_path / "c.json"), *extra])
        return settings_from_args(args)

    assert settings("--max-download-rate", "2.5").max_download_rate == 2.5 * MB
    assert settings().max_download_rate is None


# ----- hashing a file while it downloads -----------------------------------------------------------

def test_file_hashed_as_pieces_arrive_out_of_order(tmp_path):
    data = os.urandom(5 * MB + 123)
    path = tmp_path / "x.part"
    path.write_bytes(bytes(len(data)))
    sha, digests = hash_file_bytes(data, tmp_path)
    state = SwarmState(PieceInfo(sha, len(data), digests), path)
    hasher = InOrderHasher(state, path)
    hasher.start()
    order = list(range(state.info.count))
    random.Random(7).shuffle(order)
    with open(path, "r+b") as f:
        for i in order:
            start, length = state.info.span(i)
            f.seek(start)
            f.write(data[start:start + length])
            f.flush()
            state.mark(i)
    assert hasher.finish() == sha


def test_hasher_lets_go_of_the_file(tmp_path):
    path = tmp_path / "x.part"
    path.write_bytes(bytes(3 * MB))
    state = SwarmState(PieceInfo("0" * 64, 3 * MB, bytes(32 * 3)), path)
    hasher = InOrderHasher(state, path)
    hasher.start()
    hasher.close()
    path.rename(tmp_path / "moved")  # Windows refuses this while a handle is open


def hash_file_bytes(data: bytes, tmp_path) -> tuple[str, bytes]:
    p = tmp_path / "source.bin"
    p.write_bytes(data)
    return hash_file(p)


# ----- passing pieces on ----------------------------------------------------------------------------

def test_piece_reads_reuse_handles_but_never_on_a_finished_file(tmp_path):
    path = tmp_path / "x.part"
    path.write_bytes(bytes(2 * MB))
    state = SwarmState(PieceInfo("0" * 64, 2 * MB, bytes(64)), path, bytearray([1, 1]))
    assert state.locate(0) is not None
    f = state.open_reader()
    state.release_reader(f)
    assert state.locate(1) is not None
    assert state.open_reader() is f  # kept from before
    state.release_reader(f)
    with state.exclusive():
        path.rename(tmp_path / "x.bin")  # the kept handle was closed for this
    state.set_complete(tmp_path / "x.bin")
    assert state.locate(0) is not None
    g = state.open_reader()
    state.release_reader(g)
    assert g.closed


def test_have_maps_pack_and_unpack():
    for n in (0, 1, 7, 8, 9, 1000, 2049):
        have = bytearray(random.getrandbits(1) for _ in range(n))
        assert decode_bits(encode_bits(have), n) == have
        packed = pack_bits(have)
        assert len(packed) == (n + 7) // 8
    assert base64.b64decode(encode_bits(bytearray([1, 0, 0, 0, 0, 0, 0, 1, 1]))) == bytes([0x81, 0x80])
    state = SwarmState(PieceInfo("0" * 64, 9 * MB, bytes(32 * 9)), None, bytearray([1, 0, 0, 0, 0, 0, 0, 0, 0]))
    state.set_pending(8, True)
    assert decode_bits(state.have_or_pending(), 9) == bytearray([1, 0, 0, 0, 0, 0, 0, 0, 1])


def test_waiting_for_a_piece(tmp_path):
    state = SwarmState(PieceInfo("0" * 64, 2 * MB, bytes(64)), tmp_path / "x")
    assert not state.wait_for(1, 0.05)
    threading.Timer(0.05, state.mark, args=(1,)).start()
    assert state.wait_for(1, 2.0)
