#!/usr/bin/env bash
# The repository gate: no em dashes in tracked text files, CLAUDE.md equal to
# AGENTS.md, then the unit tests. CI runs this on Ubuntu. Set PYTHON to choose
# the interpreter; it needs requirements.txt installed.
set -euo pipefail
cd "$(dirname "$0")/.."

python="${PYTHON:-}"
if [[ -z "$python" ]]; then
  for candidate in .venv/bin/python .venv/Scripts/python.exe python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then python="$candidate"; break; fi
  done
fi
[[ -n "$python" ]] || { echo "check: no Python found" >&2; exit 1; }

echo "==> no em dashes in tracked files"
# Built from its UTF-8 bytes so this script never contains the character.
# git grep exits 0 on a match, 1 on none, anything else on an error.
# docs/MY-SETUP.md is excluded until its next edit.
em_dash=$'\xe2\x80\x94'
status=0
git grep -nIF -e "$em_dash" -- . ':(exclude)docs/MY-SETUP.md' || status=$?
if [[ $status -eq 0 ]]; then
  echo "check: em dash (U+2014) found in the lines above" >&2
  exit 1
elif [[ $status -ne 1 ]]; then
  echo "check: git grep failed (exit $status)" >&2
  exit 1
fi

echo "==> CLAUDE.md and AGENTS.md are identical"
cmp CLAUDE.md AGENTS.md

echo "==> unit tests"
"$python" -m unittest discover -s tests

echo "check: all gates passed"
