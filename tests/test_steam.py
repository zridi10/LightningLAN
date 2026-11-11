"""Steam games: reading the libraries, sharing a game's files, and copying one into another library."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from lightninglan import steam as steam_module
from lightninglan.peers import parse_steam_copying
from lightninglan.pieces import hash_file
from lightninglan.steam import (
    SteamLibrary,
    dump_vdf,
    library_folders,
    parse_vdf,
    portable_manifest,
    scan_games,
    walk_game,
)
from lightninglan.steamget import _file_entry

from .conftest import LAN_CLIENT, InProcessPeer
from .harness import get_download, sha256_of, start_together, wait_download, wait_for

KB = 1024


# ----- fake Steam folders -----------------------------------------------------------------------

def make_steam(root: Path, *extra_libraries: Path) -> Path:
    """A Steam folder whose libraryfolders.vdf lists itself and `extra_libraries`."""
    (root / "steamapps").mkdir(parents=True, exist_ok=True)
    folders = {"contentstatsid": "123"}
    for i, library in enumerate((root, *extra_libraries)):
        (library / "steamapps").mkdir(parents=True, exist_ok=True)
        folders[str(i)] = {"path": str(library), "label": "", "apps": {}}
    (root / "steamapps" / "libraryfolders.vdf").write_text(dump_vdf({"libraryfolders": folders}), encoding="utf-8")
    return root


def add_game(library: Path, appid: int, name: str, files: dict[str, bytes], *, installdir: str | None = None,
             flags: int = 4, buildid: str = "1234", updated: int = 1700000000, dirs: tuple[str, ...] = ()) -> Path:
    installdir = installdir or name
    folder = library / "steamapps" / "common" / installdir
    for rel, data in files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for rel in dirs:
        (folder / rel).mkdir(parents=True, exist_ok=True)
    state = {
        "appid": str(appid),
        "universe": "1",
        "LauncherPath": "C:\\Program Files (x86)\\Steam\\steam.exe",
        "name": name,
        "StateFlags": str(flags),
        "installdir": installdir,
        "LastUpdated": str(updated),
        "LastPlayed": "1700000500",
        "SizeOnDisk": str(sum(len(d) for d in files.values())),
        "buildid": buildid,
        "LastOwner": "76561198000000000",
        "BytesToDownload": "1000",
        "BytesDownloaded": "0",
        "TargetBuildID": "9999",
        "InstalledDepots": {str(appid + 1): {"manifest": "555", "size": "42"}},
        "UserConfig": {"language": "english"},
    }
    (library / "steamapps" / f"appmanifest_{appid}.acf").write_text(dump_vdf({"AppState": state}), encoding="utf-8")
    return folder


def game_files() -> dict[str, bytes]:
    return {
        "Game.exe": os.urandom(200 * KB),
        "Data/pak0.pak": os.urandom(3 * 1024 * KB + 17),
        "Data Files/naïve ü.txt": "ünïcode".encode("utf-8"),
        "empty.txt": b"",
        ".hidden": b"dot files are part of the game too",
    }


def wait_list(library: SteamLibrary, appid: int) -> list[dict]:
    """The whole file list of a shared game, read page by page as a peer would."""
    files: list[dict] = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        payload = library.file_list(appid, len(files))
        assert payload is not None and payload["error"] is None
        files += payload["files"]
        if payload["complete"]:
            return files
        if not payload["files"]:
            time.sleep(0.05)
    raise AssertionError("the file list never completed")


# ----- the KeyValues format -------------------------------------------------------------------

def test_vdf_round_trip():
    text = '''// a comment
"AppState"
{
\t"appid"\t\t"620"
\t"name"\t\t"Portal \\"2\\""
\t"LauncherPath"\t\t"C:\\\\Program Files (x86)\\\\Steam\\\\steam.exe"
\t"cond"\t\t"1" [$WIN32]
\t"Depots" { "621" { "manifest" "1" } }
\tunquoted value
}
'''
    data = parse_vdf(text)
    state = data["AppState"]
    assert state["name"] == 'Portal "2"'
    assert state["LauncherPath"] == "C:\\Program Files (x86)\\Steam\\steam.exe"
    assert state["cond"] == "1"
    assert state["Depots"] == {"621": {"manifest": "1"}}
    assert state["unquoted"] == "value"
    assert parse_vdf(dump_vdf(data)) == data


@pytest.mark.parametrize("bad", ['"a" {', '"a" "b" }', '"a', '{ "a" "b" }', '"a" "b" "c"'])
def test_vdf_rejects_broken_files(bad):
    with pytest.raises(steam_module.VdfError):
        parse_vdf(bad)


# ----- libraries and games ----------------------------------------------------------------------

def test_libraries_and_games(tmp_path):
    root, second = tmp_path / "Steam", tmp_path / "Library 2"
    make_steam(root, second, tmp_path / "gone")
    (tmp_path / "gone" / "steamapps").rmdir()  # a library on a drive that isn't plugged in
    add_game(root, 100, "Alpha", {"a.bin": b"a"}, updated=10)
    add_game(second, 100, "Alpha", {"a.bin": b"newer"}, updated=20, flags=6)  # the same game, updated later
    add_game(second, 200, "Beta", {"b.bin": b"b"}, flags=1026)  # being updated: listed, not shared
    add_game(root, 228980, "Steamworks Common Redistributables", {"r.bin": b"r"})
    add_game(root, 300, "Escape", {"x": b"x"}, installdir="..")
    (root / "steamapps" / "appmanifest_400.acf").write_text('"AppState" { "appid" "401" }', encoding="utf-8")

    libraries = library_folders(root)
    assert [p.name for p in libraries] == ["Steam", "Library 2"]
    games = scan_games(libraries)
    assert sorted(games) == [100, 200]
    assert games[100].library == second and games[100].shareable
    assert games[200].name == "Beta" and not games[200].shareable


def test_old_style_library_list(tmp_path):
    root, other = tmp_path / "Steam", tmp_path / "Other"
    (root / "steamapps").mkdir(parents=True)
    (other / "steamapps").mkdir(parents=True)
    vdf = dump_vdf({"LibraryFolders": {"TimeNextStatsReport": "1", "1": str(other)}})
    (root / "steamapps" / "libraryfolders.vdf").write_text(vdf, encoding="utf-8")
    assert [p.name for p in library_folders(root)] == ["Other", "Steam"]


def test_portable_manifest_leaves_out_the_sharer():
    state = {"appid": "620", "name": "Portal 2", "installdir": "Portal 2", "StateFlags": "518", "buildid": "77",
             "TargetBuildID": "99", "LastOwner": "7656119", "LauncherPath": "C:\\steam.exe", "LastPlayed": "5",
             "BytesToDownload": "300", "BytesDownloaded": "0", "UpdateResult": "2", "StagingFolder": "1",
             "InstalledDepots": {"621": {"manifest": "1", "size": "2"}}}
    text = portable_manifest(dump_vdf({"AppState": state}), 620, "Portal 2")
    out = parse_vdf(text)["AppState"]
    for gone in ("LastOwner", "LauncherPath", "LastPlayed", "StagingFolder"):
        assert gone not in out
    assert out["StateFlags"] == "4" and out["TargetBuildID"] == "77" and out["UpdateResult"] == "0"
    assert out["BytesDownloaded"] == "300"
    assert out["InstalledDepots"] == {"621": {"manifest": "1", "size": "2"}}
    with pytest.raises(ValueError):
        portable_manifest(text, 621, "Portal 2")
    with pytest.raises(ValueError):
        portable_manifest(text, 620, "Portal 3")


# ----- sharing a game's files -----------------------------------------------------------------

def test_file_list_hashes_every_file(tmp_path, monkeypatch):
    root = make_steam(tmp_path / "Steam")
    files = game_files()
    folder = add_game(root, 100, "Alpha", files, dirs=("Saves/Slot 1",))
    (folder / "Data" / "pak1.pak.lightninglan-part").write_bytes(b"a copy in progress here")
    monkeypatch.setattr(steam_module, "PAGE_SIZE", 2)  # several pages

    library = SteamLibrary(root, tmp_path / "cache.json", sharing=True)
    library.scan()
    listed = wait_list(library, 100)
    assert [f["p"] for f in listed] == sorted(files)
    for entry in listed:
        assert entry["s"] == len(files[entry["p"]])
        assert entry["h"] == hashlib.sha256(files[entry["p"]]).hexdigest()
    final = library.file_list(100, len(listed))
    assert final["complete"] and final["dirs"] == ["Saves/Slot 1"]
    assert "LastOwner" not in final["acf"] and '"StateFlags"\t\t"4"' in final["acf"]

    # The hashes are kept: a second run hashes nothing.
    calls = []
    real = steam_module.sha256_file
    monkeypatch.setattr(steam_module, "sha256_file", lambda *a: calls.append(a) or real(*a))
    again = SteamLibrary(root, tmp_path / "cache.json", sharing=True)
    again.scan()
    assert wait_list(again, 100) == listed and calls == []

    library.set_sharing(False)
    assert library.file_list(100) is None and library.listing() is None


def test_open_file_only_inside_the_game(tmp_path):
    root = make_steam(tmp_path / "Steam")
    folder = add_game(root, 100, "Alpha", {"a.txt": b"a"})
    (root / "steamapps" / "common" / "secret.txt").write_text("not part of the game")
    library = SteamLibrary(root, None, sharing=True)
    library.scan()
    wait_list(library, 100)
    assert library.open_file(100, "a.txt")[1:] == (1, os.stat(folder / "a.txt").st_mtime_ns)
    for bad in ("../secret.txt", "..\\secret.txt", "/etc/passwd", "C:/Windows/win.ini", "missing.txt", ""):
        assert library.open_file(100, bad) == "not_found"
    assert library.open_file(999, "a.txt") == "not_found"
    (folder / "a.txt").write_text("changed since it was hashed")
    assert library.open_file(100, "a.txt") == "changed"


def test_walk_skips_names_other_computers_cant_save(tmp_path):
    folder = tmp_path / "game"
    folder.mkdir()
    (folder / "ok.txt").write_text("ok")
    if os.name != "nt":
        (folder / "colon:name.txt").write_text("not on Windows")
    files, empty, skipped = walk_game(folder, threading.Event())
    assert [f[0] for f in files] == ["ok.txt"] and empty == []
    assert skipped == (0 if os.name == "nt" else 1)


def test_file_list_entries_are_checked():
    sha = "ab" * 32
    assert _file_entry({"p": "Data/x.bin", "s": 3, "h": sha, "x": 1}) == ("Data/x.bin", 3, sha, True, None)
    big = 5 * 1024 * KB  # five pieces
    pieces = os.urandom(5 * 32)
    ph = base64.b64encode(pieces).decode()
    assert _file_entry({"p": "big.pak", "s": big, "h": sha, "ph": ph}) == ("big.pak", big, sha, False, pieces)
    assert _file_entry({"p": "small", "s": 3, "h": sha, "ph": ph})[4] is None  # one piece: copied whole anyway
    for bad in ({"p": "../x", "s": 1, "h": sha}, {"p": "C:/x", "s": 1, "h": sha}, {"p": "/x", "s": 1, "h": sha},
                {"p": "a", "s": -1, "h": sha}, {"p": "a", "s": 1, "h": "nope"}, {"p": "a/b:c", "s": 1, "h": sha},
                {"p": "x.lightninglan-part", "s": 1, "h": sha}, "x", None,
                {"p": "big.pak", "s": big, "h": sha, "ph": ph[:-8]}, {"p": "big.pak", "s": big, "h": sha, "ph": "!!"},
                {"p": "big.pak", "s": big, "h": sha, "ph": 7}):
        assert _file_entry(bad) is None


def test_big_files_are_listed_with_piece_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(steam_module, "PIECED_SIZE", 2 * 1024 * KB)
    root = make_steam(tmp_path / "Steam")
    files = game_files()  # Data/pak0.pak is just over 3 MiB: four pieces
    folder = add_game(root, 100, "Alpha", files)
    library = SteamLibrary(root, tmp_path / "cache.json", sharing=True)
    library.scan()
    listed = {f["p"]: f for f in wait_list(library, 100)}
    sha, digests = hash_file(folder / "Data" / "pak0.pak")
    assert listed["Data/pak0.pak"]["h"] == sha and base64.b64decode(listed["Data/pak0.pak"]["ph"]) == digests
    assert all("ph" not in f for p, f in listed.items() if p != "Data/pak0.pak")

    # The cache keeps them. A cache from 1.6, without piece hashes, has big files hashed again.
    calls = []
    real = steam_module.hash_file
    monkeypatch.setattr(steam_module, "hash_file", lambda *a: calls.append(a) or real(*a))
    again = SteamLibrary(root, tmp_path / "cache.json", sharing=True)
    again.scan()
    assert {f["p"]: f for f in wait_list(again, 100)} == listed and calls == []
    cache = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
    cache["files"] = {k: v[:3] for k, v in cache["files"].items()}
    (tmp_path / "cache.json").write_text(json.dumps(cache), encoding="utf-8")
    old = SteamLibrary(root, tmp_path / "cache.json", sharing=True)
    old.scan()
    assert {f["p"]: f for f in wait_list(old, 100)} == listed and len(calls) == 1


def test_file_list_pages_are_cut_by_size(tmp_path, monkeypatch):
    monkeypatch.setattr(steam_module, "PIECED_SIZE", 1024 * KB + 1)
    monkeypatch.setattr(steam_module, "PAGE_PIECE_BYTES", 1)  # one big file a page
    root = make_steam(tmp_path / "Steam")
    files = {f"Data/pak{i}.pak": os.urandom(1024 * KB + 1 + i) for i in range(3)}
    add_game(root, 100, "Alpha", files)
    library = SteamLibrary(root, None, sharing=True)
    library.scan()
    listed = wait_list(library, 100)
    assert sorted(f["p"] for f in listed) == sorted(files)
    first = library.file_list(100, 0)
    assert len(first["files"]) == 1 and first["listed"] == 3 and not first["complete"]


def test_which_games_a_peer_is_copying():
    good = {"steam": {"v": 1, "os": "windows", "games": [],
                      "copying": [{"appid": 100, "buildid": "77"}, {"appid": 200, "buildid": "8"}]}}
    assert parse_steam_copying(good) == {(100, "77"), (200, "8")}
    for bad in ({}, {"steam": None}, {"steam": {"copying": "x"}},
                {"steam": {"copying": [{"appid": "100", "buildid": "77"}, {"appid": 0, "buildid": "1"},
                                       {"appid": 100, "buildid": "seven"}, {"appid": True, "buildid": "1"}, 5]}}):
        assert parse_steam_copying(bad) == frozenset()


def test_lan_routes(tmp_path):
    root = make_steam(tmp_path / "Steam")
    files = game_files()
    add_game(root, 100, "Alpha", files)
    peer = InProcessPeer(tmp_path / "peer")
    peer.node.steam = SteamLibrary(root, None, sharing=True)
    peer.node.steam.scan()

    index = peer.get("/api/files", client=LAN_CLIENT).json()
    assert index["steam"]["games"] == [{"appid": 100, "name": "Alpha", "size": sum(map(len, files.values())),
                                        "buildid": "1234", "updated": 1700000000}]
    wait_list(peer.node.steam, 100)
    listed = peer.get("/api/steam/100", client=LAN_CLIENT).json()
    assert listed["complete"] and len(listed["files"]) == len(files)
    r = peer.get("/api/steam/100/Data Files/naïve ü.txt", client=LAN_CLIENT)
    assert r.status_code == 200 and r.content == files["Data Files/naïve ü.txt"]
    r = peer.get("/api/steam/100/Data/pak0.pak", client=LAN_CLIENT, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == files["Data/pak0.pak"][10:20]
    assert peer.get("/api/steam/100/..%2F..%2Flibraryfolders.vdf", client=LAN_CLIENT).status_code == 404
    assert peer.get("/api/steam/200", client=LAN_CLIENT).status_code == 404
    assert peer.get("/api/steam-copy/100", client=LAN_CLIENT).status_code == 404  # it isn't getting the game
    # The Steam button and getting games are for this computer only.
    assert peer.get("/api/my-steam", client=LAN_CLIENT).status_code == 403
    assert peer.post("/api/my-steam/share", client=LAN_CLIENT, json={"on": False},
                     headers={"X-LightningLAN-Client": "x"}).status_code == 403
    common = peer.get("/api/common").json()
    assert [(g["appid"], g["local"], g["installed"]) for g in common["games"]] == [(100, True, True)]


# ----- copying a game from one peer into another one's Steam library -----------------------------

@pytest.mark.integration
def test_get_a_game_into_another_steam_library(spawn, tmp_path):
    files = game_files()
    a_steam = make_steam(tmp_path / "A Steam")
    add_game(a_steam, 100, "Alpha Game", files, dirs=("Saves",))
    b_steam = make_steam(tmp_path / "B Steam", tmp_path / "B Games")
    # With a passphrase every request is signed, file paths included.
    a = spawn("A", start=False, passphrase="steam night", extra_args=("--steam-dir", str(a_steam)))
    b = spawn("B", start=False, passphrase="steam night", extra_args=("--steam-dir", str(b_steam)))
    start_together(a, b)

    assert b.get("/api/my-steam").json()["found"]
    r = a.post("/api/my-steam/share", json={"on": True})
    assert r.status_code == 200 and r.json()["sharing"] and r.json()["shared"] == 1

    def listed():
        games = b.get("/api/common").json()["games"]
        return games and games[0]["installable"] and games[0]

    game = wait_for(listed, timeout=60, message="B to list A's game")
    assert game["name"] == "Alpha Game" and not game["installed"]

    library = str(tmp_path / "B Games")
    r = b.post("/api/my-steam/get", json={"appid": 100, "library": library})
    assert r.status_code == 200, r.text
    d = wait_download(b, r.json()["id"], timeout=120)
    assert d["status"] == "completed", (d["error"], d["note"])
    assert d["kind"] == "steam" and d["files"] == {"done": len(files), "total": len(files)}

    folder = tmp_path / "B Games" / "steamapps" / "common" / "Alpha Game"
    assert d["path"] == str(folder)
    for rel, data in files.items():
        assert sha256_of(folder / rel) == hashlib.sha256(data).hexdigest(), rel
    assert (folder / "Saves").is_dir()
    assert not list(folder.rglob("*.lightninglan-part"))
    acf = parse_vdf((tmp_path / "B Games" / "steamapps" / "appmanifest_100.acf").read_text(encoding="utf-8"))
    state = acf["AppState"]
    assert state["StateFlags"] == "4" and state["installdir"] == "Alpha Game" and state["buildid"] == "1234"
    assert "LastOwner" not in state and "LauncherPath" not in state

    # Now B has it: no second copy, and ThunderCLOUD says so.
    wait_for(lambda: b.get("/api/common").json()["games"][0]["installed"], timeout=60,
             message="B to see the game in its library")
    again = b.post("/api/my-steam/get", json={"appid": 100})
    assert again.status_code == 409 and "already in your Steam library" in again.json()["detail"]


@pytest.mark.integration
def test_pause_resume_and_cancel_a_game(spawn, tmp_path):
    big = {f"Data/pak{i}.pak": os.urandom(6 * 1024 * KB) for i in range(4)}
    a_steam = make_steam(tmp_path / "A Steam")
    add_game(a_steam, 100, "Slow Game", big)
    add_game(a_steam, 200, "Other Game", big)
    b_steam = make_steam(tmp_path / "B Steam")
    # A sends at 4 MB/s, so there's time to pause and cancel.
    a = spawn("A", start=False, extra_args=("--steam-dir", str(a_steam), "--max-upload-rate", "4"))
    b = spawn("B", start=False, extra_args=("--steam-dir", str(b_steam)))
    start_together(a, b)
    assert a.post("/api/my-steam/share", json={"on": True}).json()["shared"] == 2
    wait_for(lambda: len(b.get("/api/common").json()["games"]) == 2, timeout=60, message="B to list both games")

    d = b.post("/api/my-steam/get", json={"appid": 100}).json()
    wait_for(lambda: get_download(b, d["id"])["received"] > 2 * 1024 * KB, timeout=60,
             message="some of it to arrive")
    assert b.post(f"/api/downloads/{d['id']}/pause").json()["status"] == "paused"
    paused = get_download(b, d["id"])
    assert 0 < paused["received"] < paused["size"]
    assert b.post(f"/api/downloads/{d['id']}/resume").status_code == 200
    done = wait_download(b, d["id"], timeout=120)
    assert done["status"] == "completed", (done["error"], done["note"])
    folder = b_steam / "steamapps" / "common" / "Slow Game"
    for rel, data in big.items():
        assert sha256_of(folder / rel) == hashlib.sha256(data).hexdigest()

    other = b.post("/api/my-steam/get", json={"appid": 200}).json()
    wait_for(lambda: get_download(b, other["id"])["received"] > 1024 * KB, timeout=60,
             message="some of the other game to arrive")
    assert b.post(f"/api/downloads/{other['id']}/cancel").json()["status"] == "canceled"
    assert not (b_steam / "steamapps" / "common" / "Other Game").exists()
    assert not (b_steam / "steamapps" / "appmanifest_200.acf").exists()


@pytest.mark.integration
def test_peers_getting_the_same_game_pass_it_on(spawn, tmp_path):
    # A's upload is slow. B and C get the game at the same time: what one of them has, the other gets
    # from it, small files whole and the big one (with piece hashes) piece by piece.
    files = {f"Data/{i:02}.dat": os.urandom(512 * KB) for i in range(24)}
    files["Data/big.pak"] = os.urandom(steam_module.PIECED_SIZE + 5 * 1024 * KB)
    size = sum(map(len, files.values()))
    a_steam = make_steam(tmp_path / "A Steam")
    add_game(a_steam, 100, "Swarm Game", files)
    b_steam, c_steam = make_steam(tmp_path / "B Steam"), make_steam(tmp_path / "C Steam")
    a = spawn("A", start=False, extra_args=("--steam-dir", str(a_steam), "--max-upload-rate", "8"))
    b = spawn("B", start=False, extra_args=("--steam-dir", str(b_steam)))
    c = spawn("C", start=False, extra_args=("--steam-dir", str(c_steam)))
    start_together(a, b, c)
    assert a.post("/api/my-steam/share", json={"on": True}).json()["shared"] == 1
    for p in (b, c):
        wait_for(lambda p=p: p.get("/api/common").json()["games"], timeout=60, message=f"{p.name} to list the game")

    ids = {p.name: p.post("/api/my-steam/get", json={"appid": 100}).json()["id"] for p in (b, c)}
    done = {p.name: wait_download(p, ids[p.name], timeout=180) for p in (b, c)}
    for p, steam_dir in ((b, b_steam), (c, c_steam)):
        d = done[p.name]
        assert d["status"] == "completed", (d["error"], d["note"])
        folder = steam_dir / "steamapps" / "common" / "Swarm Game"
        for rel, data in files.items():
            assert sha256_of(folder / rel) == hashlib.sha256(data).hexdigest(), (p.name, rel)
        assert not list(folder.rglob("*.lightninglan-part"))
    from_c = next((x["bytes"] for x in done["B"]["peers"] if x["peer_id"] == c.peer_id), 0)
    from_b = next((x["bytes"] for x in done["C"]["peers"] if x["peer_id"] == b.peer_id), 0)
    sent_by_a = a.get("/api/status").json()["uploaded_bytes"]
    assert from_b > 0 and from_c > 0, (from_b, from_c)
    assert sent_by_a < 1.5 * size, f"A sent {sent_by_a / size:.2f}x the game for two copies"
    print(f"two copies of {size / 1024 / KB:.0f} MiB: A sent {sent_by_a / size:.2f}x, "
          f"B got {from_c / 1024 / KB:.0f} MiB from C, C got {from_b / 1024 / KB:.0f} MiB from B")
