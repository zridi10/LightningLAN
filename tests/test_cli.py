"""The command-line entry point."""

import socket
import subprocess
import sys

import pytest

from .harness import REPO_ROOT


def run_cli(*args, timeout=60):
    return subprocess.run([sys.executable, "-m", "lightninglan", *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, timeout=timeout)


def test_help_lists_the_documented_options():
    out = run_cli("--help").stdout
    for option in ["--share", "--downloads", "--port", "--name", "--passphrase", "--no-browser"]:
        assert option in out


def test_port_in_use_is_a_friendly_error(tmp_path):
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        r = run_cli("--share", str(tmp_path / "s"), "--downloads", str(tmp_path / "d"), "--port", str(port),
                    "--config", str(tmp_path / "c.json"), "--no-browser")
    assert r.returncode == 1
    assert f"cannot listen on port {port}" in r.stderr
    assert "Traceback" not in r.stderr


@pytest.mark.parametrize("args", [["--refresh-interval", "0"], ["--max-upload-rate", "-1"], ["--port", "70000"]])
def test_invalid_options_are_rejected(tmp_path, args):
    r = run_cli("--share", str(tmp_path / "s"), "--downloads", str(tmp_path / "d"), "--config",
                str(tmp_path / "c.json"), "--no-browser", *args)
    assert r.returncode == 2
    assert r.stderr.startswith("error:")


@pytest.mark.integration
def test_non_ascii_name_with_redirected_output(spawn):
    # Redirected stdout on Windows uses the ANSI code page; the banner must not crash.
    p = spawn("Café ☕ 日本")
    assert p.get("/api/status").json()["name"] == "Café ☕ 日本"
    assert "is running as" in p.log_text()
    assert "Traceback" not in p.log_text()
