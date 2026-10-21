"""Downloads between real peer processes: large files, interruption and resume, corruption."""

import os

import pytest

from .harness import (
    make_random_file,
    process_tree_memory,
    sha256_of,
    start_together,
    wait_download,
    wait_for,
    wait_until_listed,
    get_download,
)

pytestmark = pytest.mark.integration

MB = 1024 * 1024


def partial_path(peer, file_id):
    return peer.downloads / ".lightninglan-partial" / f"{file_id}.part"


def test_large_file_download_streams_and_verifies(spawn):
    a, b = spawn("A", start=False), spawn("B", start=False)
    size = 200 * MB
    file_id = make_random_file(a.share / "big.bin", size)
    start_together(a, b)
    wait_until_listed(b, file_id, a, timeout=120)

    peak = {"A": 0, "B": 0}

    def sample():
        peak["A"] = max(peak["A"], process_tree_memory(a))
        peak["B"] = max(peak["B"], process_tree_memory(b))

    r = b.post("/api/downloads", json={"file_id": file_id})
    assert r.status_code == 200, r.text
    d = wait_download(b, r.json()["id"], timeout=300, on_poll=sample)
    sample()
    assert d["status"] == "completed", d
    saved = b.downloads / "big.bin"
    assert d["path"] == str(saved)
    assert saved.stat().st_size == size
    assert sha256_of(saved) == file_id
    assert not partial_path(b, file_id).exists()
    # Streaming end to end: neither process came anywhere near holding the 200 MB file.
    assert peak["A"] < 150 * MB, f"serving peer peaked at {peak['A'] / MB:.0f} MB"
    assert peak["B"] < 150 * MB, f"downloading peer peaked at {peak['B'] / MB:.0f} MB"


def test_interrupted_download_resumes_where_it_stopped(spawn):
    # A serves at 8 MB/s so the transfer is slow enough to interrupt reliably.
    a = spawn("A", start=False, extra_args=("--max-upload-rate", "8"))
    b = spawn("B", start=False)
    size = 32 * MB
    file_id = make_random_file(a.share / "video.mp4", size)
    start_together(a, b)
    wait_until_listed(b, file_id, a)

    download_id = b.post("/api/downloads", json={"file_id": file_id}).json()["id"]
    wait_for(lambda: get_download(b, download_id)["received"] >= 6 * MB, timeout=30, interval=0.05,
             message="6 MB to arrive")
    paused = b.post(f"/api/downloads/{download_id}/pause").json()
    assert paused["status"] == "paused", paused

    # Pieces arrive out of order; "received" counts the verified ones, which are kept on disk.
    kept = paused["received"]
    assert 6 * MB <= kept < size
    assert paused["swarm"]["have"] * MB == kept  # 1 MiB pieces for a 32 MB file
    part = partial_path(b, file_id)
    with open(a.share / "video.mp4", "rb") as original, open(part, "rb") as partial:
        matching = 0
        for _ in range(size // MB):
            matching += original.read(MB) == partial.read(MB)
    assert matching * MB == kept, "every piece counted as received is on disk and correct"
    assert not (b.downloads / "video.mp4").exists()

    resumed = b.post(f"/api/downloads/{download_id}/resume").json()
    assert resumed["status"] in ("queued", "downloading")
    d = wait_download(b, download_id, timeout=60)
    assert d["status"] == "completed", d
    assert d["resumed_from"] == kept, "the second request must continue from the kept bytes"
    assert sha256_of(b.downloads / "video.mp4") == file_id
    assert not part.exists()


def test_partial_file_from_a_previous_run_is_resumed(spawn):
    a, b = spawn("A", start=False), spawn("B", start=False)
    size = 12 * MB
    file_id = make_random_file(a.share / "archive.zip", size)
    part = partial_path(b, file_id)
    part.parent.mkdir(parents=True)
    with open(a.share / "archive.zip", "rb") as src:
        part.write_bytes(src.read(5 * MB))  # as if B was closed mid-download last time
    start_together(a, b)
    wait_until_listed(b, file_id, a)

    d = wait_download(b, b.post("/api/downloads", json={"file_id": file_id}).json()["id"])
    assert d["status"] == "completed", d
    assert d["resumed_from"] == 5 * MB
    assert sha256_of(b.downloads / "archive.zip") == file_id


def test_corrupted_transfer_is_rejected(spawn):
    a, b = spawn("A", start=False), spawn("B", start=False)
    target = a.share / "data.bin"
    file_id = make_random_file(target, 4 * MB)
    start_together(a, b)
    wait_until_listed(b, file_id, a)

    # Flip one byte on A but keep size and mtime, so A keeps advertising the old hash and
    # serves bytes that no longer match it: exactly what corruption in transit looks like.
    st = target.stat()
    with open(target, "r+b") as f:
        f.seek(2 * MB)
        byte = f.read(1)
        f.seek(2 * MB)
        f.write(bytes([byte[0] ^ 0xFF]))
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert target.stat().st_mtime_ns == st.st_mtime_ns

    d = wait_download(b, b.post("/api/downloads", json={"file_id": file_id}).json()["id"])
    assert d["status"] == "failed", d
    assert "SHA-256 verification" in d["error"]
    assert not (b.downloads / "data.bin").exists()
    assert not partial_path(b, file_id).exists(), "corrupt data must be discarded"


def test_download_of_unknown_file_is_404(spawn):
    b = spawn("B")
    r = b.post("/api/downloads", json={"file_id": "ab" * 32})
    assert r.status_code == 404
    assert "No online peer" in r.json()["detail"]
