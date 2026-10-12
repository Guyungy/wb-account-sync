#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.cargo/bin:$PATH"
export CARGO_TARGET_DIR="$ROOT/target"
cd "$ROOT"
if [ ! -d web/node_modules ]; then npm --prefix web ci --no-audit --no-fund; fi
npm --prefix web run check
cd desktop
npm run dev
