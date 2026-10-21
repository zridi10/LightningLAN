import hashlib
import os
import time
from pathlib import Path

import pytest

from lightninglan.indexer import Indexer, is_ignored_name

from .conftest import can_symlink, make_junction, windows_only


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def names(indexer: Indexer) -> list[str]:
    return [e.relpath for e in indexer.snapshot.entries]


def test_indexes_files_with_size_and_hash(tmp_path):
    write(tmp_path / "a.txt", b"hello")
    write(tmp_path / "sub" / "b.bin", b"\x00" * 1000)
    write(tmp_path / "sub" / "deeper" / "c.txt", b"c")
    idx = Indexer(tmp_path)
    assert idx.scan() is True
    entries = {e.relpath: e for e in idx.snapshot.entries}
    assert set(entries) == {"a.txt", "sub/b.bin", "sub/deeper/c.txt"}
    assert entries["a.txt"].size == 5
    assert entries["a.txt"].sha256 == sha(b"hello")
    assert entries["sub/b.bin"].sha256 == sha(b"\x00" * 1000)
    assert idx.snapshot.pending == 0
    assert idx.lookup(sha(b"hello")).relpath == "a.txt"

    d = entries["a.txt"].to_dict()
    assert d["id"] == d["sha256"] == sha(b"hello")
    assert d["name"] == "a.txt" and d["size"] == 5
    assert abs(d["mtime"] - (tmp_path / "a.txt").stat().st_mtime) < 1


def test_large_file_hash_matches(tmp_path):
    data = os.urandom(3 * 1024 * 1024 + 17)  # spans several read chunks
    write(tmp_path / "big.bin", data)
    idx = Indexer(tmp_path)
    idx.scan()
    assert idx.snapshot.entries[0].sha256 == sha(data)


def test_skips_hidden_temp_and_junk_files(tmp_path):
    write(tmp_path / "keep.txt", b"k")
    for junk in [".hidden", "Thumbs.db", "desktop.ini", ".DS_Store", "movie.mkv.part", "x.tmp",
                 "~$report.docx", "dl.crdownload"]:
        write(tmp_path / junk, b"j")
    write(tmp_path / ".lightninglan-partial" / "abc.part", b"partial")
    write(tmp_path / ".git" / "config", b"git")
    idx = Indexer(tmp_path)
    idx.scan()
    assert names(idx) == ["keep.txt"]


@pytest.mark.parametrize("name", ["readme.txt", "photo.JPG", "notes"])
def test_is_ignored_name_allows_normal_files(name):
    assert not is_ignored_name(name)


def test_hashes_are_cached_until_size_or_mtime_changes(tmp_path):
    f = write(tmp_path / "a.txt", b"one")
    write(tmp_path / "b.txt", b"two")
    idx = Indexer(tmp_path)
    idx.scan()
    assert idx.hash_count == 2

    assert idx.scan() is False  # nothing changed: no re-hash, no new version
    assert idx.hash_count == 2

    f.write_bytes(b"one but longer")  # size changes
    assert idx.scan() is True
    assert idx.hash_count == 3
    assert idx.lookup(sha(b"one but longer")) is not None
    assert idx.lookup(sha(b"one")) is None

    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # mtime only
    idx.scan()
    assert idx.hash_count == 4


def test_deleted_files_disappear(tmp_path):
    f = write(tmp_path / "a.txt", b"a")
    idx = Indexer(tmp_path)
    idx.scan()
    f.unlink()
    assert idx.scan() is True
    assert names(idx) == []
    assert idx.lookup(sha(b"a")) is None


def test_hash_cache_persists_across_restarts(tmp_path):
    share = tmp_path / "share"
    cache = tmp_path / "state" / "hash_cache.json"
    write(share / "a.txt", b"a")
    write(share / "b.txt", b"b")
    first = Indexer(share, cache_path=cache)
    first.scan()
    assert first.hash_count == 2 and cache.exists()

    second = Indexer(share, cache_path=cache)
    second.scan()
    assert second.hash_count == 0
    assert names(second) == ["a.txt", "b.txt"]


def test_corrupt_hash_cache_is_ignored(tmp_path):
    share = tmp_path / "share"
    cache = tmp_path / "hash_cache.json"
    write(share / "a.txt", b"a")
    cache.write_text("{not json")
    idx = Indexer(share, cache_path=cache)
    idx.scan()
    assert names(idx) == ["a.txt"]


def test_version_increments_only_on_change(tmp_path):
    write(tmp_path / "a.txt", b"a")
    idx = Indexer(tmp_path)
    idx.scan()
    v = idx.snapshot.version
    idx.scan()
    assert idx.snapshot.version == v
    write(tmp_path / "b.txt", b"b")
    idx.scan()
    assert idx.snapshot.version > v


def test_symlink_escaping_root_is_not_indexed(tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("creating symlinks needs privileges on this machine")
    root, outside = tmp_path / "root", tmp_path / "outside"
    write(outside / "secret.txt", b"secret")
    write(root / "inside.txt", b"inside")
    os.symlink(outside / "secret.txt", root / "escape.txt")
    os.symlink(outside, root / "escape-dir", target_is_directory=True)
    os.symlink(root / "inside.txt", root / "alias.txt")
    idx = Indexer(root)
    idx.scan()
    assert names(idx) == ["alias.txt", "inside.txt"]


@windows_only
def test_junction_escaping_root_is_not_followed(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    write(outside / "secret.txt", b"secret")
    write(root / "inside.txt", b"inside")
    make_junction(outside, root / "junction")
    idx = Indexer(root)
    idx.scan()
    assert names(idx) == ["inside.txt"]


def test_background_thread_picks_up_new_files(tmp_path):
    write(tmp_path / "a.txt", b"a")
    idx = Indexer(tmp_path, interval=30)
    idx.start()
    try:
        deadline = time.monotonic() + 5
        while names(idx) != ["a.txt"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert names(idx) == ["a.txt"]
        write(tmp_path / "b.txt", b"b")
        idx.request_rescan()  # long interval: only the explicit wake-up can find it in time
        deadline = time.monotonic() + 5
        while names(idx) != ["a.txt", "b.txt"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert names(idx) == ["a.txt", "b.txt"]
    finally:
        idx.stop()


def test_switching_the_shared_folder(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.txt").write_bytes(b"from the first folder")
    (second / "b.txt").write_bytes(b"from the second folder")
    idx = Indexer(first)
    idx.scan()
    assert [e.relpath for e in idx.snapshot.entries] == ["a.txt"]
    version = idx.snapshot.version

    idx.set_root(second)
    assert idx.snapshot.entries == ()  # emptied at once, before the new folder is scanned
    assert idx.snapshot.version > version
    assert idx.lookup(sha(b"from the first folder")) is None
    idx.scan()
    assert [e.relpath for e in idx.snapshot.entries] == ["b.txt"]
    assert idx.root == second.resolve()
