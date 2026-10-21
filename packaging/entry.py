"""PyInstaller entry point for the LightningLAN desktop app (same as `python -m lightninglan`)."""

import sys

from lightninglan.cli import main

if __name__ == "__main__":
    sys.exit(main())
