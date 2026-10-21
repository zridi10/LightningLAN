"""Passphrase authentication: token unit tests, middleware behaviour, and real peers."""

import hashlib
import time

import httpx
import pytest

from lightninglan.auth import PROOF_HEADER, TOKEN_HEADER, AuthError, Authenticator

from .conftest import LAN_CLIENT, InProcessPeer
from .harness import common_files, start_together, wait_for

A_ID, B_ID = "a" * 32, "b" * 32


# ----- tokens ---------------------------------------------------------------------------------

def test_token_roundtrip_and_proof():
    client, server = Authenticator("open sesame", A_ID), Authenticator("open sesame", B_ID)
    headers = client.sign_request("GET", "/api/files")
    assert server.verify_request("GET", "/api/files", headers[TOKEN_HEADER]) == A_ID
    proof = server.proof_for(headers[TOKEN_HEADER])
    assert client.verify_response(headers, {PROOF_HEADER: proof})


def test_passphrase_is_not_in_the_token():
    headers = Authenticator("open sesame", A_ID).sign_request("GET", "/api/files")
    assert "open sesame" not in headers[TOKEN_HEADER]


def test_wrong_passphrase_is_rejected_both_ways():
    good, bad = Authenticator("open sesame", A_ID), Authenticator("guess", B_ID)
    with pytest.raises(AuthError) as exc:
        good.verify_request("GET", "/api/files", bad.sign_request("GET", "/api/files")[TOKEN_HEADER])
    assert exc.value.reason == "bad_signature"
    headers = good.sign_request("GET", "/api/files")
    assert not good.verify_response(headers, {PROOF_HEADER: bad.proof_for(headers[TOKEN_HEADER])})
    assert not good.verify_response(headers, {})  # peer without a passphrase sends no proof


@pytest.mark.parametrize("method, path", [("HEAD", "/api/files"), ("GET", "/api/download/" + "0" * 64)])
def test_token_is_bound_to_method_and_path(method, path):
    auth = Authenticator("pw", A_ID)
    token = auth.sign_request("GET", "/api/files")[TOKEN_HEADER]
    with pytest.raises(AuthError):
        Authenticator("pw", B_ID).verify_request(method, path, token)


def test_replayed_token_is_rejected():
    client, server = Authenticator("pw", A_ID), Authenticator("pw", B_ID)
    token = client.sign_request("GET", "/api/files")[TOKEN_HEADER]
    server.verify_request("GET", "/api/files", token)
    with pytest.raises(AuthError) as exc:
        server.verify_request("GET", "/api/files", token)
    assert exc.value.reason == "replay"


def test_old_token_is_rejected(monkeypatch):
    client, server = Authenticator("pw", A_ID), Authenticator("pw", B_ID)
    token = client.sign_request("GET", "/api/files")[TOKEN_HEADER]
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 3600)
    with pytest.raises(AuthError) as exc:
        server.verify_request("GET", "/api/files", token)
    assert exc.value.reason == "clock_skew"


@pytest.mark.parametrize("token", [None, "", "v1.garbage", "v2." + "a" * 32 + ".1.aaaaaaaaaaaaaaaaaaaaaaaa." + "0" * 64])
def test_missing_or_malformed_tokens(token):
    with pytest.raises(AuthError):
        Authenticator("pw", B_ID).verify_request("GET", "/api/files", token)


def test_disabled_auth_signs_nothing_and_accepts_any_response():
    auth = Authenticator(None, A_ID)
    assert auth.sign_request("GET", "/api/files") == {}
    assert auth.verify_response({}, {})


# ----- middleware -----------------------------------------------------------------------------

@pytest.fixture
def locked_peer(tmp_path):
    p = InProcessPeer(tmp_path, passphrase="open sesame")
    (p.share / "secret-plans.txt").write_bytes(b"plans")
    p.scan()
    return p


def test_lan_routes_require_a_token(locked_peer):
    r = locked_peer.get("/api/files", client=LAN_CLIENT)
    assert r.status_code == 401
    assert r.json()["reason"] == "missing"
    assert b"secret-plans" not in r.content
    sha = hashlib.sha256(b"plans").hexdigest()
    assert locked_peer.get(f"/api/download/{sha}", client=LAN_CLIENT).status_code == 401


