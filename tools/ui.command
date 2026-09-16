#!/bin/bash
# ui.command — 双击启动「跨 App 打通」浏览器界面（macOS）
#
# 在 Finder 里双击本文件即可启动并自动打开浏览器；
# 也可以把本文件拖到 Dock 上，当作一个 App 用。
#
# 界面只监听 127.0.0.1，并且需要一个一次性 token 才能访问——
# 每次启动的完整链接会打印在下面，浏览器也会自动打开。
#
# 用法（命令行）：
#   ./tools/ui.command                  # 默认端口 8788，被占用时自动换
#   WB_UI_PORT=9123 ./tools/ui.command  # 指定端口
#   ./tools/ui.command --home-a /path --home-b /path   # 显式指定数据目录（参数原样转发）

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

find_python() {
  if [ -n "${WB_PYTHON:-}" ]; then
    printf '%s\n' "$WB_PYTHON"
    return
  fi
  if [ -x "$REPO/.venv/bin/python" ]; then
    printf '%s\n' "$REPO/.venv/bin/python"
    return
  fi
  # WorkBuddy 自带的托管 Python 优先——版本通常最新
  for root in "$HOME/.workbuddy" "$HOME/.workbuddy-ai"; do
    for cand in "$root"/binaries/python/versions/*/bin/python3; do
      if [ -x "$cand" ]; then
        printf '%s\n' "$cand"
        return
      fi
    done
  done
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$cand" ]; then
      printf '%s\n' "$cand"
      return
    fi
  done
  command -v python3 || true
}

PY="$(find_python)"
if [ -z "$PY" ]; then
  echo "找不到 Python 3.10+。可显式指定：WB_PYTHON=/绝对路径/python3 ./tools/ui.command" >&2
  echo "按回车关闭。" >&2
  read -r _
  exit 2
fi

if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "找到的 Python 版本过低（需要 3.10+）：$PY" >&2
  "$PY" --version >&2
  echo "可用 WB_PYTHON=/绝对路径/python3 显式指定。按回车关闭。" >&2
  read -r _
  exit 2
fi

echo "解释器：$PY"
echo "工作目录：$REPO"
echo

"$PY" "$REPO/tools/wb_ui.py" --port "${WB_UI_PORT:-8788}" "$@"
code=$?

echo
if [ "$code" -ne 0 ]; then
  echo "界面进程退出，退出码 $code。"
else
  echo "界面已停止。"
fi
echo "按回车关闭这个窗口。"
read -r _
