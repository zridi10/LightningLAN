"""Peers leaving: graceful shutdown, hard kills, and kills in the middle of a transfer."""

import time

import pytest

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

pytestmark = pytest.mark.integration

MB = 1024 * 1024


def test_graceful_shutdown_removes_peer_via_mdns_goodbye(spawn):
    a, b = spawn("A", start=False), spawn("B", start=False)
    (a.share / "from-a.txt").write_text("a")
    start_together(a, b)
    wait_for(lambda: "from-a.txt" in common_files(b), timeout=30, message="B to see A's file")

    a.stop()
    assert a.proc.returncode == 0
    wait_for(lambda: a.peer_id not in b.online_peer_ids(), timeout=10, message="B to drop A")
    assert "from-a.txt" not in common_files(b)
    assert a.peer_id not in b.ignored_peer_ids()


def test_killed_peer_is_removed_after_three_failed_health_checks(spawn):
    a, b = spawn("A", start=False), spawn("B", start=False)
    (a.share / "from-a.txt").write_text("a")
    start_together(a, b)
    wait_for(lambda: "from-a.txt" in common_files(b), timeout=30, message="B to see A's file")

    a.kill()  # no goodbye packet: only health checks can notice
    killed_at = time.monotonic()
    wait_for(lambda: a.peer_id not in b.online_peer_ids(), timeout=20, message="B to drop the dead peer")
    elapsed = time.monotonic() - killed_at
    assert elapsed < 15, f"took {elapsed:.1f}s"
    assert "from-a.txt" not in common_files(b)
    assert "offline after 3 failed checks" in b.log_text()


def test_restarted_peer_comes_back(spawn):
    a, b = spawn("A", start=False), spawn("B", start=False)
    (a.share / "from-a.txt").write_text("a")
    start_together(a, b)
    wait_for(lambda: a.peer_id in b.online_peer_ids(), timeout=30, message="B to see A")
    original_id = a.peer_id
    a.kill()
    wait_for(lambda: a.peer_id not in b.online_peer_ids(), timeout=20, message="B to drop A")
    a.start()
    assert a.peer_id == original_id, "peer ID must survive restarts"
    wait_for(lambda: "from-a.txt" in common_files(b), timeout=30, message="B to see A again")


def test_peer_killed_mid_transfer_shows_error_and_resumes_after_restart(spawn):
    a = spawn("A", start=False, extra_args=("--max-upload-rate", "8"))
    b = spawn("B", start=False)
    size = 40 * MB
    file_id = make_random_file(a.share / "movie.mkv", size)
    start_together(a, b)
    wait_until_listed(b, file_id, a)

    download_id = b.post("/api/downloads", json={"file_id": file_id}).json()["id"]
    wait_for(lambda: get_download(b, download_id)["received"] >= 5 * MB, timeout=30, interval=0.05,
             message="5 MB to arrive")
    a.kill()

    d = wait_download(b, download_id, timeout=30)
    assert d["status"] == "failed", d
    assert "A went offline during the transfer" in d["error"], d["error"]
    assert "press Resume" in d["error"]
    part = b.downloads / ".lightninglan-partial" / f"{file_id}.part"
    kept = part.stat().st_size
    assert 5 * MB <= kept < size
    assert not (b.downloads / "movie.mkv").exists()
    wait_for(lambda: a.peer_id not in b.online_peer_ids(), timeout=20, message="B to notice A is gone")

    # Resuming while A is gone fails cleanly and keeps the partial file.
    b.post(f"/api/downloads/{download_id}/resume")
    d = wait_download(b, download_id, timeout=30)
    assert d["status"] == "failed" and "No online peer" in d["error"]
    assert part.stat().st_size == kept

    # A comes back (same config, same peer ID): resume continues from the kept bytes.
    a.extra_args = ()
    a.start()
    wait_until_listed(b, file_id, a)
    b.post(f"/api/downloads/{download_id}/resume")
    d = wait_download(b, download_id, timeout=60)
    assert d["status"] == "completed", d
    assert d["resumed_from"] == kept
    assert sha256_of(b.downloads / "movie.mkv") == file_id


def test_download_fails_over_to_another_source(spawn):
    a = spawn("A", start=False, extra_args=("--max-upload-rate", "8"))
    c = spawn("C", start=False)
    b = spawn("B", start=False)
    size = 24 * MB
    file_id = make_random_file(a.share / "dataset.bin", size)
    (c.share / "dataset-copy.bin").write_bytes((a.share / "dataset.bin").read_bytes())
    start_together(a, b, c)
    wait_until_listed(b, file_id, a)
    wait_until_listed(b, file_id, c)

    download_id = b.post("/api/downloads", json={"file_id": file_id, "peer_id": a.peer_id}).json()["id"]
    wait_for(lambda: get_download(b, download_id)["received"] >= 4 * MB, timeout=30, interval=0.05,
             message="4 MB to arrive from A")
    assert get_download(b, download_id)["source"]["peer_id"] == a.peer_id
    a.kill()

    d = wait_download(b, download_id, timeout=60)
    assert d["status"] == "completed", d
    assert d["source"]["peer_id"] == c.peer_id
    assert d["resumed_from"] >= 4 * MB, "C should continue where A stopped, not start over"
    assert sha256_of(b.downloads / "dataset.bin") == file_id
