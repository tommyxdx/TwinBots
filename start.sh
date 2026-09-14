#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if [ ! -f .venv/installed-v1.txt ]; then
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -c "from pathlib import Path; Path('.venv/installed-v1.txt').touch()"
fi
.venv/bin/python -m twobots init
exec .venv/bin/python -m twobots run
