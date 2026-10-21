import hashlib

import pytest

from .harness import common_files, source_ids, start_together, wait_for

pytestmark = pytest.mark.integration


def test_files_shared_on_a_appear_on_b_and_c(spawn):
    a, b, c = (spawn(name, start=False) for name in ("A", "B", "C"))
    (a.share / "report.pdf").write_bytes(b"%PDF-1.7 pretend report")
    (a.share / "photos").mkdir()
    (a.share / "photos" / "cat.jpg").write_bytes(b"\xff\xd8 meow")
    start_together(a, b, c)

    for viewer in (b, c):
        def has_a_files():
            files = common_files(viewer)
            return files if {"report.pdf", "photos/cat.jpg"} <= set(files) else None

        items = wait_for(has_a_files, timeout=30, message=f"{viewer.name} to list A's files")
        report = items["report.pdf"]
        assert source_ids(report) == {a.peer_id}
        assert report["sources"][0]["name"] == "A"
        assert report["size"] == len(b"%PDF-1.7 pretend report")
        assert report["id"] == hashlib.sha256(b"%PDF-1.7 pretend report").hexdigest()
        assert report["local"] is False

    # A file added while everything is running shows up too.
    (a.share / "notes.txt").write_text("added later")
    for viewer in (b, c):
        wait_for(lambda: "notes.txt" in common_files(viewer), timeout=30, message=f"{viewer.name} to see notes.txt")

    # A sees its own files as local.
    own = common_files(a)
    assert own["report.pdf"]["local"] is True
    assert own["report.pdf"]["sources"][0]["self"] is True


def test_same_file_on_two_peers_is_shown_once_with_both_sources(spawn):
    a, b, c = (spawn(name, start=False) for name in ("A", "B", "C"))
    payload = b"identical bytes on two machines"
    file_id = hashlib.sha256(payload).hexdigest()
    (a.share / "shared.bin").write_bytes(payload)
    (b.share / "shared-copy.bin").write_bytes(payload)  # different name, same content
    (c.share / "only-on-c.txt").write_text("c")
    start_together(a, b, c)

    def merged_rows():
        rows = [f for f in c.get("/api/common").json()["files"] if f["id"] == file_id]
        return rows if rows and source_ids(rows[0]) == {a.peer_id, b.peer_id} else None

    rows = wait_for(merged_rows, timeout=30, message="C to merge the copies on A and B")
    assert len(rows) == 1
    assert {rows[0]["name"], *rows[0]["aliases"]} == {"shared.bin", "shared-copy.bin"}

    wait_for(lambda: "only-on-c.txt" in common_files(a), timeout=30, message="A to see C's file")
    assert source_ids(common_files(a)["only-on-c.txt"]) == {c.peer_id}


def test_removed_file_disappears_from_other_peers(spawn):
    a, b = (spawn(name, start=False) for name in ("A", "B"))
    (a.share / "temporary.txt").write_text("soon gone")
    start_together(a, b)
    wait_for(lambda: "temporary.txt" in common_files(b), timeout=30, message="B to see the file")
    (a.share / "temporary.txt").unlink()
    wait_for(lambda: "temporary.txt" not in common_files(b), timeout=30, message="B to drop the file")
