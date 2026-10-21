"""Desktop integration: file manager, browser, message boxes and the folder chooser."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from .resources import is_frozen

log = logging.getLogger(__name__)


def open_in_file_manager(path: Path, reveal: bool = False) -> None:
    """Open a folder, or with reveal=True show a file selected in its folder. Never runs the file."""
    if sys.platform == "win32":
        if reveal:
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]  # a directory: opens Explorer
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if reveal else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if reveal else path)])


def open_url(url: str) -> bool:
    """Open a page in the default browser. Never raises."""
    try:
        return webbrowser.open(url)
    except Exception:
        log.warning("could not open %s in a browser", url, exc_info=True)
        return False


def restore_library_path() -> None:
    """Keep a Linux build's bundled libraries out of the programs it starts (browser, file manager).

    PyInstaller's bootloader points LD_LIBRARY_PATH at the bundle and saves the original value.
    This process has already loaded its libraries, so changing it only affects child processes.
    """
    if not (is_frozen() and sys.platform.startswith("linux")):
        return
    original = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if original is not None:
        os.environ["LD_LIBRARY_PATH"] = original
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


# ----- message boxes ---------------------------------------------------------------------------

def show_message(title: str, message: str, error: bool = True) -> None:
    """A native pop-up message, for a packaged app that has no console to print to. Never raises."""
    try:
        if sys.platform == "win32":
            import ctypes

            flags = (0x10 if error else 0x40) | 0x10000 | 0x40000  # icon | SETFOREGROUND | TOPMOST
            ctypes.windll.user32.MessageBoxW(None, message, title, flags)
        elif sys.platform == "darwin":
            script = (
                f"display dialog {_applescript_text(message)} with title {_applescript_text(title)} "
                f'buttons {{"OK"}} default button "OK" with icon {"stop" if error else "note"} giving up after 600'
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=610)
        else:
            commands = [
                ["zenity", "--error" if error else "--info", "--no-markup", "--title", title, "--text", message],
                ["kdialog", "--title", title, "--error" if error else "--msgbox", message],
            ]
            for cmd in commands:
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, capture_output=True, timeout=3600)
                    return
    except Exception:
        log.debug("could not show a message box", exc_info=True)


def _applescript_text(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ----- folder chooser ----------------------------------------------------------------------------

class FolderPickerError(Exception):
    """The folder chooser could not be shown (message is user-facing)."""


_picker_lock = threading.Lock()


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def fix_tcl_tk_paths() -> None:
    """Help tkinter find Tcl/Tk's script folders in a Windows virtualenv.

    Python 3.13.0's venvs look for them next to the venv instead of the base install (fixed in
    3.13.1). Packaged builds set these variables themselves.
    """
    if sys.platform != "win32" or is_frozen() or "TCL_LIBRARY" in os.environ:
        return
    base = Path(sys.base_prefix) / "tcl"
    tcl, tk = sorted(base.glob("tcl8.*")), sorted(base.glob("tk8.*"))
    if tcl and tk:
        os.environ["TCL_LIBRARY"] = str(tcl[-1])
        os.environ.setdefault("TK_LIBRARY", str(tk[-1]))


def _has_tkinter() -> bool:
    try:
        import tkinter
    except ImportError:
        return False
    return hasattr(tkinter, "Tk")


def folder_picker_available() -> bool:
    if sys.platform == "darwin":
        return shutil.which("osascript") is not None
    if sys.platform.startswith("linux"):
        if not _has_display():
            return False
        if shutil.which("zenity") or shutil.which("kdialog"):
            return True
    return _has_tkinter()


def pick_folder(title: str, initial: Path | None = None) -> Path | None:
    """Show the system's folder chooser and wait. Returns the chosen folder, or None if cancelled."""
    if not _picker_lock.acquire(blocking=False):
        raise FolderPickerError("A folder chooser is already open. Look for it behind the browser window.")
    try:
        start = initial if initial is not None and initial.is_dir() else None
        if sys.platform == "darwin":
            return _pick_with_osascript(title, start)
        if sys.platform.startswith("linux") and _has_display():
            if shutil.which("zenity"):
                return _pick_with_command(
                    ["zenity", "--file-selection", "--directory", "--title", title]
                    + (["--filename", str(start) + os.sep] if start else [])
                )
            if shutil.which("kdialog"):
                return _pick_with_command(
                    ["kdialog", "--title", title, "--getexistingdirectory", str(start or Path.home())]
                )
        return _pick_with_tkinter(title, start)
    finally:
        _picker_lock.release()


def _pick_with_command(cmd: list[str]) -> Path | None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    chosen = result.stdout.strip()
    return Path(chosen) if result.returncode == 0 and chosen else None


def _pick_with_osascript(title: str, start: Path | None) -> Path | None:
    choose = f"choose folder with prompt {_applescript_text(title)}"
    if start is not None:
        choose += f" default location (POSIX file {_applescript_text(str(start))})"
    return _pick_with_command(["osascript", "-e", "activate", "-e", f"POSIX path of ({choose})"])


def _pick_with_tkinter(title: str, start: Path | None) -> Path | None:
    fix_tcl_tk_paths()
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError as exc:
        raise FolderPickerError("No folder chooser is available; type the folder's full path instead.") from exc
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        raise FolderPickerError("No folder chooser is available; type the folder's full path instead.") from exc
    try:
        root.withdraw()
        root.attributes("-topmost", True)  # otherwise it can open hidden behind the browser
        root.update()
        chosen = filedialog.askdirectory(
            parent=root, title=title, initialdir=str(start) if start else None, mustexist=False
        )
    finally:
        root.destroy()
    return Path(chosen) if chosen else None
