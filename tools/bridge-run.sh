#!/bin/bash
# bridge-run.sh — macOS / Linux 便捷入口。
#
# 真正的流程实现在 tools/bridge_run.py（跨平台）。Windows 用户请直接运行：
#   python tools\bridge_run.py
#
# ⚠️ 必须在客户端外部运行（macOS 用 Terminal.app，Windows 用 PowerShell），
#    且 WorkBuddy 与 WorkBuddy AI 都已完全退出。不要在 WorkBuddy 内部的会话里跑：
#    退出客户端会连带杀掉正在执行的进程，而且客户端运行时写入的数据不会被它的
#    内存缓存看到。
#
# 用法：
#   ./tools/bridge-run.sh                 # 完整流程（含确认门）
#   ./tools/bridge-run.sh --no-backup     # 跳过备份（不推荐）
#   ./tools/bridge-run.sh --no-changes    # 不搬 changes-detail / file-history

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

find_python() {
  if [ -n "${WB_PYTHON:-}" ]; then
    printf '%s\n' "$WB_PYTHON"
    return
  fi
  if [ -x "$REPO/.venv/bin/python" ]; then
    printf '%s\n' "$REPO/.venv/bin/python"
    return
  fi
  for root in "$HOME/.workbuddy-ai" "$HOME/.workbuddy"; do
    for cand in "$root"/binaries/python/versions/*/bin/python3; do
      if [ -x "$cand" ]; then
        printf '%s\n' "$cand"
        return
      fi
    done
  done
  command -v python3 || true
}

PY="$(find_python)"
if [ -z "$PY" ]; then
  echo "需要 Python 3.10+。可用 WB_PYTHON=/绝对路径/python3 显式指定。" >&2
  exit 2
fi

exec "$PY" "$REPO/tools/bridge_run.py" "$@"
