"""Uploading files into the shared folder through the local UI endpoint."""

import hashlib
import os
import socket
import time

import pytest

from .conftest import LAN_CLIENT, make_junction, windows_only
from .harness import common_files, start_together, wait_for

UI = {"X-LanShare-Client": "ui"}


def upload(peer, name, data: bytes, **params):
    return peer.post("/api/upload", params={"name": name, **params}, content=data, headers=UI)


def test_upload_saves_file_into_shared_folder(peer):
    r = upload(peer, "hello.txt", b"hello world")
    assert r.status_code == 201, r.text
    assert r.json() == {"name": "hello.txt", "size": 11, "renamed": False}
    assert (peer.share / "hello.txt").read_bytes() == b"hello world"
    peer.scan()
    names = [f["name"] for f in peer.get("/api/files").json()["files"]]
    assert names == ["hello.txt"]


def test_upload_streams_large_body_without_leaving_temp_files(peer):
    chunk = os.urandom(1024 * 1024)

    async def body():
        for _ in range(24):
            yield chunk

    r = peer.post("/api/upload", params={"name": "big.bin"}, content=body(), headers=UI)
    assert r.status_code == 201, r.text
    saved = peer.share / "big.bin"
    assert saved.stat().st_size == 24 * len(chunk)
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == hashlib.sha256(chunk * 24).hexdigest()
    assert [p.name for p in peer.share.iterdir()] == ["big.bin"]


def test_name_collisions_get_numbered(peer):
    names = [upload(peer, "report.pdf", f"version {i}".encode()).json()["name"] for i in range(4)]
    assert names == ["report.pdf", "report (1).pdf", "report (2).pdf", "report (3).pdf"]
    assert (peer.share / "report.pdf").read_bytes() == b"version 0"
    assert (peer.share / "report (3).pdf").read_bytes() == b"version 3"
    r = upload(peer, "report.pdf", b"again")
    assert r.json()["renamed"] is True


def test_collision_naming_without_extension_and_with_double_extension(peer):
    assert upload(peer, "README", b"1").json()["name"] == "README"
    assert upload(peer, "README", b"2").json()["name"] == "README (1)"
    assert upload(peer, "backup.tar.gz", b"1").json()["name"] == "backup.tar.gz"
    assert upload(peer, "backup.tar.gz", b"2").json()["name"] == "backup (1).tar.gz"


def test_upload_into_subfolder(peer):
    r = upload(peer, "docs/2024/notes.txt", b"n")
    assert r.status_code == 201
    assert r.json()["name"] == "docs/2024/notes.txt"
    assert (peer.share / "docs" / "2024" / "notes.txt").read_bytes() == b"n"


def test_upload_keeps_original_modification_time(peer):
    mtime_ms = 1_600_000_000_000
    upload(peer, "old.txt", b"o", mtime=mtime_ms)
    assert abs((peer.share / "old.txt").stat().st_mtime - mtime_ms / 1000) < 2


def test_empty_file_upload(peer):
    assert upload(peer, "empty.txt", b"").status_code == 201
    assert (peer.share / "empty.txt").read_bytes() == b""


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "..\\escape.txt",
        "a/../../escape.txt",
        "sub/../../../escape.txt",
        "/tmp/escape.txt",
        "\\escape.txt",
        "C:\\escape.txt",
        "C:escape.txt",
        "\\\\server\\share\\escape.txt",
        "//server/share/escape.txt",
        "\\\\?\\C:\\escape.txt",
        "escape.txt:hidden-stream",
        "CON",
        "nul.txt",
        "",
        ".",
        "..",
        ".hidden",
        "folder/.git/config",
        "partial.part",
        "x.tmp",
        "trailing.",
    ],
)
def test_unsafe_or_unshareable_names_are_rejected(peer, name):
    r = upload(peer, name, b"evil")
    assert r.status_code in (400, 422), (name, r.status_code, r.text)
    # Nothing landed in or next to the shared folder.
    assert list(peer.share.iterdir()) == []
    assert not (peer.root / "escape.txt").exists()


