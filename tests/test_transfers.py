"""DownloadManager against a scriptable fake peer (misbehaving servers, hostile names)."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lanshare.auth import Authenticator
from lanshare.peers import ONLINE, Peer, PeerTable, RemoteFile
from lanshare.transfers import COMPLETED, FAILED, PARTIAL_DIR_NAME, DownloadManager

OWN_ID = "0" * 32
FAKE_ID = "f" * 32


class FakePeer:
    """Serves one file at /api/download/<sha>. Behaviour can be changed between requests."""

    def __init__(self, content: bytes):
        self.content = content
        self.honor_range = True
        self.cut_after: int | None = None  # close the connection after this many body bytes
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append(dict(self.headers))
                body, status, extra = outer.content, 200, {}
                m = re.match(r"bytes=(\d+)-$", self.headers.get("Range", ""))
                if m and outer.honor_range:
                    start = int(m.group(1))
                    body, status = outer.content[start:], 206
                    extra["Content-Range"] = f"bytes {start}-{len(outer.content) - 1}/{len(outer.content)}"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                if outer.cut_after is not None:
                    self.wfile.write(body[: outer.cut_after])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def setup(tmp_path):
    made: list[FakePeer] = []

    def _setup(content: bytes, name: str = "file.bin", size: int | None = None):
        fake = FakePeer(content)
        made.append(fake)
        table = PeerTable(OWN_ID, Authenticator(None, OWN_ID), refresh_interval=1.0)
        peer = Peer(FAKE_ID, "Fake", fake.port, ("127.0.0.1",))
        peer.state = ONLINE
        sha = hashlib.sha256(content).hexdigest()
        peer.files = (RemoteFile(sha, name, len(content) if size is None else size, 0.0),)
        table._peers[FAKE_ID] = peer
        downloads = tmp_path / "downloads"
        downloads.mkdir(exist_ok=True)
        return DownloadManager(downloads, Authenticator(None, OWN_ID), table), fake, sha, downloads

    yield _setup
    for fake in made:
        fake.close()


def wait_done(mgr: DownloadManager, download_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = next(x for x in mgr.list() if x["id"] == download_id)
        if d["status"] in (COMPLETED, FAILED, "paused", "canceled"):
            return d
        time.sleep(0.02)
    raise AssertionError("download did not finish")


def test_downloads_and_verifies(setup):
    content = os.urandom(3 * 1024 * 1024 + 5)
    mgr, fake, sha, downloads = setup(content)
    d = wait_done(mgr, mgr.start(sha).id)
    assert d["status"] == COMPLETED, d
    assert Path(d["path"]).read_bytes() == content
    assert not (downloads / PARTIAL_DIR_NAME / f"{sha}.part").exists()


@pytest.mark.parametrize(
    "hostile, expected",
    [("..\\..\\..\\evil.txt", "evil.txt"), ("../../etc/cron.d/job", "job"), ("C:\\Windows\\win.ini", "win.ini"),
     ("CON", "_CON"), ("sub/dir/report.pdf", "report.pdf")],
)
def test_hostile_remote_names_stay_inside_downloads_folder(setup, hostile, expected):
    mgr, fake, sha, downloads = setup(b"payload", name=hostile)
    d = wait_done(mgr, mgr.start(sha).id)
    assert d["status"] == COMPLETED, d
    saved = Path(d["path"])
    assert saved.parent == downloads
    assert saved.name == expected


def test_existing_file_gets_numbered_name(setup):
    mgr, fake, sha, downloads = setup(b"new content", name="report.txt")
    (downloads / "report.txt").write_text("already here")
    (downloads / "report (1).txt").write_text("also here")
    d = wait_done(mgr, mgr.start(sha).id)
    assert Path(d["path"]).name == "report (2).txt"
    assert (downloads / "report.txt").read_text() == "already here"


def test_resumes_existing_partial_with_range_request(setup):
    content = os.urandom(1024 * 1024)
    mgr, fake, sha, downloads = setup(content)
    part = downloads / PARTIAL_DIR_NAME / f"{sha}.part"
    part.parent.mkdir()
    part.write_bytes(content[:300_000])
    d = wait_done(mgr, mgr.start(sha).id)
    assert d["status"] == COMPLETED
    assert d["resumed_from"] == 300_000
    assert fake.requests[-1]["Range"] == "bytes=300000-"
    assert Path(d["path"]).read_bytes() == content


def test_server_ignoring_range_restarts_cleanly(setup):
    content = os.urandom(500_000)
    mgr, fake, sha, downloads = setup(content)
    fake.honor_range = False
    part = downloads / PARTIAL_DIR_NAME / f"{sha}.part"
    part.parent.mkdir()
    part.write_bytes(content[:100_000])
    d = wait_done(mgr, mgr.start(sha).id)
    assert d["status"] == COMPLETED
    assert Path(d["path"]).read_bytes() == content


def test_damaged_partial_is_detected_and_downloaded_again(setup):
    content = os.urandom(800_000)
    mgr, fake, sha, downloads = setup(content)
    part = downloads / PARTIAL_DIR_NAME / f"{sha}.part"
    part.parent.mkdir()
    part.write_bytes(b"\x00" * 200_000)  # wrong bytes: verification must catch this
    d = wait_done(mgr, mgr.start(sha).id)
    assert d["status"] == COMPLETED, d
    assert Path(d["path"]).read_bytes() == content
    assert "Range" not in fake.requests[-1]  # second attempt started from zero


def test_corrupted_data_is_rejected(setup):
    content = os.urandom(400_000)
    mgr, fake, sha, downloads = setup(content)
    fake.content = content[:-1] + bytes([content[-1] ^ 0xFF])  # same size, one bit-flipped byte
    d = wait_done(mgr, mgr.start(sha).id)
    assert d["status"] == FAILED
    assert "verification" in d["error"]
    assert [p.name for p in downloads.iterdir() if p.is_file()] == []
    assert not (downloads / PARTIAL_DIR_NAME / f"{sha}.part").exists()


def test_peer_sending_more_than_advertised_is_rejected(setup):
    content = os.urandom(10_000)
    mgr, fake, sha, downloads = setup(content, size=5_000)
    d = wait_done(mgr, mgr.start(sha).id)
    assert d["status"] == FAILED
    assert [p.name for p in downloads.iterdir() if p.is_file()] == []


def test_connection_cut_mid_transfer_keeps_partial_and_resume_finishes(setup):
    content = os.urandom(2 * 1024 * 1024)
    mgr, fake, sha, downloads = setup(content)
    fake.cut_after = 700_000
    d = wait_done(mgr, mgr.start(sha).id)
    assert d["status"] == FAILED
    assert "went offline during the transfer" in d["error"] or "ended the transfer early" in d["error"]
    part = downloads / PARTIAL_DIR_NAME / f"{sha}.part"
    assert 0 < part.stat().st_size <= 700_000
    kept = part.stat().st_size

    fake.cut_after = None
    mgr.resume(d["id"])
    d = wait_done(mgr, d["id"])
    assert d["status"] == COMPLETED
    assert d["resumed_from"] == kept
    assert Path(d["path"]).read_bytes() == content


def test_starting_a_failed_download_again_resumes_the_same_entry(setup):
    content = os.urandom(1024 * 1024)
    mgr, fake, sha, downloads = setup(content)
    fake.cut_after = 300_000
    first = wait_done(mgr, mgr.start(sha).id)
    assert first["status"] == FAILED
    fake.cut_after = None
    again = mgr.start(sha)  # the UI's Download button on the same file
    assert again.id == first["id"], "must reuse the entry (and its partial file), not start a second one"
    d = wait_done(mgr, again.id)
    assert d["status"] == COMPLETED
    assert d["resumed_from"] > 0
    assert len(mgr.list()) == 1


def test_start_without_sources(setup):
    from lanshare.transfers import DownloadError

    mgr, fake, sha, downloads = setup(b"x")
    with pytest.raises(DownloadError):
        mgr.start("e" * 64)
    with pytest.raises(DownloadError):
        mgr.start("../../etc/passwd")


def test_changing_the_downloads_folder_affects_new_downloads_only(setup, tmp_path):
    content = os.urandom(1024 * 1024)
    mgr, fake, sha, downloads = setup(content)
    fake.cut_after = 300_000
    first = wait_done(mgr, mgr.start(sha).id)
    assert first["status"] == FAILED
    new_folder = tmp_path / "new downloads"
    new_folder.mkdir()
    mgr.downloads_dir = new_folder  # what the Settings panel does

    fake.cut_after = None
    resumed = wait_done(mgr, mgr.start(sha).id)  # the partial file is in the old folder: finish there
    assert resumed["status"] == COMPLETED and resumed["resumed_from"] > 0
    assert Path(resumed["path"]).parent == downloads

    again = wait_done(mgr, mgr.start(sha).id)
    assert again["id"] != resumed["id"]
    assert Path(again["path"]).parent == new_folder
    assert Path(again["path"]).read_bytes() == content
