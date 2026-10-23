#!/usr/bin/env python3
"""Build the LightningLAN desktop app with PyInstaller, for the system this runs on.

    python build.py                 build into dist/
    python build.py --smoke-test    ...then start the result and check that it works

Results:
    Windows  dist/LightningLAN.exe
    macOS    dist/LightningLAN-macOS.zip  (LightningLAN.app, zipped for download)
    Linux    dist/LightningLAN-linux-<arch>

If the Python running this lacks the requirements, a .venv is created (or reused) next to this
file and the build runs from there. Building for another OS needs that OS; the GitHub Actions
workflow in .github/workflows/release.yml builds all three.
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
REQUIRED_MODULES = ("fastapi", "uvicorn", "zeroconf", "httpx", "pystray", "PIL", "segno", "PyInstaller") + (
    ("webview",) if sys.platform == "win32" else ()
)


# ----- environment -----------------------------------------------------------------------------

def has_requirements() -> bool:
    import importlib.util

    return all(importlib.util.find_spec(name) is not None for name in REQUIRED_MODULES)


def venv_python() -> Path:
    windows = ROOT / ".venv" / "Scripts" / "python.exe"
    return windows if windows.exists() else ROOT / ".venv" / "bin" / "python"


def run_in_venv(argv: list[str]) -> int:
    """Create or reuse .venv, install the requirements into it and run this script there."""
    py = venv_python()
    if not py.exists():
        print("Creating virtual environment in .venv ...", flush=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        py = venv_python()
    print("Installing requirements ...", flush=True)
    subprocess.check_call([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                           "-r", str(ROOT / "requirements.txt"), "-r", str(ROOT / "requirements-build.txt")])
    return subprocess.call([str(py), str(Path(__file__).resolve()), *argv])


# ----- build -------------------------------------------------------------------------------------

def version() -> str:
    sys.path.insert(0, str(ROOT))
    from lightninglan import __version__

    return __version__


def write_windows_version_file(path: Path, ver: str) -> None:
    parts = [int(p) for p in (ver.split(".") + ["0", "0", "0"])[:4] if p.isdigit()]
    numbers = tuple((parts + [0, 0, 0, 0])[:4])
    strings = {
        "CompanyName": "LightningLAN",
        "FileDescription": "LightningLAN",  # shown in Task Manager and the firewall prompt
        "FileVersion": ver,
        "InternalName": "LightningLAN",
        "OriginalFilename": "LightningLAN.exe",
        "ProductName": "LightningLAN",
        "ProductVersion": ver,
    }
    table = ",\n          ".join(f"StringStruct({k!r}, {v!r})" for k, v in strings.items())
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
          {table}])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


def linux_asset_name() -> str:
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower())
    return f"LightningLAN-linux-{arch}"


def build() -> tuple[Path, list[str]]:
    """Run PyInstaller. Returns (file to distribute, command that starts the app)."""
    from lightninglan.desktop import fix_tcl_tk_paths
    from lightninglan.icon import write_icon_files

    fix_tcl_tk_paths()  # so PyInstaller can bundle Tcl/Tk (the Windows folder chooser) from a venv
    BUILD.mkdir(exist_ok=True)
    write_icon_files(BUILD / "icons")
    write_windows_version_file(BUILD / "version_info.txt", version())
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "lightninglan.spec"),
        "--noconfirm", "--clean", "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller"),
    ])
    if sys.platform == "win32":
        exe = DIST / "LightningLAN.exe"
        return exe, [str(exe)]
    if sys.platform == "darwin":
        app = DIST / "LightningLAN.app"
        return app, [str(app / "Contents" / "MacOS" / "LightningLAN")]
    binary = DIST / linux_asset_name()
    if binary.exists():
        binary.unlink()
    (DIST / "LightningLAN").rename(binary)
    return binary, [str(binary)]


def zip_mac_app(app: Path) -> Path:
    """Zip LightningLAN.app the way Finder does (keeps symlinks and code signatures intact)."""
    archive = DIST / "LightningLAN-macOS.zip"
    if archive.exists():
        archive.unlink()
    subprocess.check_call(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)])
    return archive


# ----- smoke test --------------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, method=method, headers={"X-LightningLAN-Client": "build"})
    with opener.open(req, timeout=5) as resp:
        return resp.status, resp.read()


def check_window_support(command: list[str]) -> None:
    """The Windows app's own window: pywebview, pythonnet and WebView2 load from inside the bundle."""
    result = subprocess.run(command + ["--check-window"], capture_output=True, text=True, timeout=120)
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 3:
        print("  app window: WebView2 is not installed on this machine, so it couldn't be checked", flush=True)
        return
    if result.returncode != 0:
        raise RuntimeError(f"the app window doesn't work in the build (exit code {result.returncode}):\n{output}")
    print("  app window (WebView2) loads", flush=True)