@windows_only
def test_upload_through_junction_escaping_the_share_is_rejected(peer, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    make_junction(outside, peer.share / "link")
    r = upload(peer, "link/evil.txt", b"evil")
    assert r.status_code == 400
    assert list(outside.iterdir()) == []


# ----- localhost-only enforcement -------------------------------------------------------------

def test_upload_from_the_lan_is_forbidden(peer):
    r = peer.post("/api/upload", params={"name": "x.txt"}, content=b"x", headers=UI, client=LAN_CLIENT)
    assert r.status_code == 403
    assert list(peer.share.iterdir()) == []


@pytest.mark.parametrize("client", [("127.0.0.1", 1), ("127.8.9.10", 1), ("::1", 1), ("::ffff:127.0.0.1", 1)])
def test_upload_from_loopback_variants_is_allowed(peer, client):
    r = peer.post("/api/upload", params={"name": "ok.txt"}, content=b"x", headers=UI, client=client)
    assert r.status_code == 201


@pytest.mark.parametrize("client", [("192.168.1.50", 1), ("10.0.0.2", 1), ("::ffff:192.168.1.5", 1),
                                    ("fe80::1", 1), ("testclient", 1)])
def test_ui_endpoints_refuse_non_local_clients(peer, client):
    for method, path in [("GET", "/"), ("GET", "/api/status"), ("GET", "/api/common"), ("GET", "/api/peers"),
                         ("GET", "/api/downloads"), ("POST", "/api/downloads"), ("POST", "/api/shutdown"),
                         ("POST", "/api/upload?name=a.txt")]:
        r = peer.request(method, path, client=client, headers=UI)
        assert r.status_code == 403, (method, path, client)


def test_lan_routes_are_open_to_the_lan(peer):
    (peer.share / "a.txt").write_bytes(b"a")
    peer.scan()
    assert peer.get("/api/files", client=LAN_CLIENT).status_code == 200
    sha = hashlib.sha256(b"a").hexdigest()
    assert peer.get(f"/api/download/{sha}", client=LAN_CLIENT).content == b"a"


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:8765", "192.168.1.5:8765", "localhost.evil.com"])
def test_dns_rebinding_host_headers_are_refused(peer, host):
    r = peer.get("/api/status", headers={"Host": host})
    assert r.status_code == 403


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1:8765", "[::1]:8765", "localhost"])
def test_local_host_headers_are_accepted(peer, host):
    assert peer.get("/api/status", headers={"Host": host}).status_code == 200


def test_state_changing_requests_need_the_client_header(peer):
    r = peer.post("/api/upload", params={"name": "csrf.txt"}, content=b"x")
    assert r.status_code == 403
    assert not (peer.share / "csrf.txt").exists()


@pytest.mark.parametrize("origin", ["https://evil.example.com", "null", "http://localhost.evil.com", "ftp://127.0.0.1"])
def test_cross_site_origins_are_refused(peer, origin):
    r = peer.post("/api/upload", params={"name": "csrf.txt"}, content=b"x", headers={**UI, "Origin": origin})
    assert r.status_code == 403
    assert not (peer.share / "csrf.txt").exists()


def test_same_origin_requests_are_accepted(peer):
    r = peer.post("/api/upload", params={"name": "ok.txt"}, content=b"x",
                  headers={**UI, "Origin": "http://localhost:8765"})
    assert r.status_code == 201


# ----- with real peer processes -----------------------------------------------------------------

@pytest.mark.integration
def test_uploaded_file_appears_on_other_peers(spawn):
    a, b = spawn("A", start=False), spawn("B", start=False)
    start_together(a, b)
    r = a.post("/api/upload", params={"name": "dropped.txt"}, content=b"dropped into the UI")
    assert r.status_code == 201, r.text
    item = wait_for(lambda: common_files(b).get("dropped.txt"), timeout=30, message="B to see the upload")
    assert item["sources"][0]["peer_id"] == a.peer_id
    assert item["id"] == hashlib.sha256(b"dropped into the UI").hexdigest()


@pytest.mark.integration
def test_upload_via_lan_address_is_forbidden(spawn):
    a = spawn("A")
    lan_ips = [ip for ip in a.get("/api/status").json()["addresses"] if not ip.startswith("127.")]
    if not lan_ips:
        pytest.skip("no LAN address on this machine")
    import httpx

    url = f"http://{lan_ips[0]}:{a.port}"
    with httpx.Client(base_url=url, headers=UI, trust_env=False, timeout=10) as c:
        assert c.post("/api/upload", params={"name": "x.txt"}, content=b"x").status_code == 403
        assert c.get("/api/status").status_code == 403
        assert c.get("/").status_code == 403
        assert c.get("/api/files").status_code == 200
    assert list(a.share.iterdir()) == []


@pytest.mark.integration
def test_aborted_upload_leaves_no_temp_file(spawn):
    a = spawn("A")
    with socket.create_connection(("127.0.0.1", a.port)) as s:
        s.sendall(
            b"POST /api/upload?name=aborted.bin HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nX-LanShare-Client: test\r\n"
            b"Content-Length: 50000000\r\n\r\n" + os.urandom(2 * 1024 * 1024)
        )
        time.sleep(0.5)
    wait_for(lambda: list(a.share.iterdir()) == [], timeout=10, message="temp file to be cleaned up")
