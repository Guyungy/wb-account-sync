#!/bin/bash
# 双击这个文件，完成一次两个数据目录之间的同步。
#
# 等价于在终端里执行 `wb-bridge sync`，做的事情一模一样：
#   1. 请求退出 WorkBuddy 与 WorkBuddy AI，等进程真的消失
#   2. 生成计划并打印摘要（待复制多少条、约多少体积）
#   3. 问一次确认（y 回车继续，其它一律视为放弃）
#   4. 执行 → 核验
#
# 顺序不能反：先建计划再退客户端，会让源侧指纹漂移，执行阶段会被引擎拒掉。
# 也别在 WorkBuddy 正在跑的时候硬来——引擎自己会拦，退不干净就中止。
#
# 只想看看会发生什么、不实际写入：在终端里加 --dry-run。
# 想跳过确认（脚本化场景）：加 --yes。

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

BIN="legacy-go/wb-bridge"

if [ ! -x "$BIN" ]; then
  echo "首次使用，正在构建一次可执行文件…"
  GO_BIN="$(command -v go 2>/dev/null || echo /usr/local/go/bin/go)"
  if [ ! -x "$GO_BIN" ]; then
    echo "找不到 go，无法构建。请先安装 Go，或用已有的打包版应用。"
    echo
    read -r -p "按回车关闭…" _
    exit 1
  fi
  ( cd legacy-go && "$GO_BIN" build -o wb-bridge ./cmd/wb-bridge ) || {
    echo "构建失败。"
    read -r -p "按回车关闭…" _
    exit 1
  }
fi

"$BIN" sync "$@"
code=$?

echo
if [ "$code" = "0" ]; then
  echo "同步完成。请重启两个 App 后再查看——客户端有内存缓存，不重启看不到新会话。"
else
  echo "未完成（退出码 $code）。上面的日志里有原因。"
fi
echo
read -r -p "按回车关闭这个窗口…" _
exit "$code"
