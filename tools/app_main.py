#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面应用入口：给打包出来的 ``.app`` / ``.exe`` 用。

为什么不能直接把 ``wb_ui.py`` 当入口
------------------------------------

``wb_ui.py`` 是给终端写的：出错时往 stderr 打一行就够了，用户看得见。
打包成窗口模式（macOS 的 ``.app``、Windows 的 ``--noconsole``）之后没有终端，
异常会被静默吞掉，用户看到的现象只是"双击了没反应"。所以这里包一层：

1. 捕获全部异常与 ``SystemExit``；
2. 用**原生对话框**把消息摊开给用户（macOS 走 ``osascript``，
   Windows 走 ``MessageBoxW``，其他平台退回 stderr）；
3. 返回非零退出码，系统层面也能记录失败。

界面逻辑一律复用 ``wb_ui``，本文件不复制任何业务代码。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback

APP_TITLE = "wb-account-sync"


def _ensure_streams() -> None:
    """窗口模式下 ``stdout`` / ``stderr`` 可能是 ``None``。

    Windows 的 ``--noconsole`` 构建尤其如此：没有控制台就拿不到流对象，
    连 ``argparse`` 的 ``--version`` 都会抛 ``AttributeError``，
    现象是"双击没反应"，排查起来非常难受。这里补成 devnull。
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _selftest() -> int:
    """``--selftest``：不启动服务，只验证打包产物完整可用。

    打包最容易出问题的地方是模块没被收进去（``hiddenimports`` 漏项），
    而这类错误在窗口模式下**完全没有现象**。CI 用它做冒烟测试，
    用户遇到"双击没反应"时也可以拿它排障：

        wb-account-sync.app/Contents/MacOS/wb-account-sync --selftest
        wb-account-sync.exe --selftest

    返回值：0 表示全部就绪，1 表示有模块或探测失败。
    """
    import json as _json

    modules: dict[str, str] = {}
    report: dict[str, object] = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "modules": modules,
        "clients": [],
        "ok": True,
    }

    # wb_autosync 和 acct_probe 在界面里都是延迟 import，最容易被漏掉
    # （acct_probe 由 wb_ui 在请求 /api/accounts 时才导入，静态分析追不到），
    # 漏打的现象是"面板点开就报错"，窗口模式还看不到堆栈，所以这里一并验证。
    for name in ("wb_ui", "wb_home_bridge", "wb_platform", "wb_autosync", "acct_probe"):
        try:
            module = __import__(name)
            modules[name] = str(getattr(module, "VERSION", "ok"))
        except Exception as exc:
            modules[name] = f"FAILED: {exc.__class__.__name__}: {exc}"
            report["ok"] = False

    try:
        import wb_platform

        report["clients"] = [
            {
                "key": item["key"],
                "running": item["running"],
                "home": item["home"],
                "home_confirmed": item["home_confirmed"],
            }
            for item in wb_platform.client_statuses()
        ]
    except Exception as exc:
        report["clients_error"] = f"{exc.__class__.__name__}: {exc}"
        report["ok"] = False

    # pywebview 决定能不能开自带窗口。缺了它不算致命（会退回浏览器），
    # 但界面上"跳浏览器"还是"自带窗口"完全取决于这一项，必须能看见。
    try:
        import webview  # noqa: F401

        report["webview"] = "ok"
    except Exception as exc:
        report["webview"] = f"MISSING: {exc.__class__.__name__}: {exc}"

    sys.stdout.write(_json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()
    return 0 if report["ok"] else 1


def _tools_dir() -> str:
    """返回同级模块所在目录。

    PyInstaller 运行时入口被解包到 ``sys._MEIPASS``，``__file__`` 指向那里；
    源码方式运行时就是本文件所在目录。两种情况同级模块都在一起。
    """
    return os.path.dirname(os.path.abspath(__file__))


def _notify_macos(title: str, message: str) -> bool:
    """用 AppleScript 弹窗。

    正文走临时文件而不是内联进脚本：AppleScript 的字符串转义很容易出错，
    而错误信息里恰恰全是引号、反斜杠和路径。
    """
    import subprocess

    fd, path = tempfile.mkstemp(prefix="wb-app-err-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(message)
    try:
        script = (
            f'set msgText to (read POSIX file "{path}" as «class utf8»)\n'
            f'display dialog msgText with title {json.dumps(title)} '
            'buttons {"OK"} default button 1 with icon stop\n'
        )
        proc = subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=300, check=False
        )
        return proc.returncode == 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _notify_windows(title: str, message: str) -> bool:
    """用 Win32 ``MessageBoxW``。``0x10`` = ``MB_ICONERROR``。"""
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, message, title, 0x00000010)
    return True


def _notify_native(title: str, message: str) -> bool:
    """尽力弹原生对话框，成功返回 True；任何失败都只是静默降级。"""
    try:
        if sys.platform == "darwin":
            return _notify_macos(title, message)
        if sys.platform.startswith("win"):
            return _notify_windows(title, message)
    except Exception:
        return False
    return False


def _report(prefix: str, detail: str) -> None:
    """stderr + 原生弹窗双通道，缺一个都能看到。"""
    text = f"{prefix}\n\n{detail}".strip()
    sys.stderr.write(text + "\n")
    sys.stderr.flush()
    _notify_native(APP_TITLE, text)


def main(argv: list[str] | None = None) -> int:
    _ensure_streams()

    tools = _tools_dir()
    if tools not in sys.path:
        sys.path.insert(0, tools)

    args = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in args:
        try:
            return _selftest()
        except BrokenPipeError:
            # 下游把管道关了，例如 `--selftest | grep '"ok"'`（grep 找到就退）。
            # 这不是自检失败：弹错误框会吓人，退出码变 1 更会让 CI 误判。
            return 0
        except Exception:
            _report("自检失败。", traceback.format_exc())
            return 1

    # 打包版默认用**自己的窗口**显示界面，不再往外跳浏览器——用户双击图标
    # 就该看到一个应用窗口，而不是"浏览器被打开了一个标签页"。想回到旧行为
    # 加 --no-window（wb_ui 不认识这个参数，所以在这里消化掉）。
    if "--no-window" in args:
        args.remove("--no-window")
    elif "--window" not in args:
        args.append("--window")

    try:
        import wb_ui
    except Exception:
        _report("界面模块导入失败。", traceback.format_exc())
        return 1

    # 把原生弹窗交给界面层。自动打开浏览器失败时，这是唯一还能告诉用户
    # "服务在跑、地址是什么"的通道——窗口模式下看不到 stdout。
    wb_ui.NOTIFY_HOOK = _notify_native

    try:
        code = wb_ui.main(args)
    except SystemExit as exc:
        value = exc.code
        if isinstance(value, str):
            _report("启动参数有误。", value)
            return 2
        code = int(value or 0)
        if code:
            _report("界面退出。", f"退出码 {code}")
        return code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # 同 selftest：下游关管道是正常用法（`... --handshake | head -1`），
        # 不该被弹成"界面异常退出"。
        return 0
    except Exception:
        _report("界面异常退出。", traceback.format_exc())
        return 1

    code = int(code or 0)
    if code:
        _report("界面退出。", f"退出码 {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
