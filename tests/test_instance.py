"""One instance per user: the lock, the hand-over to a running copy, and picking a free port."""

import json
import shutil
import socket
import subprocess
import sys

import httpx
import pytest

from lightninglan import __version__
from lightninglan.cli import bind_first_free
from lightninglan.instance import INSTANCE_FILE, InstanceLock, is_newer, pages_missing, running_instance
from lightninglan.resources import static_dir

from .harness import REPO_ROOT, PeerProcess, free_port, wait_for


def test_lock_is_exclusive_until_released(tmp_path):
    first, second = InstanceLock(tmp_path), InstanceLock(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_instance_file_is_written_and_removed(tmp_path):
    lock = InstanceLock(tmp_path)
    assert lock.acquire()
    lock.publish(8765, "a" * 32)
    info = json.loads((tmp_path / INSTANCE_FILE).read_text())
    assert info["port"] == 8765 and info["peer_id"] == "a" * 32
    lock.release()
    assert not (tmp_path / INSTANCE_FILE).exists()


def test_no_running_instance(tmp_path):
    assert running_instance(tmp_path, timeout=0.3) is None
    # A stale record pointing at a port nobody answers on is not a running instance either.
    (tmp_path / INSTANCE_FILE).write_text(json.dumps({"pid": 1, "port": free_port(), "peer_id": "b" * 32}))
    assert running_instance(tmp_path, timeout=0.3) is None


def test_which_version_is_newer():
    assert is_newer("1.7.1", "1.7.0")
    assert is_newer("1.10.0", "1.9.3")  # compared as numbers, not as text
    assert is_newer("2.0.0", "1.9.9")
    assert not is_newer("1.7.0", "1.7.0")
    assert not is_newer("1.6.1", "1.7.0")
    assert not is_newer("1.7.1", "")  # a copy that didn't say which it is
    assert not is_newer("1.7.1+dev", "1.7.0")  # nor one that can't be read


def test_a_copy_that_does_not_answer_is_not_missing_its_pages():
    assert not pages_missing(f"http://localhost:{free_port()}")


def test_port_fallback():
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen()
        busy = blocker.getsockname()[1]
        with bind_first_free(busy, explicit=False) as sock:
            assert sock.getsockname()[1] > busy
            # Already listening, so a copy starting at the same moment cannot bind it too.
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
        with pytest.raises(OSError):
            bind_first_free(busy, explicit=True)


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def _launch(*args, **kwargs):
    return subprocess.run([sys.executable, "-m", "lightninglan", *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, timeout=60, **kwargs)


@pytest.mark.integration
def test_second_launch_opens_the_running_copy(spawn):
    a = spawn("Alice PC")
    r = _launch("--config", str(a.config), "--no-browser", "--no-tray")
    assert r.returncode == 0, r.stderr
    assert f"LightningLAN is already running: http://localhost:{a.port}" in r.stdout
    assert a.running
    assert a.get("/api/status").status_code == 200


@pytest.mark.integration
def test_busy_port_moves_to_the_next_free_one(tmp_path):
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen()
        busy = blocker.getsockname()[1]
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"port": busy, "share_dir": str(tmp_path / "s"),
                                   "downloads_dir": str(tmp_path / "d")}))
        proc = subprocess.Popen([sys.executable, "-m", "lightninglan", "--config", str(cfg), "--no-browser",
                                 "--no-tray", "--interface", "127.0.0.1"], cwd=REPO_ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            info = wait_for(lambda: _read_json(tmp_path / INSTANCE_FILE), timeout=30, message="the instance file")
            assert info["port"] > busy
            found = running_instance(tmp_path, timeout=20)
            assert found.url == f"http://localhost:{info['port']}" and found.version == __version__
            # The remembered port stays the preferred one for next time.
            assert json.loads(cfg.read_text())["port"] == busy
            httpx.post(f"http://127.0.0.1:{info['port']}/api/shutdown", headers={"X-LightningLAN-Client": "test"},
                       trust_env=False)
            assert proc.wait(timeout=15) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
    log = (tmp_path / "logs" / "lightninglan.log").read_text(encoding="utf-8")
    assert "running as" in log and "LightningLAN stopped" in log


class _ShimCopy(PeerProcess):
    """A copy started through a few lines of Python that change it before it runs."""

    shim = ""

    def command(self) -> list[str]:
        code = f"import sys, lightninglan; {self.shim}; from lightninglan.cli import main; sys.exit(main(sys.argv[1:]))"
        return [sys.executable, "-c", code, *super().command()[3:]]


class _OlderCopy(_ShimCopy):
    shim = "lightninglan.__version__ = '1.0.0'"


class _UnpackedCopy(_ShimCopy):
    """Runs from page files unpacked into a folder of its own, as the packaged app does."""

    def __init__(self, base, name, **kwargs):
        super().__init__(base, name, **kwargs)
        self.bundle = self.dir / "unpacked"
        shutil.copytree(static_dir(), self.bundle / "lightninglan" / "static")
        self.shim = f"sys._MEIPASS = {str(self.bundle)!r}"


def _takes_its_place(running: PeerProcess, tmp_path) -> None:
    newer = PeerProcess(tmp_path, running.name)  # the same settings folder
    try:
        newer.start(wait=False)
        assert running.proc.wait(timeout=40) == 0, running.log_text()
        newer.wait_ready()
        found = running_instance(newer.dir, timeout=20)
        assert found.url == f"http://localhost:{newer.port}" and found.version == __version__
    finally:
        newer.stop()
    assert newer.proc.returncode == 0
    assert "asking it to quit" in newer.log_text()


@pytest.mark.integration
def test_a_newer_version_takes_the_place_of_the_running_copy(tmp_path):
    older = _OlderCopy(tmp_path, "Den PC").start()
    try:
        assert running_instance(older.dir, timeout=5).version == "1.0.0"
        _takes_its_place(older, tmp_path)
    finally:
        older.stop()
    log = (older.dir / "logs" / "lightninglan.log").read_text(encoding="utf-8")
    assert "took the place of LightningLAN 1.0.0" in log


@pytest.mark.integration
def test_a_copy_whose_pages_were_deleted_is_replaced(tmp_path):
    broken = _UnpackedCopy(tmp_path, "Den PC").start()
    try:
        assert not pages_missing(broken.url)
        shutil.rmtree(broken.bundle / "lightninglan")  # a temp-folder clean-up while it runs
        assert pages_missing(broken.url)
        _takes_its_place(broken, tmp_path)
    finally:
        broken.stop()
