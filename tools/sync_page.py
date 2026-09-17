#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ``wb_ui.PAGE`` 原样导出到 Go 侧内嵌的 ``index.html``。

前端只有一份：Python 版直接把 ``PAGE`` 端出去，Go 版用 ``go:embed`` 端**同一个
文件**，``tests/test_go_ui_parity.py`` 还会逐字节比对两者。所以改完前端必须跑
一次这个脚本，否则 Go 侧那份就是旧的（测试会红）。

用法（仓库根目录）：``python3 tools/sync_page.py``
"""

from __future__ import annotations

import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_TOOLS_DIR)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import wb_ui  # noqa: E402  同目录模块

DEST = os.path.join(_REPO, "gobridge", "internal", "webui", "static", "index.html")


def main() -> int:
    # 直接从模块取 PAGE，不用正则去抠源码：上一次手工抠的时候漏掉了结尾换行，
    # 结果两边差一个字节，测试报"页面不同"却看不出差在哪。
    page = wb_ui.PAGE
    old = None
    if os.path.isfile(DEST):
        with open(DEST, "r", encoding="utf-8") as fh:
            old = fh.read()
    if old == page:
        print(f"已是最新，未改动：{os.path.relpath(DEST, _REPO)}")
        return 0
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    with open(DEST, "w", encoding="utf-8", newline="") as fh:
        fh.write(page)
    before = "（新建）" if old is None else f"{len(old)} 字节"
    print(f"已导出 {os.path.relpath(DEST, _REPO)}：{before} → {len(page)} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