def test_wrong_passphrase_token_is_refused(locked_peer):
    headers = Authenticator("not it", A_ID).sign_request("GET", "/api/files")
    r = locked_peer.get("/api/files", client=LAN_CLIENT, headers=headers)
    assert r.status_code == 401
    assert r.json()["reason"] == "bad_signature"


def test_valid_token_is_accepted_and_response_carries_proof(locked_peer):
    client = Authenticator("open sesame", A_ID)
    headers = client.sign_request("GET", "/api/files")
    r = locked_peer.get("/api/files", client=LAN_CLIENT, headers=headers)
    assert r.status_code == 200
    assert [f["name"] for f in r.json()["files"]] == ["secret-plans.txt"]
    assert client.verify_response(headers, r.headers)

    sha = hashlib.sha256(b"plans").hexdigest()
    path = f"/api/download/{sha}"
    headers = client.sign_request("GET", path)
    r = locked_peer.get(path, client=LAN_CLIENT, headers=headers)
    assert r.content == b"plans" and client.verify_response(headers, r.headers)


def test_local_ui_does_not_need_the_passphrase(locked_peer):
    assert locked_peer.get("/api/common").status_code == 200
    assert locked_peer.get("/api/status").json()["passphrase_enabled"] is True


# ----- real peers -----------------------------------------------------------------------------

@pytest.mark.integration
def test_peers_with_wrong_or_missing_passphrase_are_excluded(spawn):
    a = spawn("A", start=False, passphrase="correct horse")
    b = spawn("B", start=False, passphrase="correct horse")
    c = spawn("C", start=False, passphrase="wrong guess")
    d = spawn("D", start=False)  # no passphrase at all
    for p in (a, b, c, d):
        (p.share / f"from-{p.name}.txt").write_text(p.name)
    start_together(a, b, c, d)

    wait_for(lambda: b.peer_id in a.online_peer_ids(), timeout=30, message="A to see B")
    wait_for(lambda: a.peer_id in b.online_peer_ids(), timeout=30, message="B to see A")
    wait_for(lambda: {c.peer_id, d.peer_id} <= a.ignored_peer_ids(), timeout=30,
             message="A to ignore C (wrong passphrase) and D (none)")
    wait_for(lambda: {a.peer_id, b.peer_id, d.peer_id} <= c.ignored_peer_ids(), timeout=30,
             message="C to ignore everyone")
    wait_for(lambda: {a.peer_id, b.peer_id, c.peer_id} <= d.ignored_peer_ids(), timeout=30,
             message="D to ignore everyone")

    assert c.peer_id not in a.online_peer_ids() and d.peer_id not in a.online_peer_ids()
    assert c.online_peer_ids().isdisjoint({a.peer_id, b.peer_id, d.peer_id})
    assert d.online_peer_ids().isdisjoint({a.peer_id, b.peer_id, c.peer_id})

    wait_for(lambda: "from-B.txt" in common_files(a), timeout=30, message="A to see B's file")
    assert set(common_files(a)) == {"from-A.txt", "from-B.txt"}
    assert set(common_files(c)) == {"from-C.txt"}
    assert set(common_files(d)) == {"from-D.txt"}

    ignored = {p["peer_id"]: p["reason"] for p in a.get("/api/peers").json()["ignored"]}
    assert ignored[c.peer_id] == "passphrase does not match"
    assert ignored[d.peer_id] == "peer does not use the same passphrase"
    ignored_by_d = {p["peer_id"]: p["reason"] for p in d.get("/api/peers").json()["ignored"]}
    assert ignored_by_d[a.peer_id] == "this peer requires a passphrase"

    # And the wrong-passphrase peer can't fetch anything directly either.
    sha = hashlib.sha256(b"A").hexdigest()
    with httpx.Client(trust_env=False) as raw:
        headers = Authenticator("wrong guess", c.peer_id).sign_request("GET", f"/api/download/{sha}")
        assert raw.get(f"{a.url}/api/download/{sha}", headers=headers).status_code == 401
        assert raw.get(f"{a.url}/api/files").status_code == 401
