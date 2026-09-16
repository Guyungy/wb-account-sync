#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台抽象层：客户端进程检测与数据目录探测。

被 ``wb_home_bridge.py`` 与 ``wb_ui.py`` 共用，只依赖标准库。

设计要点
--------

**进程检测为什么不能按进程名匹配。** 客户端主进程是 Electron，macOS 上
``argv[0]`` 是 ``Electron`` 而不是 ``WorkBuddy``，按进程名匹配会静默失效。
因此 macOS 走 ``ps`` 按 bundle 路径匹配，Windows 走 ``tasklist`` 按镜像名匹配，
Linux 走 ``/proc`` 按可执行文件名匹配。

**home 探测为什么要有候选列表。** macOS 侧已实测为 ``~/.workbuddy`` 与
``~/.workbuddy-ai``。Windows 版的数据目录布局**未经本机验证**（无 Windows 环境），
候选顺序是推断值；全部候选都找不到时会退回首选路径并标记未确认，由调用方
提示用户显式指定路径。
"""

from __future__ import annotations

import os
import platform as _platform
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

VERSION = "0.1.0"

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

DB_NAME = "workbuddy.db"
CREATE_NO_WINDOW = 0x08000000


class PlatformError(Exception):
    """进程探测或路径探测失败。"""


@dataclass(frozen=True)
class ClientSpec:
    key: str
    display: str
    mac_app: str
    win_image: str
    linux_image: str
    home_candidates: tuple[str, ...]
    hint: str


CLIENTS: tuple[ClientSpec, ...] = (
    ClientSpec(
        key="wb",
        display="WorkBuddy",
        mac_app="WorkBuddy.app",
        win_image="WorkBuddy.exe",
        linux_image="workbuddy",
        home_candidates=(
            "~/.workbuddy",
            "%APPDATA%/WorkBuddy",
            "%APPDATA%/workbuddy",
        ),
        hint="WorkBuddy 客户端的数据目录",
    ),
    ClientSpec(
        key="wb_ai",
        display="WorkBuddy AI",
        mac_app="WorkBuddy AI.app",
        win_image="WorkBuddy AI.exe",
        linux_image="workbuddy-ai",
        home_candidates=(
            "~/.workbuddy-ai",
            "%APPDATA%/WorkBuddy AI",
            "%APPDATA%/workbuddy-ai",
        ),
        hint="WorkBuddy AI 客户端的数据目录",
    ),
)

CLIENTS_BY_KEY = {spec.key: spec for spec in CLIENTS}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def expand_path(path: str) -> str:
    """展开 ``~`` 与 ``%VAR%``，返回绝对路径。"""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _decode(raw: bytes) -> str:
    """按常见编码依次尝试解码子进程输出。

    中文 Windows 的 ``tasklist`` 输出是 GBK，直接按 UTF-8 解码会抛异常。
    """
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _run(args: list[str]) -> str:
    kwargs: dict[str, Any] = {"capture_output": True, "check": False}
    if IS_WIN:
        # 避免在 GUI 程序里弹出控制台黑框。
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(args, **kwargs)
    except OSError as exc:
        raise PlatformError(f"无法执行 {args[0]}：{exc}") from exc
    return _decode(proc.stdout or b"")


# --------------------------------------------------------------------------
# 进程扫描
# --------------------------------------------------------------------------


def _is_self_noise(cmd: str) -> bool:
    """排除 helper 进程与本次探测自身的命令行。"""
    if "/Frameworks/" in cmd:
        return True
    lowered = cmd.lower()
    return any(
        token in lowered
        for token in ("wb_platform", "wb_home_bridge", "wb_ui", "grep workbuddy")
    )


def _scan_macos(found: dict[str, list[dict[str, Any]]]) -> None:
    out = _run(["ps", "-Ao", "pid=,command="])
    for line in out.splitlines():
        line = line.strip()
        if not line or _is_self_noise(line):
            continue
        pid_str, _, cmd = line.partition(" ")
        if not pid_str.isdigit():
            continue
        for spec in CLIENTS:
            # 带斜杠前缀保证 "WorkBuddy.app" 不会误命中 "WorkBuddy AI.app"。
            if f"/{spec.mac_app}/Contents/MacOS/" in cmd:
                found[spec.key].append({"pid": int(pid_str), "cmd": cmd[:200]})
                break


def _scan_windows(found: dict[str, list[dict[str, Any]]]) -> None:
    out = _run(["tasklist", "/FO", "CSV", "/NH"])
    by_image = {spec.win_image.lower(): spec.key for spec in CLIENTS}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        fields = [f.strip('"') for f in line.split('","')]
        if len(fields) < 2:
            continue
        image = fields[0].strip('"')
        key = by_image.get(image.lower())
        if key and fields[1].strip().isdigit():
            found[key].append({"pid": int(fields[1]), "cmd": image})


def _scan_linux(found: dict[str, list[dict[str, Any]]]) -> None:
    try:
        entries = os.listdir("/proc")
    except OSError as exc:
        raise PlatformError(f"无法读取 /proc：{exc}") from exc
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if not raw:
            continue
        argv0 = _decode(raw).split("\x00")[0]
        base = os.path.basename(argv0).lower()
        for spec in CLIENTS:
            # 用相等而非包含，避免 "workbuddy" 命中 "workbuddy-ai"。
            if spec.linux_image and base == spec.linux_image:
                found[spec.key].append({"pid": int(entry), "cmd": argv0[:200]})
                break


def _scan_processes() -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {spec.key: [] for spec in CLIENTS}
    if IS_MAC:
        _scan_macos(found)
    elif IS_WIN:
        _scan_windows(found)
    elif IS_LINUX:
        _scan_linux(found)
    return found


# --------------------------------------------------------------------------
# home 探测
# --------------------------------------------------------------------------


def client_home(spec: ClientSpec) -> tuple[str, bool]:
    """返回 ``(路径, 是否为已确认的数据目录)``。

    优先返回确实含有 ``workbuddy.db`` 的候选；否则退回第一个存在的目录；
    都不存在时返回首选候选并标记未确认。
    """
    fallback: str | None = None
    for cand in spec.home_candidates:
        path = expand_path(cand)
        if os.path.isfile(os.path.join(path, DB_NAME)):
            return path, True
        if fallback is None and os.path.isdir(path):
            fallback = path
    if fallback is not None:
        return fallback, False
    return expand_path(spec.home_candidates[0]), False


def client_statuses() -> list[dict[str, Any]]:
    """两个客户端的运行状态与数据目录，供 UI 与 CLI 共用。"""
    procs = _scan_processes()
    out: list[dict[str, Any]] = []
    for spec in CLIENTS:
        hits = procs.get(spec.key, [])
        home, confirmed = client_home(spec)
        exists = os.path.isdir(home)
        out.append(
            {
                "key": spec.key,
                "display": spec.display,
                "hint": spec.hint,
                "running": bool(hits),
                "processes": hits,
                "home": home,
                "home_confirmed": confirmed,
                "home_exists": exists,
                "home_note": (
                    "" if confirmed
                    else ("目录存在但未找到 workbuddy.db" if exists
                          else "未找到数据目录")
                ),
            }
        )
    return out


def running_clients() -> list[dict[str, Any]]:
    return [item for item in client_statuses() if item["running"]]


def clients_all_stopped() -> bool:
    try:
        return not running_clients()
    except PlatformError:
        # 探测本身失败时不假装安全。
        return False


def platform_label() -> str:
    if IS_MAC:
        return f"macOS {_platform.mac_ver()[0]} · {_platform.machine()}"
    if IS_WIN:
        return f"Windows {_platform.release()} · {_platform.machine()}"
    if IS_LINUX:
        return f"Linux {_platform.release()} · {_platform.machine()}"
    return f"{_platform.system()} · {_platform.machine()}"


def stop_instructions() -> str:
    """给用户的退出客户端指引，按平台措辞。"""
    if IS_WIN:
        quit_keys = "在托盘图标上右键退出"
        shell = "PowerShell 或命令提示符"
    elif IS_MAC:
        quit_keys = "按 Command-Q"
        shell = "Terminal.app"
    else:
        quit_keys = "正常退出"
        shell = "系统终端"
    names = " 与 ".join(spec.display for spec in CLIENTS)
    return (
        f"1) 在 {names} 中{quit_keys}，确保进程完全退出；\n"
        f"2) 打开{shell}（本工具不能在客户端内部会话中执行）；\n"
        "3) 重新运行本命令。"
    )


# --------------------------------------------------------------------------
# 自检入口
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import json

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" in argv:
        payload = {
            "platform": platform_label(),
            "clients": client_statuses(),
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"平台：{platform_label()}")
    for item in client_statuses():
        mark = "运行中" if item["running"] else "已退出"
        print(f"\n[{item['display']}] {mark}")
        print(f"  数据目录：{item['home']}")
        if item["home_note"]:
            print(f"  ! {item['home_note']}")
        for proc in item["processes"][:5]:
            print(f"  pid {proc['pid']}  {proc['cmd'][:100]}")
    print()
    print("全部客户端已退出。" if clients_all_stopped() else "仍有客户端在运行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
