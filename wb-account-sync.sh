#!/bin/bash
# Repository launcher; prefer an explicitly selected or local virtual environment.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${WB_PYTHON:-}"
if [ -z "$PY" ] && [ -x "$DIR/.venv/bin/python" ]; then
  PY="$DIR/.venv/bin/python"
fi
if [ -z "$PY" ]; then
  for root in "$HOME/.workbuddy-ai" "$HOME/.workbuddy"; do
    for cand in "$root"/binaries/python/versions/*/bin/python3; do
      if [ -x "$cand" ]; then PY="$cand"; break 2; fi
    done
  done
fi
if [ -z "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  printf '%s\n' 'Python 3.10+ is required. See README.md for installation.' >&2
  exit 2
fi
export PYTHONDONTWRITEBYTECODE=1
exec "$PY" "$DIR/wb-account-sync.py" "$@"
