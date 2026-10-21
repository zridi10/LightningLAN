"""Stop and Start LightningLAN (going offline without quitting), and closing the web UI."""

import io
import json
import time
from contextlib import contextmanager

import httpx
import pytest

from lightninglan.node import UI_CLOSE_GRACE
from lightninglan.serving import UploadLimiter, UploadsCut, _iter_file

from .harness import (
    common_files,
    get_download,
    make_random_file,
    sha256_of,
    start_together,
    wait_download,
    wait_for,
    wait_until_listed,
)

MB = 1024 * 1024


# ----- unit ----------------------------------------------------------------------------------

def test_cut_off_ends_transfers_in_progress_and_refuses_new_ones_until_reopened():
    limiter = UploadLimiter(None)
    stream = _iter_file(io.BytesIO(b"x" * MB), 0, MB, limiter)
    next(stream)
    limiter.cut_off()
    with pytest.raises(UploadsCut):
        next(stream)
    with pytest.raises(UploadsCut):  # started while offline
        next(_iter_file(io.BytesIO(b"x" * MB), 0, MB, limiter))
    limiter.reopen()
    assert next(_iter_file(io.BytesIO(b"x" * MB), 0, MB, limiter))


def test_stop_and_start_in_process(peer):
    (peer.share / "a.txt").write_text("a")
    peer.scan()
    assert peer.get("/api/status").json()["online"] is True

    status = peer.post("/api/stop", headers={"X-LightningLAN-Client": "ui"}).json()
    assert (status["online"], status["offline_reason"]) == (False, "stopped")
    from .conftest import LAN_CLIENT

    r = peer.get("/api/files", client=LAN_CLIENT)
    assert r.status_code == 503 and r.json()["error"] == "offline"
    r = peer.post("/api/downloads", json={"file_id": "0" * 64}, headers={"X-LightningLAN-Client": "ui"})
    assert r.status_code == 409 and "Start LightningLAN" in r.json()["detail"]
    r = peer.post("/api/downloads/resume-all", headers={"X-LightningLAN-Client": "ui"})
    assert r.status_code == 409
    assert peer.post("/api/downloads/pause-all", headers={"X-LightningLAN-Client": "ui"}).status_code == 200
    assert peer.get("/api/status").json()["online"] is False  # the local UI keeps working

    status = peer.post("/api/start", headers={"X-LightningLAN-Client": "ui"}).json()
    assert (status["online"], status["offline_reason"]) == (True, None)
    assert peer.get("/api/files", client=LAN_CLIENT).status_code == 200
    peer.request("POST", "/api/stop", headers={"X-LightningLAN-Client": "ui"})  # stops the peer table again


# ----- integration ---------------------------------------------------------------------------

@contextmanager
def ui_tab(peer):
    """Behave like an open web UI tab: hold the session stream open. Yields its first message."""
    with httpx.Client(timeout=30, trust_env=False) as client:
        with client.stream("GET", f"{peer.url}/api/ui-session") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            lines = resp.iter_lines()  # keep it: dropping it closes the connection, like closing the tab
            for line in lines:
                if line.startswith("data:"):
                    break
            yield json.loads(line[len("data:"):])


def status(peer) -> dict:
    return peer.get("/api/status").json()


@pytest.mark.integration
def test_stop_hides_this_peer_and_start_brings_it_back(spawn):
    a, b = spawn("A", start=False), spawn("B", start=False)
    (a.share / "from-a.txt").write_text("a")
    start_together(a, b)
    wait_for(lambda: "from-a.txt" in common_files(b), timeout=30, message="B to see A's file")

    assert a.post("/api/stop").json()["online"] is False
    wait_for(lambda: a.peer_id not in b.online_peer_ids(), timeout=10, message="B to drop A")
    assert "from-a.txt" not in common_files(b)
    assert a.get("/api/files").status_code == 503
    assert a.get("/api/peers").json()["peers"] == []
    assert a.running, "Stop must not quit the program"

    assert a.post("/api/start").json()["online"] is True
    wait_for(lambda: "from-a.txt" in common_files(b), timeout=30, message="B to see A's file again")
    wait_for(lambda: b.peer_id in a.online_peer_ids(), timeout=30, message="A to see B again")


