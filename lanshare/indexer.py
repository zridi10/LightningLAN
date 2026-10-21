"""Index of the shared folder: relative path, size, mtime and SHA-256 of every file.

Hashes are cached (in memory and in a JSON file) keyed by absolute path, and a file is only
re-hashed when its size or modification time changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .pathsafe import is_within

log = logging.getLogger(__name__)

HASH_CHUNK = 1024 * 1024
CACHE_FORMAT_VERSION = 1

_JUNK_NAMES = frozenset({"thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store"})
_TEMP_SUFFIXES = (".part", ".partial", ".crdownload", ".tmp")
_WIN_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0) | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0)
_REPARSE_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)


def is_ignored_name(name: str) -> bool:
    """Hidden, temporary and OS junk files are never shared."""
    lower = name.lower()
    return (
        name.startswith((".", "~$"))
        or lower in _JUNK_NAMES
        or lower.endswith(_TEMP_SUFFIXES)
    )


def sha256_file(path: Path, stop: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    buf = bytearray(HASH_CHUNK)
    view = memoryview(buf)
    with open(path, "rb") as f:
        while True:
            if stop is not None and stop.is_set():
                raise InterruptedError("hashing cancelled")
            n = f.readinto(buf)
            if not n:
                break
            digest.update(view[:n])
    return digest.hexdigest()


@dataclass(frozen=True)
class FileEntry:
    relpath: str  # always '/'-separated, relative to the shared folder
    size: int
    mtime_ns: int
    sha256: str

    def to_dict(self) -> dict:
        return {
            "id": self.sha256,
            "name": self.relpath,
            "size": self.size,
            "sha256": self.sha256,
            "mtime": self.mtime_ns / 1_000_000_000,
        }


@dataclass(frozen=True)
class IndexSnapshot:
    entries: tuple[FileEntry, ...]
    pending: int  # files found but not hashed yet
    version: int


class Indexer:
    def __init__(self, root: Path, cache_path: Path | None = None, interval: float = 5.0):
        self.root = Path(root).resolve()
        self.cache_path = cache_path
        self.interval = interval
        self.hash_count = 0  # files hashed since start (diagnostics and tests)
        self.on_change: Callable[[IndexSnapshot], None] | None = None
        self._cache: dict[str, tuple[int, int, str]] = self._load_cache()
        self._snapshot = IndexSnapshot((), 0, 0)
        self._by_hash: dict[str, FileEntry] = {}
        self._scan_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._generation = 0  # bumped by set_root(); results of older scans are dropped
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._abort = threading.Event()  # interrupts hashing (stop or set_root)
        self._thread: threading.Thread | None = None

    # ----- public API -------------------------------------------------------------------

    @property
    def snapshot(self) -> IndexSnapshot:
        return self._snapshot

    def lookup(self, sha256: str) -> FileEntry | None:
        return self._by_hash.get(sha256)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lanshare-indexer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._abort.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def request_rescan(self) -> None:
        self._wake.set()

    def set_root(self, root: Path) -> None:
        """Share a different folder: the index empties at once and the new folder is scanned."""
        with self._publish_lock:
            self._generation += 1
            self.root = Path(root).resolve()
            self._abort.set()
            self._publish_locked({}, pending=0)
        self._wake.set()

    def scan(self) -> bool:
        """Run one full pass over the folder. Returns True if the index changed."""
        with self._scan_lock:
            return self._scan()

    # ----- scanning ---------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:  # keep indexing alive no matter what one pass hits
                log.exception("index scan failed")
            self._wake.wait(self.interval)
            self._wake.clear()

    def _scan(self) -> bool:
        with self._publish_lock:
            generation, root = self._generation, self.root
            if not self._stop.is_set():
                self._abort.clear()
        entries: dict[str, FileEntry] = {}
        new_cache: dict[str, tuple[int, int, str]] = {}
        todo: list[tuple[str, Path, int, int]] = []
        for rel, path, size, mtime_ns in self._walk(root):
            key = str(path)
            cached = self._cache.get(key)
            if cached is not None and cached[0] == size and cached[1] == mtime_ns:
                entries[rel] = FileEntry(rel, size, mtime_ns, cached[2])
                new_cache[key] = cached
            else:
                todo.append((rel, path, size, mtime_ns))

        changed = self._publish(entries, len(todo), generation)
        last_publish = time.monotonic()
        for i, (rel, path, size, mtime_ns) in enumerate(todo):
            try:
                digest = sha256_file(path, self._abort)
                st = os.stat(path)
            except InterruptedError:
                return changed
            except OSError as exc:
                log.debug("skipping %s: %s", path, exc)
                continue
            if st.st_size != size or st.st_mtime_ns != mtime_ns:
                continue  # modified while hashing; the next pass picks it up
            self.hash_count += 1
            entries[rel] = FileEntry(rel, size, mtime_ns, digest)
            new_cache[str(path)] = (size, mtime_ns, digest)
            if time.monotonic() - last_publish > 0.5:
                changed |= self._publish(entries, len(todo) - i - 1, generation)
                last_publish = time.monotonic()
        changed |= self._publish(entries, 0, generation)

        if generation != self._generation:
            return changed  # the shared folder changed during this scan
        if new_cache != self._cache:
            self._cache = new_cache
            self._save_cache()
        return changed

    def _publish(self, entries: dict[str, FileEntry], pending: int, generation: int) -> bool:
        with self._publish_lock:
            if generation != self._generation:
                return False
            return self._publish_locked(entries, pending)

    def _publish_locked(self, entries: dict[str, FileEntry], pending: int) -> bool:
        ordered = tuple(sorted(entries.values(), key=lambda e: e.relpath.lower()))
        current = self._snapshot
        if ordered == current.entries and pending == current.pending:
            return False
        by_hash: dict[str, FileEntry] = {}
        for entry in ordered:
            by_hash.setdefault(entry.sha256, entry)
        self._by_hash = by_hash
        self._snapshot = IndexSnapshot(ordered, pending, current.version + 1)
        if self.on_change is not None:
            try:
                self.on_change(self._snapshot)
            except Exception:
                log.exception("index change callback failed")
        return True

    def _walk(self, root: Path) -> Iterator[tuple[str, Path, int, int]]:
        """Yield (relpath, path, size, mtime_ns) for every shareable file under the root.

        Symlinked and junctioned directories are not entered. Symlinked files are included
        only if they resolve to a file inside the root.
        """
        stack: list[tuple[str, str]] = [(str(root), "")]
        while stack:
            dirpath, prefix = stack.pop()
            try:
                scanner = os.scandir(dirpath)
            except OSError as exc:
                log.debug("cannot list %s: %s", dirpath, exc)
                continue
            with scanner:
                for entry in scanner:
                    if is_ignored_name(entry.name):
                        continue
                    rel = prefix + entry.name
                    try:
                        if _is_hidden_on_windows(entry):
                            continue
                        if entry.is_symlink():
                            target = Path(entry.path).resolve()
                            if not is_within(target, root) or not target.is_file():
                                continue
                            st = target.stat()
                        elif entry.is_dir(follow_symlinks=False):
                            if not _is_junction(entry):
                                stack.append((entry.path, rel + "/"))
                            continue
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                        else:
                            continue
                    except OSError:
                        continue
                    yield rel, Path(entry.path), st.st_size, st.st_mtime_ns

    # ----- hash cache persistence -------------------------------------------------------

    def _load_cache(self) -> dict[str, tuple[int, int, str]]:
        if self.cache_path is None or not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if data.get("version") != CACHE_FORMAT_VERSION:
                return {}
            return {
                k: (int(v[0]), int(v[1]), str(v[2]))
                for k, v in data["files"].items()
                if isinstance(v, list) and len(v) == 3
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("ignoring unreadable hash cache %s: %s", self.cache_path, exc)
            return {}

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        payload = {"version": CACHE_FORMAT_VERSION, "files": {k: list(v) for k, v in self._cache.items()}}
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            log.warning("could not save hash cache: %s", exc)


def _is_hidden_on_windows(entry: os.DirEntry) -> bool:
    if sys.platform != "win32":
        return False
    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attrs & _WIN_HIDDEN)


def _is_junction(entry: os.DirEntry) -> bool:
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    if is_junction is not None:
        return is_junction()
    if _REPARSE_MOUNT_POINT is None:
        return False
    return getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0) == _REPARSE_MOUNT_POINT
