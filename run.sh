#!/usr/bin/env bash
# LanShare launcher for macOS and Linux: creates .venv, installs requirements, starts LanShare.
# Any arguments are passed on, e.g.  ./run.sh --share ~/Shared --name "Den PC"
set -euo pipefail
cd "$(dirname "$0")"

find_python() {
  for cmd in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 &&
       "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      echo "$cmd"
      return 0
    fi
  done
  return 1
}

venv_python() {
  if [ -x .venv/bin/python ]; then echo .venv/bin/python;
  elif [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe;  # Git Bash on Windows
  else return 1; fi
}

if ! VENV_PY=$(venv_python); then
  if ! PY=$(find_python); then
    echo "LanShare needs Python 3.11 or newer: https://www.python.org/downloads/" >&2
    exit 1
  fi
  echo "Creating virtual environment in .venv ..."
  if ! "$PY" -m venv .venv; then
    rm -rf .venv
    echo "Could not create the virtual environment." >&2
    echo "On Debian/Ubuntu install the venv module first: sudo apt install python3-venv" >&2
    exit 1
  fi
  VENV_PY=$(venv_python)
fi

# Install requirements on first run and whenever requirements.txt changes.
if ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "Installing requirements ..."
  "$VENV_PY" -m pip install --disable-pip-version-check -q -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi

exec "$VENV_PY" -m lanshare "$@"
