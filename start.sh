#!/bin/sh
# macOS / Linux: sets up on first run, then starts Jukebox. Extra args go to run.py (e.g. --demo).
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || exit 1
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt || exit 1
fi
exec .venv/bin/python run.py "$@"
