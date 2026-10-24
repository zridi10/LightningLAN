"""The Settings panel's API: reading, validating, saving and applying changes without a restart."""

import asyncio
import json

import pytest

from lightninglan import desktop, server
from lightninglan.auth import Authenticator

from .conftest import LAN_CLIENT

UI = {"X-LightningLAN-Client": "ui"}


def save(peer, **changes):
    return peer.post("/api/settings", json=changes, headers=UI)


def config_of(peer) -> dict:
    return json.loads(peer.settings.config_path.read_text(encoding="utf-8"))


def test_settings_payload(peer):
    s = peer.get("/api/settings").json()
    assert s["name"] == "Test Peer"
    assert s["share_dir"] == str(peer.settings.share_dir)
    assert s["downloads_dir"] == str(peer.settings.downloads_dir)
    assert s["passphrase"] == ""
    assert s["url"] == f"http://localhost:{peer.settings.port}"
    assert s["log_dir"] == str(peer.settings.state_dir / "logs")
    assert isinstance(s["can_pick_folder"], bool)


def test_settings_are_local_only(peer):
    # The passphrase is in there: never readable or writable from the network or other websites.
    assert peer.get("/api/settings", client=LAN_CLIENT).status_code == 403
    assert peer.post("/api/settings", json={"name": "x"}, client=LAN_CLIENT, headers=UI).status_code == 403
    assert peer.post("/api/settings", json={"name": "x"}).status_code == 403  # no X-LightningLAN-Client header
    r = peer.post("/api/settings", json={"name": "x"},
                  headers={**UI, "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_the_tutorial_plays_by_itself_once(peer):
    assert peer.get("/api/status").json()["tutorial_due"] is True  # a new config: the first run
    assert peer.post("/api/tutorial/seen").status_code == 403  # local UI only, with the client header
    assert peer.post("/api/tutorial/seen", client=LAN_CLIENT, headers=UI).status_code == 403
    r = peer.post("/api/tutorial/seen", headers=UI)
    assert r.status_code == 200, r.text
    assert peer.get("/api/status").json()["tutorial_due"] is False
    assert "tutorial_due" not in config_of(peer)
    assert peer.post("/api/tutorial/seen", headers=UI).status_code == 200  # again: nothing changes


def test_rename(peer):
    peer.scan()
    before = peer.get("/api/files", client=LAN_CLIENT).json()
    r = save(peer, name="  Den PC  ")
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["name"]
    assert peer.get("/api/status").json()["name"] == "Den PC"
    assert config_of(peer)["name"] == "Den PC"
    assert before["name"] == "Test Peer"
    assert peer.get("/api/files", client=LAN_CLIENT).json()["name"] == "Den PC"  # peers see it on next poll


def clash_with(peer, name, name_auto, peer_id="f" * 32):
    from lightninglan.peers import ONLINE, Peer

    other = Peer(peer_id=peer_id, name=name, port=8765, addresses=("10.9.9.9",), state=ONLINE, name_auto=name_auto)
    peer.node.peers._peers[other.peer_id] = other

    async def check():
        peer.node._check_name()
        await asyncio.gather(*peer.node._tasks)

    asyncio.run(check())


def test_a_made_up_name_gives_way_when_another_peer_has_it(peer):
    peer.node.settings.name_generated = True
    clash_with(peer, "TEST PEER", name_auto=False)  # a name someone picked, or a peer from before 1.3
    assert peer.node.name.casefold() != "test peer" and peer.node.settings.name_generated
    assert config_of(peer)["name"] == peer.node.name and config_of(peer)["name_generated"] is True
    assert peer.get("/api/files", client=LAN_CLIENT).json()["name_auto"] is True


def test_when_both_names_were_made_up_only_one_peer_gives_way(peer):
    peer.node.settings.name_generated = True
    clash_with(peer, "Test Peer", name_auto=True, peer_id="f" * 32)  # its ID is larger: it gives way
    assert peer.node.name == "Test Peer"
    clash_with(peer, "Test Peer", name_auto=True, peer_id="0" * 32)  # ours is larger: we do
    assert peer.node.name != "Test Peer"


def test_a_picked_name_never_changes_by_itself(peer):
    assert not peer.node.settings.name_generated  # the fixture picks "Test Peer"
    clash_with(peer, "Test Peer", name_auto=False)
    assert peer.node.name == "Test Peer"
    peer.node.settings.name_generated = True
    assert save(peer, name="Den PC").status_code == 200
    assert not peer.node.settings.name_generated and config_of(peer)["name_generated"] is False


def test_unchanged_values_change_nothing(peer):
    s = peer.get("/api/settings").json()
    r = save(peer, name=s["name"], share_dir=s["share_dir"], downloads_dir=s["downloads_dir"], passphrase="")
    assert r.json()["changed"] == []


@pytest.mark.parametrize("changes, message", [
    ({"name": "   "}, "Enter a name"),
    ({"name": "x" * 65}, "at most 64"),
    ({"share_dir": "relative/folder"}, "full path"),
    ({"share_dir": ""}, "full path"),
    ({"passphrase": "p" * 257}, "at most 256"),
])
def test_invalid_values_are_rejected_and_nothing_is_saved(peer, changes, message):
    before = peer.settings.config_path.read_text(encoding="utf-8")
    r = save(peer, **changes)
    assert r.status_code == 400
    assert message in r.json()["detail"]
    assert peer.settings.config_path.read_text(encoding="utf-8") == before


def test_folder_that_is_a_file_is_rejected(peer, tmp_path):
    f = tmp_path / "not-a-folder.txt"
    f.write_text("x")
    r = save(peer, downloads_dir=str(f))
    assert r.status_code == 400
    assert "not a directory" in r.json()["detail"]


def test_switching_the_shared_folder(peer, tmp_path):
    (peer.share / "old.txt").write_text("old file")
    peer.scan()
    old_id = peer.get("/api/files", client=LAN_CLIENT).json()["files"][0]["id"]
    new = tmp_path / "Brand new share"
    r = save(peer, share_dir=str(new))
    assert r.status_code == 200, r.text
    assert new.is_dir(), "a missing folder is created"
    assert config_of(peer)["share_dir"] == str(new)
    # The old folder's files disappear at once and can no longer be downloaded.
    assert peer.get("/api/files", client=LAN_CLIENT).json()["files"] == []
    assert peer.get(f"/api/download/{old_id}", client=LAN_CLIENT).status_code == 404
    (new / "new.txt").write_text("new file")
    peer.scan()
    assert [f["name"] for f in peer.get("/api/files", client=LAN_CLIENT).json()["files"]] == ["new.txt"]
    # Uploads go to the new folder too.
    up = peer.post("/api/upload", params={"name": "dropped.txt"}, content=b"hi", headers=UI)
    assert up.status_code == 201
    assert (new / "dropped.txt").read_bytes() == b"hi"


def test_switching_the_downloads_folder(peer, tmp_path):
    new = tmp_path / "dl2"
    assert save(peer, downloads_dir=str(new)).status_code == 200
    assert peer.node.downloads.downloads_dir == new
    assert peer.get("/api/status").json()["downloads_dir"] == str(new)


def test_setting_and_removing_the_passphrase(peer):
    peer.scan()
    assert peer.get("/api/files", client=LAN_CLIENT).status_code == 200

    r = save(peer, passphrase="correct horse")
    assert r.status_code == 200 and r.json()["changed"] == ["passphrase"]
    assert config_of(peer)["passphrase"] == "correct horse"
    assert peer.get("/api/status").json()["passphrase_enabled"] is True
    assert peer.get("/api/settings").json()["passphrase"] == "correct horse"
    # Peers without it are now turned away; peers with it get in.
    assert peer.get("/api/files", client=LAN_CLIENT).status_code == 401
    other = Authenticator("correct horse", "a" * 32)
    ok = peer.get("/api/files", client=LAN_CLIENT, headers=other.sign_request("GET", "/api/files"))
    assert ok.status_code == 200

    r = save(peer, passphrase="")
    assert r.json()["changed"] == ["passphrase"]
    assert "passphrase" not in config_of(peer)
    assert peer.get("/api/status").json()["passphrase_enabled"] is False
    assert peer.get("/api/files", client=LAN_CLIENT).status_code == 200


def test_browse_button(peer, monkeypatch, tmp_path):
    calls = []

    def fake_pick(title, initial):
        calls.append((title, initial))
        return tmp_path / "Picked"

    monkeypatch.setattr(server, "pick_folder", fake_pick)
    r = peer.post("/api/pick-folder", json={"purpose": "share", "initial": str(tmp_path)}, headers=UI)
    assert r.json() == {"path": str(tmp_path / "Picked")}
    assert calls == [("Choose the folder to share", tmp_path)]

    monkeypatch.setattr(server, "pick_folder", lambda title, initial: None)
    r = peer.post("/api/pick-folder", json={"purpose": "downloads"}, headers=UI)
    assert r.json() == {"path": None}

    def busy(title, initial):
        raise desktop.FolderPickerError("A folder chooser is already open.")

    monkeypatch.setattr(server, "pick_folder", busy)
    r = peer.post("/api/pick-folder", json={"purpose": "share"}, headers=UI)
    assert r.status_code == 409
    assert peer.post("/api/pick-folder", json={"purpose": "share"}, client=LAN_CLIENT, headers=UI).status_code == 403


def test_only_one_folder_chooser_at_a_time():
    assert desktop._picker_lock.acquire(blocking=False)
    try:
        with pytest.raises(desktop.FolderPickerError):
            desktop.pick_folder("title")
    finally:
        desktop._picker_lock.release()


def test_open_log_folder(peer, monkeypatch):
    opened = []
    monkeypatch.setattr(server, "open_in_file_manager", lambda path, reveal=False: opened.append(path))
    r = peer.post("/api/open", json={"target": "logs"}, headers=UI)
    assert r.status_code == 200
    assert opened == [peer.settings.state_dir / "logs"]
    assert opened[0].is_dir()


def test_first_run_flag(tmp_path):
    from .conftest import InProcessPeer

    first = InProcessPeer(tmp_path / "p")
    assert first.get("/api/status").json()["first_run"] is True
    again = InProcessPeer(tmp_path / "p")
    assert again.get("/api/status").json()["first_run"] is False


# ----- live changes seen by another real peer ----------------------------------------------------

@pytest.mark.integration
def test_changes_reach_other_peers_without_a_restart(spawn, tmp_path):
    from .harness import common_files, start_together, wait_for

    a, b = spawn("Alice PC", start=False), spawn("Bob PC", start=False)
    start_together(a, b)
    wait_for(lambda: b.peer_id in a.online_peer_ids() and a.peer_id in b.online_peer_ids(), timeout=30,
             message="A and B to see each other")

    # New name and a different shared folder on A.
    other = tmp_path / "Alice other folder"
    other.mkdir()
    (other / "fresh.txt").write_text("in the new folder")
    r = a.post("/api/settings", json={"name": "Alice Laptop", "share_dir": str(other)})
    assert r.status_code == 200, r.text
    def name_of_a_on_b():
        return next((p["name"] for p in b.get("/api/peers").json()["peers"] if p["peer_id"] == a.peer_id), None)

    wait_for(lambda: name_of_a_on_b() == "Alice Laptop", timeout=30, message="B to see A's new name")
    wait_for(lambda: "fresh.txt" in common_files(b), timeout=30, message="B to see the new folder's file")

    # A passphrase on A only: the two stop seeing each other, on both sides.
    assert a.post("/api/settings", json={"passphrase": "orange cat"}).status_code == 200
    wait_for(lambda: b.peer_id in a.ignored_peer_ids() and a.peer_id in b.ignored_peer_ids(), timeout=60,
             message="A and B to ignore each other")
    assert "fresh.txt" not in common_files(b)

    # The same passphrase on B: back together.
    assert b.post("/api/settings", json={"passphrase": "orange cat"}).status_code == 200
    wait_for(lambda: b.peer_id in a.online_peer_ids() and a.peer_id in b.online_peer_ids(), timeout=60,
             message="A and B to reconnect with the shared passphrase")
    wait_for(lambda: "fresh.txt" in common_files(b), timeout=30, message="B to list A's files again")
