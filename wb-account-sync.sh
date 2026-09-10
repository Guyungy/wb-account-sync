#!/bin/bash
# WorkBuddy 跨账号数据保留工具 —— 便捷包装
#
# 持续同步（推荐，装一次就不用管）
#   ./wb-account-sync.sh daemon-install        # 后台常驻，切账号自动同步
#   ./wb-account-sync.sh daemon-status         # 看状态
#   ./wb-account-sync.sh daemon-log -n 50      # 看日志
#   ./wb-account-sync.sh daemon-uninstall      # 关掉
#   ./wb-account-sync.sh sync [--dry-run]      # 手动跑一轮
#
# 一次性操作
#   ./wb-account-sync.sh status
#   ./wb-account-sync.sh backup --label before-switch
#   ./wb-account-sync.sh adopt --from a1b2c3d4 --to current --yes
#   ./wb-account-sync.sh revert --yes
#
# 注意：restore / adopt / revert 需要先完全退出 WorkBuddy（⌘Q），并在系统终端运行。
#       持续同步（sync / live / daemon-*）可以在客户端运行时使用。

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 解释器选择：优先用 WorkBuddy 自带的托管 Python，其次 PATH 里的 python3
PY=""
for cand in "$HOME"/.workbuddy/binaries/python/versions/*/bin/python3; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then PY="$(command -v python3)"; fi
fi
if [ -z "$PY" ]; then
  echo "找不到可用的 python3（需要 Python 3.9+）" >&2
  exit 1
fi

exec "$PY" "$DIR/wb-account-sync.py" "$@"