@pytest.mark.integration
def test_stop_pauses_downloads_and_start_resumes_them(spawn):
    a = spawn("A", start=False, extra_args=("--max-upload-rate", "4"))
    b = spawn("B", start=False)
    file_id = make_random_file(a.share / "big.bin", 24 * MB)
    start_together(a, b)
    wait_until_listed(b, file_id, a)

    download_id = b.post("/api/downloads", json={"file_id": file_id}).json()["id"]
    wait_for(lambda: get_download(b, download_id)["received"] >= 4 * MB, timeout=30, interval=0.05,
             message="4 MB to arrive")
    b.post("/api/stop")
    d = get_download(b, download_id)
    assert d["status"] == "paused", d
    kept = d["received"]
    assert b.post(f"/api/downloads/{download_id}/resume").status_code == 409  # not while stopped

    b.post("/api/start")
    d = wait_download(b, download_id, timeout=90, until=("completed", "failed"))
    assert d["status"] == "completed", d
    assert d["resumed_from"] >= kept > 0
    assert sha256_of(b.downloads / "big.bin") == file_id


@pytest.mark.integration
def test_stop_cuts_off_uploads_to_other_peers(spawn):
    a = spawn("A", start=False, extra_args=("--max-upload-rate", "4"))
    b = spawn("B", start=False)
    file_id = make_random_file(a.share / "big.bin", 24 * MB)
    start_together(a, b)
    wait_until_listed(b, file_id, a)

    download_id = b.post("/api/downloads", json={"file_id": file_id}).json()["id"]
    wait_for(lambda: get_download(b, download_id)["received"] >= 4 * MB, timeout=30, interval=0.05,
             message="4 MB to arrive")
    a.post("/api/stop")
    sent = status(a)["uploaded_bytes"]
    time.sleep(1.5)
    assert status(a)["uploaded_bytes"] == sent, "uploads went on after Stop"
    assert get_download(b, download_id)["status"] != "completed"

    a.post("/api/start")
    wait_until_listed(b, file_id, a)

    def finished():
        d = get_download(b, download_id)
        if d["status"] in ("failed", "paused"):
            b.post(f"/api/downloads/{download_id}/resume")
        return d if d["status"] == "completed" else None

    wait_for(finished, timeout=90, interval=0.5, message="the download to finish after A started again")
    assert sha256_of(b.downloads / "big.bin") == file_id
    assert "Exception in ASGI application" not in a.log_text()
    assert "without completing response" not in a.log_text()


@pytest.mark.integration
def test_closing_the_web_ui_stops_sharing_and_opening_it_starts_again(spawn):
    a = spawn("A")
    with ui_tab(a) as first:
        assert first == {"online": True}
    with ui_tab(a):  # a reload: the page comes back within the grace period
        pass
    with ui_tab(a):
        time.sleep(UI_CLOSE_GRACE + 2)
        assert status(a)["online"] is True, "a reload must not count as closing the page"

    closed_at = time.monotonic()
    wait_for(lambda: status(a)["online"] is False, timeout=UI_CLOSE_GRACE + 10, message="A to stop sharing")
    assert time.monotonic() - closed_at >= UI_CLOSE_GRACE - 1
    assert status(a)["offline_reason"] == "closed"
    assert a.running

    with ui_tab(a):  # opening the page again starts sharing again
        wait_for(lambda: status(a)["online"] is True, timeout=15, message="A to share again")

        a.post("/api/stop")  # an explicit Stop ...
    with ui_tab(a) as first:  # ... is not undone by reopening the page
        assert first == {"online": False}
        time.sleep(2)
        assert (status(a)["online"], status(a)["offline_reason"]) == (False, "stopped")


@pytest.mark.integration
def test_quit_is_prompt_with_the_web_ui_open(spawn):
    a = spawn("A")
    with ui_tab(a):
        started = time.monotonic()
        assert a.post("/api/shutdown").json() == {"ok": True}
        a.proc.wait(timeout=15)
    assert a.proc.returncode == 0
    assert time.monotonic() - started < 3, "an open web UI must not hold up quitting"
    assert "graceful shutdown exceeded" not in a.log_text()
