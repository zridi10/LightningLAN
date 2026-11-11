"""One running LightningLAN per user (more precisely, per config folder).

The running instance holds a lock file in the app-data folder and records the port it listens
on. A second launch finds that record, checks that the instance answers, and opens its web UI
instead of starting another copy, unless the running copy is an older version or can no longer
load its own pages: then the new launch asks it to quit and takes its place.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import httpx

from .netutil import tls_context

LOCK_FILE = "lightninglan.lock"
INSTANCE_FILE = "instance.json"


class InstanceLock:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self._file: BinaryIO | None = None

    @property
    def info_path(self) -> Path:
        return self.state_dir / INSTANCE_FILE

    def acquire(self) -> bool:
        """Take the lock without waiting. False if another instance holds it."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        f = open(self.state_dir / LOCK_FILE, "a+b")
        try:
            _lock(f)
        except OSError:
            f.close()
            return False
        self._file = f
        return True

    def publish(self, port: int, peer_id: str) -> None:
        """Record where this instance listens, for later launches to find."""
        data = {"pid": os.getpid(), "port": port, "peer_id": peer_id}
        tmp = self.info_path.with_name(INSTANCE_FILE + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.info_path)

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self.info_path.unlink()
        except OSError:
            pass
        try:
            _unlock(self._file)
        except OSError:
            pass
        self._file.close()
        self._file = None


if sys.platform == "win32":
    import msvcrt

    def _lock(f: BinaryIO) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f: BinaryIO) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(f: BinaryIO) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f: BinaryIO) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class RunningInstance:
    url: str  # its web UI
    version: str  # "" if it didn't say


def running_instance(state_dir: Path, timeout: float = 15.0) -> RunningInstance | None:
    """The instance holding the lock, once it answers; None if it never does.

    Waits up to `timeout` seconds, since the other instance may still be starting.
    """
    info_path = Path(state_dir) / INSTANCE_FILE
    deadline = time.monotonic() + timeout
    while True:
        found = _probe(info_path)
        if found is not None or time.monotonic() >= deadline:
            return found
        time.sleep(0.25)


def _probe(info_path: Path) -> RunningInstance | None:
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        port, peer_id = int(info["port"]), str(info["peer_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return None  # not written yet
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=2.0, trust_env=False, verify=tls_context())
        status = r.json() if r.status_code == 200 else {}
        if status.get("peer_id") == peer_id:
            return RunningInstance(f"http://localhost:{port}", str(status.get("version") or ""))
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return None


def is_newer(version: str, than: str) -> bool:
    """Whether `version` is a later release than `than` ("1.7.1" is later than "1.7.0").

    A version that can't be read is never later, nor earlier, than another.
    """
    a, b = _release(version), _release(than)
    return a is not None and b is not None and a > b


def _release(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def pages_missing(url: str) -> bool:
    """Whether the running copy answers that its pages are gone.

    The packaged app runs from files unpacked into the temp folder, and a temp-folder clean-up can
    delete them while it runs: its window then shows only "Internal Server Error". A copy that
    doesn't answer at all is not counted as missing them, since it may just be busy.
    """
    try:
        r = httpx.head(f"{url}/static/index.html", timeout=5.0, trust_env=False)
    except httpx.HTTPError:
        return False
    return r.status_code != 200
