"""PyInstaller entry point for the LanShare desktop app (same as `python -m lanshare`)."""

import sys

from lanshare.cli import main

if __name__ == "__main__":
    sys.exit(main())
