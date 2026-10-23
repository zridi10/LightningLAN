"""Phones: the QR code link, pairing, the phone page's API and relaying other peers' files."""

import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest

from lightninglan.config import load_settings
from lightninglan.phone import COOKIE, RelayEnded, hold_until_verified, phone_links

from .conftest import LAN_CLIENT, InProcessPeer
from .harness import make_random_file, start_together, wait_for

PHONE = {"X-LightningLAN-Client": "phone"}


def cookie(key: str) -> dict:
    return {"Cookie": f"{COOKIE}={key}"}


def paired(peer) -> dict:
    """Set up the phone link the way the web UI's phone dialog does; returns the phone's cookie header."""
    assert peer.get("/api/phone").status_code == 200
    return cookie(peer.settings.phone_key)


# ----- unit ----------------------------------------------------------------------------------

def test_the_qr_code_holds_the_link_for_each_address():
    links = phone_links(["192.168.1.23", "10.0.0.5"], 8765, "k" * 24)
    assert [link["url"] for link in links] == [
        "http://192.168.1.23:8765/phone?key=" + "k" * 24,
        "http://10.0.0.5:8765/phone?key=" + "k" * 24,
    ]
    rows = links[0]["qr"]
    assert len(rows) >= 21 and all(len(row) == len(rows) and set(row) <= {"0", "1"} for row in rows)
    for top, left in [(0, 0), (0, len(rows) - 7), (len(rows) - 7, 0)]:  # the three finder squares
        assert rows[top][left:left + 7] == "1111111"
        assert rows[top + 1][left:left + 7] == "1000001"


def collect(chunks, sha256):
    async def source():
        for c in chunks:
            yield c

    async def run():
        out = []
        try:
            async for c in hold_until_verified(source(), sha256):
                out.append(c)
        except RelayEnded:
            return out, False
        return out, True

    return asyncio.run(run())


def test_a_relayed_file_is_held_back_at_the_end_unless_it_matches_its_sha256():
    import hashlib

    chunks = [b"abc", b"", b"def", b"ghi"]
    good = hashlib.sha256(b"abcdefghi").hexdigest()
    assert collect(chunks, good) == ([b"abc", b"def", b"ghi"], True)
    out, ok = collect(chunks, "0" * 64)
    assert not ok and b"".join(out) == b"abcdef"  # the last part never reaches the phone
    assert collect([], hashlib.sha256(b"").hexdigest()) == ([], True)


# ----- the phone link and pairing ------------------------------------------------------------

def test_the_phone_page_and_its_files_are_open_to_the_lan(peer):
    page = peer.get("/phone", client=LAN_CLIENT)
    assert page.status_code == 200 and 'id="file-list"' in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    for asset in ["phone.js", "style.css", "favicon.svg", "themes/bliss-sun.svg"]:
        assert peer.get(f"/static/{asset}", client=LAN_CLIENT).status_code == 200, asset
    # Typing the computer's address on a phone leads to the phone page; the computer still gets its own.
    root = peer.get("/", client=LAN_CLIENT)
    assert root.status_code == 307 and root.headers["location"] == "/phone"
    assert 'id="drop-zone"' in peer.get("/").text


def test_the_phone_api_needs_the_key_from_the_qr_code(peer):
    assert peer.get("/phone/api/status", client=LAN_CLIENT).status_code == 401  # no phone set up yet
    assert peer.get("/api/phone", client=LAN_CLIENT).status_code == 403  # the QR code is for this computer only
    cookies = paired(peer)
    assert peer.get("/phone/api/status", client=LAN_CLIENT).status_code == 401
    assert peer.get("/phone/api/status", client=LAN_CLIENT, headers=cookie("x" * 24)).status_code == 401
    status = peer.get("/phone/api/status", client=LAN_CLIENT, headers=cookies)
    assert status.status_code == 200
    assert status.json()["name"] == "Test Peer" and status.json()["online"] is True


def test_scanning_the_qr_code_pairs_the_phone_with_a_cookie(peer):
    link = peer.get("/api/phone").json()["links"]
    key = peer.settings.phone_key
    assert all(item["url"].endswith(f":{peer.settings.port}/phone?key={key}") for item in link)
    r = peer.get(f"/phone?key={key}", client=LAN_CLIENT)
    assert r.status_code == 303 and r.headers["location"] == "/phone"  # the key leaves the address bar
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}={key};")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    stale = peer.get("/phone?key=an-old-key-from-before", client=LAN_CLIENT)
    assert stale.status_code == 303 and stale.headers["location"] == "/phone#expired"
    assert "set-cookie" not in stale.headers