def smoke_test(command: list[str]) -> None:
    """Start the built app in a scratch folder and check the web UI, indexing and shutdown."""
    with tempfile.TemporaryDirectory(prefix="lightninglan-smoke-") as tmp:
        tmp_path = Path(tmp)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        (tmp_path / "share").mkdir()
        (tmp_path / "share" / "hello.txt").write_text("hello from the smoke test\n")
        (tmp_path / "share" / "pieces.bin").write_bytes(bytes(range(256)) * (3 * 4096 + 1))  # 3 MB + 256 B
        cmd = command + [
            "--config", str(tmp_path / "config.json"), "--share", str(tmp_path / "share"),
            "--downloads", str(tmp_path / "downloads"), "--port", str(port), "--name", "Smoke Test",
            "--no-browser", "--no-tray",
        ]
        print("Smoke test:", " ".join(cmd), flush=True)
        log_path = tmp_path / "output.log"
        with open(log_path, "wb") as out:
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=tmp)
        try:
            deadline = time.monotonic() + 90  # a one-file build unpacks itself first
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f"the app exited with code {proc.returncode}")
                try:
                    status, body = http("GET", f"{base}/api/status")
                    if status == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("the app did not answer within 90 seconds")
                time.sleep(0.5)
            print("  status:", json.loads(body)["name"], flush=True)
            status, page = http("GET", f"{base}/")
            assert status == 200 and b'id="drop-zone"' in page, "the web UI page is missing"
            for asset in ("app.js", "style.css", "favicon.svg", "phone.js"):
                assert http("GET", f"{base}/static/{asset}")[0] == 200, f"{asset} is missing"
            status, page = http("GET", f"{base}/phone")
            assert status == 200 and b'id="file-list"' in page, "the phone page is missing"
            assert http("GET", f"{base}/api/phone")[0] == 200, "the phone QR code doesn't work (segno missing?)"
            status, body = http("POST", f"{base}/api/show-window")
            assert status == 200 and json.loads(body) == {"shown": False}, "unexpected app window answer"
            if sys.platform == "win32":
                check_window_support(command)
            deadline = time.monotonic() + 30
            while True:
                files = json.loads(http("GET", f"{base}/api/files")[1])["files"]
                if sorted(f["name"] for f in files) == ["hello.txt", "pieces.bin"]:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"the shared file was not indexed: {files}")
                time.sleep(0.5)
            big = next(f for f in files if f["name"] == "pieces.bin")
            pieces = json.loads(http("GET", f"{base}/api/pieces/{big['id']}")[1])
            assert pieces["size"] == big["size"] and pieces["hashes"], "no piece list"
            print("  web UI, static files, indexing and swarm piece lists work", flush=True)
            http("POST", f"{base}/api/shutdown")
            code = proc.wait(timeout=30)
            if code != 0:
                raise RuntimeError(f"the app exited with code {code} after shutdown")
            log_file = tmp_path / "logs" / "lightninglan.log"
            assert log_file.exists() and "LightningLAN stopped" in log_file.read_text(encoding="utf-8"), "no log file"
            print("  clean shutdown, log file written", flush=True)
        except BaseException:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
            print("---- app output ----")
            print(log_path.read_text(encoding="utf-8", errors="replace"))
            log_file = tmp_path / "logs" / "lightninglan.log"
            if log_file.exists():
                print("---- lightninglan.log ----")
                print(log_file.read_text(encoding="utf-8", errors="replace"))
            raise
    print("Smoke test passed.", flush=True)


# ----- main --------------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--smoke-test", action="store_true", help="start the built app and check it works")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        print("LightningLAN needs Python 3.11 or newer.", file=sys.stderr)
        return 1
    if not has_requirements():
        return run_in_venv(sys.argv[1:])
    sys.path.insert(0, str(ROOT))

    artifact, command = build()
    if args.smoke_test:
        smoke_test(command)
    if sys.platform == "darwin":
        artifact = zip_mac_app(artifact)
    size = artifact.stat().st_size if artifact.is_file() else 0
    print(f"\nBuilt {artifact.relative_to(ROOT)} ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
