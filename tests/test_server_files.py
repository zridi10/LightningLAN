import hashlib

from lightninglan import __version__


def test_files_endpoint_serves_own_index(peer):
    (peer.share / "a.txt").write_bytes(b"hello")
    (peer.share / "sub").mkdir()
    (peer.share / "sub" / "b.txt").write_bytes(b"world!")
    peer.scan()

    r = peer.get("/api/files")
    assert r.status_code == 200
    body = r.json()
    assert body["peer_id"] == peer.settings.peer_id
    assert body["name"] == "Test Peer"
    assert body["version"] == __version__
    assert body["indexing"] == {"pending": 0}
    files = {f["name"]: f for f in body["files"]}
    assert set(files) == {"a.txt", "sub/b.txt"}
    assert files["a.txt"]["size"] == 5
    assert files["a.txt"]["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert files["a.txt"]["id"] == files["a.txt"]["sha256"]
    assert isinstance(files["a.txt"]["mtime"], float)


def test_files_endpoint_supports_etag(peer):
    (peer.share / "a.txt").write_bytes(b"hello")
    peer.scan()
    first = peer.get("/api/files")
    etag = first.headers["etag"]
    again = peer.get("/api/files", headers={"If-None-Match": etag})
    assert again.status_code == 304

    (peer.share / "b.txt").write_bytes(b"new")
    peer.scan()
    changed = peer.get("/api/files", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag


def test_status_endpoint(peer):
    r = peer.get("/api/status")
    assert r.status_code == 200
    s = r.json()
    assert s["peer_id"] == peer.settings.peer_id
    assert s["share_dir"] == str(peer.share.absolute())
    assert s["passphrase_enabled"] is False
