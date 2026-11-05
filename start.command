#!/bin/bash
# Double-click this file on the tabulation Mac to start the scoring app.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: installing (needs internet, ~2 minutes)..."
  python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt
fi
.venv/bin/python run.py serve
