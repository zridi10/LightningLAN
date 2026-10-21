"""Serving files: Range requests, HEAD, validation (in-process, no sockets)."""

import hashlib
import os

import pytest

from lightninglan.serving import UNSATISFIABLE, parse_range

from .conftest import LAN_CLIENT

DATA = bytes(range(256)) * 40  # 10240 bytes
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def shared(peer):
    (peer.share / "data.bin").write_bytes(DATA)
    peer.scan()
    return peer


@pytest.mark.parametrize(
    "header, expected",
    [
        ("bytes=0-99", (0, 99)),
        ("bytes=100-", (100, 10239)),
        ("bytes=-50", (10190, 10239)),
        ("bytes=-999999", (0, 10239)),
        ("bytes=10000-99999", (10000, 10239)),
        ("BYTES = 5 - 6", (5, 6)),
        ("bytes=10240-", UNSATISFIABLE),
        ("bytes=-0", UNSATISFIABLE),
        ("bytes=9-3", None),
        ("bytes=0-1,5-6", None),  # multi-range: send the whole file
        ("items=0-1", None),
        ("bytes=-", None),
        ("garbage", None),
    ],
)
def test_parse_range(header, expected):
    assert parse_range(header, len(DATA)) == expected


def test_parse_range_on_empty_file():
    assert parse_range("bytes=0-", 0) == UNSATISFIABLE


def test_full_download(shared):
    r = shared.get(f"/api/download/{SHA}")
    assert r.status_code == 200
    assert r.content == DATA
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["etag"] == f'"{SHA}"'
    assert r.headers["content-length"] == str(len(DATA))
    assert "data.bin" in r.headers["content-disposition"]


def test_download_is_reachable_from_the_lan(shared):
    assert shared.get(f"/api/download/{SHA}", client=LAN_CLIENT).status_code == 200


@pytest.mark.parametrize(
    "rng, start, end",
    [("bytes=0-9", 0, 9), ("bytes=5000-", 5000, len(DATA) - 1), ("bytes=-100", len(DATA) - 100, len(DATA) - 1)],
)
def test_range_requests(shared, rng, start, end):
    r = shared.get(f"/api/download/{SHA}", headers={"Range": rng})
    assert r.status_code == 206
    assert r.content == DATA[start : end + 1]
    assert r.headers["content-range"] == f"bytes {start}-{end}/{len(DATA)}"
    assert r.headers["content-length"] == str(end - start + 1)


def test_unsatisfiable_range(shared):
    r = shared.get(f"/api/download/{SHA}", headers={"Range": f"bytes={len(DATA)}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(DATA)}"


def test_malformed_range_sends_whole_file(shared):
    r = shared.get(f"/api/download/{SHA}", headers={"Range": "bytes=0-1,4-5"})
    assert r.status_code == 200 and r.content == DATA


def test_if_range_mismatch_sends_whole_file(shared):
    r = shared.get(f"/api/download/{SHA}", headers={"Range": "bytes=100-", "If-Range": '"somethingelse"'})
    assert r.status_code == 200 and r.content == DATA
    r = shared.get(f"/api/download/{SHA}", headers={"Range": "bytes=100-", "If-Range": f'"{SHA}"'})
    assert r.status_code == 206 and r.content == DATA[100:]


def test_head_request(shared):
    r = shared.request("HEAD", f"/api/download/{SHA}")
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(DATA))
    assert r.content == b""


@pytest.mark.parametrize("file_id", ["0" * 64, "not-a-hash", "..%2F..%2Fconfig.json", SHA.upper()])
def test_unknown_or_invalid_ids_are_404(shared, file_id):
    assert shared.get(f"/api/download/{file_id}").status_code == 404


def test_traversal_through_the_download_url_is_impossible(shared):
    # Only 64-hex-digit IDs from our own index are ever mapped to files.
    for path in ["/api/download/../config.json", "/api/download/..%5C..%5Cconfig.json", "/api/download/%2e%2e/x"]:
        r = shared.get(path)
        assert r.status_code in (403, 404), path
        assert b"peer_id" not in r.content


def test_post_is_not_allowed_on_lan_routes(shared):
    assert shared.post(f"/api/download/{SHA}", client=LAN_CLIENT).status_code == 405
    assert shared.post("/api/files", client=LAN_CLIENT).status_code == 405


def test_file_changed_after_indexing_is_refused(shared):
    path = shared.share / "data.bin"
    path.write_bytes(DATA + b"more")
    r = shared.get(f"/api/download/{SHA}")
    assert r.status_code == 409


def test_deleted_file_is_404(shared):
    os.remove(shared.share / "data.bin")
    assert shared.get(f"/api/download/{SHA}").status_code == 404