def test_the_phone_link_survives_a_restart(tmp_path):
    first = InProcessPeer(tmp_path)
    cookies = paired(first)
    again = load_settings(tmp_path / "config.json")
    assert cookies == cookie(again.phone_key)
    second = InProcessPeer(tmp_path)
    assert second.get("/phone/api/status", client=LAN_CLIENT, headers=cookies).status_code == 200


def test_disconnect_phones_replaces_the_key(peer):
    old = paired(peer)
    r = peer.post("/api/phone/new-key", headers={"X-LightningLAN-Client": "ui"})
    assert r.status_code == 200
    new = cookie(peer.settings.phone_key)
    assert new != old
    assert all(peer.settings.phone_key in link["url"] for link in r.json()["links"])
    assert peer.get("/phone/api/status", client=LAN_CLIENT, headers=old).status_code == 401
    assert peer.get("/phone/api/status", client=LAN_CLIENT, headers=new).status_code == 200
    assert peer.post("/api/phone/new-key", client=LAN_CLIENT, headers={**PHONE, **new}).status_code == 403


# ----- what a paired phone can do ------------------------------------------------------------

def test_a_phone_lists_and_downloads_files_this_computer_shares(peer):
    (peer.share / "photos").mkdir()
    (peer.share / "photos" / "beach.jpg").write_bytes(b"0123456789" * 1000)
    peer.scan()
    cookies = paired(peer)
    files = peer.get("/phone/api/files", client=LAN_CLIENT, headers=cookies)
    assert files.status_code == 200
    item = next(f for f in files.json()["files"] if f["name"] == "photos/beach.jpg")
    again = peer.get("/phone/api/files", client=LAN_CLIENT, headers={**cookies, "If-None-Match": files.headers["etag"]})
    assert again.status_code == 304

    whole = peer.get(f"/phone/api/file/{item['id']}", client=LAN_CLIENT, headers=cookies)
    assert whole.status_code == 200 and whole.content == b"0123456789" * 1000
    assert whole.headers["content-disposition"] == "attachment; filename*=UTF-8''beach.jpg"
    part = peer.get(f"/phone/api/file/{item['id']}", client=LAN_CLIENT, headers={**cookies, "Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == b"0123456789"
    assert peer.get("/phone/api/file/" + "0" * 64, client=LAN_CLIENT, headers=cookies).status_code == 404

    peer.node.online = False  # Stop was pressed on the computer
    stopped = peer.get(f"/phone/api/file/{item['id']}", client=LAN_CLIENT, headers=cookies)
    assert stopped.status_code == 409 and "stopped" in stopped.json()["detail"]


def test_a_phone_sends_files_into_the_shared_folder(peer):
    cookies = paired(peer)
    origin = {"Origin": f"http://127.0.0.1:{peer.settings.port}"}
    r = peer.post("/phone/api/upload", params={"name": "IMG_0001.jpg", "mtime": 1_700_000_000_000},
                  content=b"jpeg data", client=LAN_CLIENT, headers={**cookies, **PHONE, **origin})
    assert r.status_code == 201 and r.json() == {"name": "IMG_0001.jpg", "size": 9, "renamed": False}
    assert (peer.share / "IMG_0001.jpg").read_bytes() == b"jpeg data"

    for headers in [origin, {**PHONE, "Origin": "http://evil.example"}]:  # cross-site request forgery
        r = peer.post("/phone/api/upload", params={"name": "x.jpg"}, content=b"x", client=LAN_CLIENT,
                      headers={**cookies, **headers})
        assert r.status_code == 403
    assert peer.post("/phone/api/upload", params={"name": "x.jpg"}, content=b"x", client=LAN_CLIENT,
                     headers=PHONE).status_code == 401
    assert peer.post("/phone/api/upload", params={"name": "../x.jpg"}, content=b"x", client=LAN_CLIENT,
                     headers={**cookies, **PHONE}).status_code == 400
    assert not (peer.share / "x.jpg").exists() and not (peer.root / "x.jpg").exists()


def test_a_phone_cannot_reach_the_computers_own_api(peer):
    cookies = paired(peer)
    for method, path in [("GET", "/api/status"), ("GET", "/api/settings"), ("POST", "/api/stop"),
                         ("POST", "/api/shutdown"), ("GET", "/api/phone")]:
        r = peer.request(method, path, client=LAN_CLIENT, headers={**cookies, **PHONE})
        assert r.status_code == 403, path


# ----- real peers ------------------------------------------------------------------------------

def phone_client(peer, cookies) -> httpx.Client:
    return httpx.Client(base_url=peer.url, cookies=cookies, timeout=60, trust_env=False)


@contextmanager
def phone_page(peer, cookies):
    """Behave like an open phone page: hold its session stream open."""
    with phone_client(peer, cookies) as client:
        with client.stream("GET", "/phone/api/session") as resp:
            assert resp.status_code == 200
            lines = resp.iter_lines()  # keep it: dropping it closes the connection
            for line in lines:
                if line.startswith("data:"):
                    break
            yield json.loads(line[len("data:"):])


@pytest.mark.integration
def test_a_phone_gets_files_that_only_another_peer_has(spawn):
    a, b = spawn("Alice PC", start=False, passphrase="pass"), spawn("Bob PC", start=False, passphrase="pass")
    sha = make_random_file(b.share / "movie.bin", 3 * 1024 * 1024 + 123)
    start_together(a, b)
    assert a.get("/api/phone").status_code == 200
    cookies = {COOKIE: json.loads(a.config.read_text())["phone_key"]}

    with phone_client(a, cookies) as phone:
        def listed():
            files = phone.get("/phone/api/files").json()["files"]
            return next((f for f in files if f["id"] == sha), None)

        item = wait_for(listed, timeout=30, message="the phone to see Bob's file through Alice")
        assert [s["name"] for s in item["sources"]] == ["Bob PC"]
        whole = phone.get(f"/phone/api/file/{sha}")
        assert whole.status_code == 200
        assert whole.content == (b.share / "movie.bin").read_bytes()
        assert whole.headers["content-disposition"] == "attachment; filename*=UTF-8''movie.bin"
        part = phone.get(f"/phone/api/file/{sha}", headers={"Range": "bytes=1000-1999"})
        assert part.status_code == 206 and part.content == (b.share / "movie.bin").read_bytes()[1000:2000]
        assert part.headers["content-range"] == f"bytes 1000-1999/{3 * 1024 * 1024 + 123}"
    assert not list(a.downloads.iterdir())  # relayed straight through: nothing saved on Alice's computer


@pytest.mark.integration
def test_a_relayed_file_that_fails_its_sha256_never_finishes_on_the_phone(spawn):
    import os

    a, b = spawn("A", start=False), spawn("B", start=False)
    path = b.share / "photo.jpg"
    sha = make_random_file(path, 2 * 1024 * 1024)
    start_together(a, b)
    assert a.get("/api/phone").status_code == 200
    cookies = {COOKIE: json.loads(a.config.read_text())["phone_key"]}
    with phone_client(a, cookies) as phone:
        wait_for(lambda: any(f["id"] == sha for f in phone.get("/phone/api/files").json()["files"]), timeout=30)
        # B's copy changes behind its back: same size and time, so B still offers it under the old SHA-256.
        st = path.stat()
        with open(path, "r+b") as f:
            f.seek(1024 * 1024)
            f.write(b"\x00" * 16)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        with pytest.raises(httpx.RemoteProtocolError):  # the connection ends before the last part
            phone.get(f"/phone/api/file/{sha}")
    wait_for(lambda: "failed SHA-256 verification" in a.log_text(), timeout=10, message="A to log the bad file")


@pytest.mark.integration
def test_an_open_phone_page_counts_as_connected_and_ends_when_disconnected(spawn):
    a = spawn("A")
    assert a.get("/api/phone").status_code == 200
    cookies = {COOKIE: json.loads(a.config.read_text())["phone_key"]}
    with phone_page(a, cookies) as first:
        assert first == {"online": True}
        assert a.get("/api/status").json()["phones"] == 1
    wait_for(lambda: a.get("/api/status").json()["phones"] == 0, timeout=10, message="the phone to count as gone")

    with phone_client(a, cookies) as client:
        with client.stream("GET", "/phone/api/session") as resp:
            lines = resp.iter_lines()
            next(line for line in lines if line.startswith("data:"))
            wait_for(lambda: a.get("/api/status").json()["phones"] == 1, timeout=10)
            assert a.post("/api/phone/new-key").status_code == 200  # Disconnect phones
            for _ in lines:  # the stream ends by itself
                pass
    wait_for(lambda: a.get("/api/status").json()["phones"] == 0, timeout=10)
    with phone_client(a, cookies) as client:
        assert client.get("/phone/api/status").status_code == 401
