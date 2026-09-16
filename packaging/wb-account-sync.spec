# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格文件：把跨 App 打通工具打成桌面应用。

产物
----

- macOS  → ``dist/wb-account-sync.app``（onedir + BUNDLE）
- Windows→ ``dist/wb-account-sync.exe``（onefile，无控制台窗口）

用法（**在仓库根目录**执行，spec 自己会定位仓库）：

    pyinstaller packaging/wb-account-sync.spec --noconfirm

注意：PyInstaller 不支持交叉编译。macOS 的 ``.app`` 只能在 macOS 上构建，
Windows 的 ``.exe`` 只能在 Windows 上构建 —— 两个平台各自跑一遍，或交给
``.github/workflows/app-build.yml`` 的矩阵。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821  SPECPATH 由 PyInstaller 注入
TOOLS = ROOT / "tools"

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")


def _version() -> str:
    """从包里读版本号，避免两处版本号打架。"""
    source = (ROOT / "wb_account_sync" / "__init__.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "0.0.0"


VERSION = _version()

# tools/ 里的同目录模块靠运行时 sys.path 注入加载，PyInstaller 的静态分析
# 追不到这条路径，必须显式列出来，否则打出来的包会在 import 时炸。
HIDDEN_IMPORTS = [
    "app_main",
    "wb_ui",
    "wb_home_bridge",
    "wb_platform",
    "wb_autosync",
    "webview",
]

# pywebview 的后端是**按平台动态挑**的，静态分析同样追不到。
# 少了它的后果和漏打 wb_ui 一样：窗口开不出来，只能退回浏览器。
if IS_MAC:
    HIDDEN_IMPORTS.append("webview.platforms.cocoa")
elif IS_WIN:
    HIDDEN_IMPORTS.append("webview.platforms.edgechromium")

# pywebview 还带 js 注入脚本等数据文件，交给 collect_all 一网打尽。
WEBVIEW_DATAS, WEBVIEW_BINARIES, WEBVIEW_HIDDEN = collect_all("webview")

# tkinter 是最大的一块无用体积（约 15 MB），本项目界面走 WebView（macOS
# WKWebView / Windows WebView2），用不到它。unittest 只服务于源码仓库里的测试。
EXCLUDES = ["tkinter", "unittest", "pydoc", "doctest"]

a = Analysis(  # noqa: F821
    [str(TOOLS / "app_main.py")],
    pathex=[str(TOOLS)],
    binaries=WEBVIEW_BINARIES,
    datas=WEBVIEW_DATAS,
    hiddenimports=HIDDEN_IMPORTS + WEBVIEW_HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

if IS_WIN:
    # Windows：单文件，双击即用，不带控制台黑框。
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="wb-account-sync",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,          # UPX 压缩会被杀软误报，也会拖慢启动
        console=False,
        disable_windowed_traceback=False,
    )
else:
    # macOS（以及 Linux 回退）：onedir，再由 BUNDLE 包成 .app。
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="wb-account-sync",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
    )
    coll = COLLECT(  # noqa: F821
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="wb-account-sync",
    )
    if IS_MAC:
        app = BUNDLE(  # noqa: F821
            coll,
            name="wb-account-sync.app",
            icon=None,
            bundle_identifier="io.github.guyungy.wb-account-sync",
            info_plist={
                "CFBundleName": "wb-account-sync",
                "CFBundleDisplayName": "WorkBuddy 跨 App 数据打通",
                "CFBundleShortVersionString": VERSION,
                "CFBundleVersion": VERSION,
                "LSMinimumSystemVersion": "11.0",
                "NSHighResolutionCapable": True,
                # 不隐藏 Dock 图标：用户需要有个地方能退出这个常驻进程。
                "LSUIElement": False,
            },
        )
