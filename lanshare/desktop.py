"""Opening folders in the operating system's file manager (for the local UI)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


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
