#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb-home-bridge 的本地 Web UI。

把跨 App 数据目录打通工具包装成浏览器界面，解决命令行做不到的两件事：

* **客户端退出状态实时可见**：两个 App 都完全退出前，执行按钮保持锁定。
  这是命令行版最容易出事的地方——用户以为退出了，实际没有。
* **迁移范围可视化**：每个选项标注后果，默认关闭会引发重复投递的项。

安全边界
--------

* 只绑 ``127.0.0.1``，不监听外部网卡。
* 每次启动生成一次性 token，所有 ``/api/*`` 请求必须携带，不匹配返回 403。
* 执行仍要求粘贴完整 ``plan_id``，与 CLI 的确认强度一致，不提供 ``--yes`` 等价物。
* 引擎逻辑全部复用 ``wb_home_bridge``，本文件只做参数装配与输出转发。
* 前端断开连接只会停止推送，不会中断正在执行的迁移。

仅标准库。用法：``python3 tools/wb_ui.py``
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import wb_home_bridge as bridge  # noqa: E402  同目录模块
import wb_platform  # noqa: E402

VERSION = "0.1.0"
# 是否运行在 PyInstaller 冻结出来的可执行文件里。打包版没有 tools/ 目录，
# 凡是依赖脚本自身路径的功能（例如 launchd 代理注册）都不能照常提供，
# 界面必须把这类入口改成说明而不是给出跑不通的命令。
IS_FROZEN = bool(getattr(sys, "frozen", False))
DEFAULT_STATE_DIR = "~/.wb-home-bridge"
# 自动同步代理的状态根默认与界面状态目录一致，但可用 WB_AUTOSYNC_ROOT 单独指向
DEFAULT_AUTOSYNC_ROOT = "~/.wb-home-bridge"


# --------------------------------------------------------------------------
# 运行时状态
# --------------------------------------------------------------------------


class UiState:
    def __init__(self, home_a: bridge.Home, home_b: bridge.Home, state_dir: str,
                 token: str | None = None) -> None:
        if home_a.path == home_b.path:
            raise bridge.BridgeError("两个 home 不能是同一个目录。")
        # 原生 .app 外壳会自带一个 token 并通过 --token 传入；命令行启动时自行生成。
        self.token = token or secrets.token_urlsafe(32)
        self.home_a = home_a
        self.home_b = home_b
        self.state_dir = os.path.abspath(os.path.expanduser(state_dir))
        # 自动同步有自己独立的状态根（launchd 代理在跑），与界面的 --state-dir 无关
        self.autosync_root = os.path.abspath(os.path.expanduser(
            os.environ.get("WB_AUTOSYNC_ROOT") or DEFAULT_AUTOSYNC_ROOT
        ))
        self.plan_path: str | None = None
        self.plan_id: str | None = None
        self.plan_doc: dict[str, Any] | None = None
        self.last_run_code: int | None = None
        self.busy = threading.Lock()

    def home_for(self, key: str) -> bridge.Home:
        return self.home_a if key == "wb" else self.home_b


def autosync_status(root: str) -> dict[str, Any]:
    """只读读取自动同步代理的状态，供界面展示。

    不复用界面自己的 --state-dir —— 代理是独立进程，它有自己的状态根。

    自动同步靠 macOS 的 launchd 实现。其他平台到此为止，**不去 import
    ``wb_autosync``** —— 那个模块里有 launchctl / osascript 调用，在
    Windows 上 import 没有意义。界面据此换一句说明，而不是报「模块不可用」。
    """
    if not wb_platform.IS_MAC:
        return {
            "available": False,
            "supported": False,
            "note": (
                f"自动同步依赖 macOS 的 launchd，当前系统"
                f"（{wb_platform.platform_label()}）暂不支持。"
                "盘点、计划、备份、执行、回滚都不受影响，仍可手动操作。"
            ),
        }
    try:
        import wb_autosync as asy
    except Exception as exc:  # 模块缺失或导入失败都不该拖垮界面
        return {"available": False, "supported": True, "error": str(exc)}
    ns = argparse.Namespace(state_root=root)
    try:
        saved = asy.read_status(ns)
        return {
            "available": True,
            "supported": True,
            "installed": os.path.exists(asy.plist_target()),
            "paused": os.path.exists(asy.paused_path(ns)),
            "state_root": root,
            "log": asy.log_path(ns),
            "plist": asy.plist_target(),
            "status": saved,
            "install_available": not IS_FROZEN,
            "install_hint": "python3 tools/wb_autosync.py install",
        }
    except Exception as exc:
        return {"available": False, "supported": True, "error": str(exc)}


# --------------------------------------------------------------------------
# 输出捕获
# --------------------------------------------------------------------------


class _StreamWriter:
    """把引擎的 stdout/stderr 逐段转给回调。

    前端断开时吞掉写异常，保证迁移继续跑完——半途中断比多推几行危险得多。
    """

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit

    def write(self, text: str) -> int:
        if text:
            try:
                self._emit(text)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        return len(text)

    def flush(self) -> None:
        return None


def run_captured(worker: Callable[[], int], emit: Callable[[str], None]) -> int:
    writer = _StreamWriter(emit)
    try:
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            return int(worker())
    except bridge.BridgeError as exc:
        writer.write(f"\n错误：{exc}\n")
        return 2
    except Exception as exc:  # noqa: BLE001  把未预期失败也送到界面，而不是让请求挂死
        writer.write(f"\n未预期失败：{type(exc).__name__}: {exc}\n")
        return 2


# --------------------------------------------------------------------------
# HTTP 处理器
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"wb-home-bridge-ui/{VERSION}"
    protocol_version = "HTTP/1.1"
    state: UiState

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return None

    # -- 输出助手 ---------------------------------------------------------

    def _send_bytes(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", code)

    def _sse_open(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            pass

    def _sse_send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False).replace("\r", " ")
        self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _token_ok(self, qs: dict[str, list[str]]) -> bool:
        given = (qs.get("t") or [""])[0] or self.headers.get("X-WB-Token") or ""
        return secrets.compare_digest(given, self.state.token)

    # -- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            self._send_bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if not self._token_ok(qs):
            self._send_json({"error": "token 无效或缺失，请从终端给出的链接进入。"}, 403)
            return
        try:
            if url.path == "/api/state":
                self._send_json(self._api_state())
            elif url.path == "/api/survey":
                self._api_survey()
            elif url.path == "/api/apply":
                self._api_apply(qs)
            else:
                self._send_json({"error": f"未知端点 {url.path}"}, 404)
        except bridge.BridgeError as exc:
            self._send_json({"error": str(exc)}, 400)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if not self._token_ok(qs):
            self._send_json({"error": "token 无效或缺失。"}, 403)
            return
        body = self._read_json()
        try:
            if url.path == "/api/plan":
                self._api_plan(body)
            elif url.path == "/api/verify":
                self._api_verify()
            elif url.path == "/api/restore":
                self._api_restore(body)
            elif url.path == "/api/backup":
                self._api_backup(body)
            else:
                self._send_json({"error": f"未知端点 {url.path}"}, 404)
        except bridge.BridgeError as exc:
            self._send_json({"error": str(exc)}, 400)

    # -- 端点实现 ---------------------------------------------------------

    def _api_state(self) -> dict[str, Any]:
        st = self.state
        clients = wb_platform.client_statuses()
        for item in clients:
            home = st.home_for(item["key"])
            item["home"] = home.path
            item["home_confirmed"] = True
            item["home_exists"] = os.path.isdir(home.path)
            item["db_exists"] = os.path.isfile(home.db_path)
            item["home_note"] = "" if item["db_exists"] else "该目录下没有 workbuddy.db"
        running = [c for c in clients if c["running"]]
        return {
            "platform": wb_platform.platform_label(),
            "state_dir": st.state_dir,
            "clients": clients,
            "running_names": [c["display"] for c in running],
            "all_stopped": not running,
            "has_plan": st.plan_path is not None,
            "last_run_code": st.last_run_code,
            "autosync": autosync_status(st.autosync_root),
        }

    def _api_survey(self) -> None:
        st = self.state
        for home in (st.home_a, st.home_b):
            home.require_valid()
        info_a = bridge.survey(st.home_a)
        info_b = bridge.survey(st.home_b)
        for info in (info_a, info_b):
            info["sizes_human"] = {
                name: bridge.human(size)
                for name, size in (info.get("sizes") or {}).items() if size
            }
        set_a = set(info_a.get("skills") or [])
        set_b = set(info_b.get("skills") or [])
        self._send_json(
            {
                "homes": [info_a, info_b],
                "skills": {
                    "only_a": sorted(set_a - set_b),
                    "only_b": sorted(set_b - set_a),
                    "shared": sorted(set_a & set_b),
                },
            }
        )

    def _api_plan(self, body: dict[str, Any]) -> None:
        st = self.state
        opts = body.get("options") or {}
        args = argparse.Namespace(
            home_a=st.home_a.path,
            home_b=st.home_b.path,
            json=True,
            no_changes=not opts.get("include_changes", True),
            no_skills=not opts.get("include_skills", True),
            include_plugins=bool(opts.get("include_plugins")),
            include_automations=bool(opts.get("include_automations")),
            include_storage=bool(opts.get("include_storage")),
            include_connectors=bool(opts.get("include_connectors")),
            no_claw=not opts.get("include_claw", True),
            no_memory=not opts.get("include_memory", True),
            overwrite_assets=bool(opts.get("overwrite_assets")),
            allow_client_running=False,
        )
        plan = bridge.build_plan(args, st.home_a, st.home_b)
        doc = plan.as_dict()

        plan_dir = os.path.join(st.state_dir, "plans")
        os.makedirs(plan_dir, mode=0o700, exist_ok=True)
        path = os.path.join(plan_dir, f"{plan.plan_id[:16]}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.chmod(path, 0o600)
        st.plan_path, st.plan_id, st.plan_doc = path, plan.plan_id, doc
        st.last_run_code = None
        self._send_json(self._plan_view(doc))

    def _plan_view(self, doc: dict[str, Any]) -> dict[str, Any]:
        rows = doc.get("rows") or {}
        sample: dict[str, list[dict[str, Any]]] = {}
        counts: dict[str, dict[str, int]] = {}
        for side in ("a2b", "b2a"):
            side_rows = rows.get(side) or {}
            counts[side] = {table: len(items) for table, items in side_rows.items()}
            sample[side] = [
                {
                    "id": row.get("id"),
                    "title": row.get("title") or "(无标题)",
                    "cwd": row.get("cwd"),
                    "status": row.get("status"),
                }
                for row in (side_rows.get("sessions") or [])[:30]
            ]
        return {
            "plan_id": doc.get("plan_id"),
            "created_at": doc.get("created_at"),
            "homes": doc.get("homes"),
            "options": doc.get("options"),
            "summary": doc.get("summary"),
            "counts": counts,
            "sample": sample,
        }

    def _api_verify(self) -> None:
        st = self.state
        if not st.plan_path:
            self._send_json({"error": "尚未生成计划。"}, 400)
            return
        args = argparse.Namespace(
            home_a=st.home_a.path, home_b=st.home_b.path,
            plan=st.plan_path, json=False,
        )
        lines: list[str] = []

        def emit(text: str) -> None:
            lines.append(text)

        code = run_captured(lambda: bridge.do_verify(args, st.home_a, st.home_b), emit)
        self._send_json({"code": code, "log": "".join(lines)})

    def _api_restore(self, body: dict[str, Any]) -> None:
        st = self.state
        confirm = str(body.get("confirm") or "")
        if not st.plan_id:
            self._send_json({"error": "本会话还没有计划，无法回滚。"}, 400)
            return
        run_dir = os.path.join(st.state_dir, "runs", st.plan_id)
        if not os.path.isdir(run_dir):
            self._send_json({"error": f"找不到运行记录目录：{run_dir}"}, 400)
            return
        args = argparse.Namespace(run_dir=run_dir, confirm=confirm)
        lines: list[str] = []

        def emit(text: str) -> None:
            lines.append(text)

        code = run_captured(lambda: bridge.do_restore(args), emit)
        self._send_json({"code": code, "log": "".join(lines)})

    def _api_backup(self, body: dict[str, Any]) -> None:
        st = self.state
        dest = str(body.get("dest") or os.path.join(st.state_dir, "backups"))
        if st.busy.locked():
            self._send_json({"error": "已有操作在执行中，请等待完成。"}, 409)
            return
        self._sse_open()
        args = argparse.Namespace(
            home_a=st.home_a.path, home_b=st.home_b.path, json=False,
            dest=dest, label="", include_heavy=False, allow_client_running=True,
        )
        code = self._run_stream(
            lambda: bridge.do_backup(args, [st.home_a, st.home_b])
        )
        self._finish_stream(code, "备份")

    def _api_apply(self, qs: dict[str, list[str]]) -> None:
        st = self.state
        if not st.plan_path or not st.plan_id:
            self._send_json({"error": "尚未生成计划。"}, 400)
            return
        if qs.get("plan_id", [""])[0] != st.plan_id:
            self._send_json({"error": "请求里的 plan_id 与当前计划不一致，请重新生成计划。"}, 400)
            return
        if st.busy.locked():
            self._send_json({"error": "已有操作在执行中，请等待完成。"}, 409)
            return
        running = wb_platform.running_clients()
        if running:
            names = "、".join(item["display"] for item in running)
            self._send_json(
                {"error": f"{names} 仍在运行。请先完全退出客户端再执行。"}, 409
            )
            return

        confirm = qs.get("confirm", [""])[0]
        self._sse_open()
        args = argparse.Namespace(
            home_a=st.home_a.path, home_b=st.home_b.path,
            plan=st.plan_path, state_dir=st.state_dir, confirm=confirm,
            allow_client_running=False,
        )

        def worker() -> int:
            bridge.require_clients_stopped(False)
            return bridge.do_apply(args, st.home_a, st.home_b)

        code = self._run_stream(worker)
        st.last_run_code = code
        self._finish_stream(code, "迁移")

    def _run_stream(self, worker: Callable[[], int]) -> int:
        with self.state.busy:
            self._sse_send({"type": "start"})
            try:
                return run_captured(worker, lambda text: self._sse_send(
                    {"type": "log", "text": text}
                ))
            except (BrokenPipeError, ConnectionResetError):
                # 浏览器断开：任务已在子流程里跑完，这里只记录结果。
                return -1

    def _finish_stream(self, code: int, what: str) -> None:
        try:
            self._sse_send({"type": "done", "code": code, "what": what})
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# --------------------------------------------------------------------------
# 前端
# --------------------------------------------------------------------------


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>跨 App 数据目录打通</title>
<style>
:root {
  --bg: #fafaf8; --panel: #ffffff; --line: #e5e3dd; --text: #22211f;
  --muted: #6f6d66; --faint: #9a978f; --accent: #534ab7; --accent-soft: #eeedfe;
  --ok: #1d9e75; --ok-soft: #e1f5ee; --warn: #ba7517; --warn-soft: #faeeda;
  --bad: #a32d2d; --bad-soft: #fcebeb; --code: #f1efe8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1c1b1a; --panel: #262523; --line: #3a3936; --text: #f0eee9;
    --muted: #a9a69e; --faint: #7c7a73; --accent: #afa9ec; --accent-soft: #2f2b52;
    --ok: #5dcaa5; --ok-soft: #16362c; --warn: #ef9f27; --warn-soft: #3a2d12;
    --bad: #f09595; --bad-soft: #3d1f1f; --code: #33322f;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.6 -apple-system, "SF Pro Text", "PingFang SC", "Microsoft YaHei", sans-serif;
}
.wrap { max-width: 940px; margin: 0 auto; padding: 32px 20px 72px; }
h1 { font-size: 20px; font-weight: 500; margin: 0 0 4px; }
h2 { font-size: 15px; font-weight: 500; margin: 0 0 12px; }
p.lead { color: var(--muted); margin: 0 0 24px; }
.card {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 12px; padding: 18px 20px; margin-bottom: 16px;
}
.row { display: flex; gap: 16px; flex-wrap: wrap; }
.row > * { flex: 1 1 300px; min-width: 0; }
.kv { display: flex; justify-content: space-between; gap: 12px; padding: 3px 0; }
.kv span:first-child { color: var(--muted); }
.kv span:last-child { text-align: right; word-break: break-all; }
.host { font-weight: 500; font-size: 15px; margin-bottom: 6px; }
.path { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
        color: var(--muted); word-break: break-all; }
.pill {
  display: inline-flex; align-items: center; gap: 6px; border-radius: 999px;
  padding: 2px 10px; font-size: 12px; font-weight: 500;
}
.pill.ok { background: var(--ok-soft); color: var(--ok); }
.pill.bad { background: var(--bad-soft); color: var(--bad); }
.pill.warn { background: var(--warn-soft); color: var(--warn); }
.pill.idle { background: var(--code); color: var(--muted); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.opt { display: flex; gap: 10px; align-items: flex-start; padding: 9px 0;
       border-bottom: 1px solid var(--line); }
.opt:last-child { border-bottom: 0; }
.opt input { margin-top: 4px; accent-color: var(--accent); }
.opt .name { font-weight: 500; }
.opt .why { color: var(--muted); font-size: 12.5px; }
.opt .name.risk { color: var(--warn); }
button {
  font: inherit; font-weight: 500; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: var(--panel); color: var(--text);
  padding: 9px 18px;
}
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.danger { background: var(--bad); border-color: var(--bad); color: #fff; }
button:disabled { opacity: .45; cursor: not-allowed; }
input[type=text], input[type=password] {
  font: inherit; font-family: ui-monospace, Menlo, monospace; font-size: 12px;
  padding: 9px 11px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--bg); color: var(--text); width: 100%;
}
pre {
  background: var(--code); border-radius: 8px; padding: 12px 14px;
  font-family: ui-monospace, Menlo, monospace; font-size: 12px;
  white-space: pre-wrap; word-break: break-word; margin: 0;
  max-height: 340px; overflow: auto; color: var(--text);
}
.code-id {
  font-family: ui-monospace, Menlo, monospace; font-size: 11.5px;
  background: var(--accent-soft); color: var(--accent);
  padding: 8px 10px; border-radius: 8px; word-break: break-all;
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.hint { color: var(--muted); font-size: 12.5px; margin: 8px 0 0; }
.gate { padding: 10px 12px; border-radius: 8px; margin-bottom: 12px; font-size: 13px; }
.gate.ok { background: var(--ok-soft); color: var(--ok); }
.gate.bad { background: var(--bad-soft); color: var(--bad); }
.gate.warn { background: var(--warn-soft); color: var(--warn); }
.hidden { display: none; }
.mono { font-family: ui-monospace, Menlo, monospace; }
.sep { height: 1px; background: var(--line); margin: 14px 0; }
.actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 14px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>跨 App 数据目录打通</h1>
  <p class="lead">把 WorkBuddy 与 WorkBuddy AI 的历史会话、记忆、技能互相补全。只新增，不覆盖已有数据。</p>

  <div class="card">
    <h2>运行环境与客户端状态</h2>
    <div id="gate" class="gate warn">正在检查客户端进程…</div>
    <div class="row" id="clients"></div>
    <p class="hint" id="platform"></p>
  </div>

  <div class="card">
    <h2>自动同步</h2>
    <div id="autosync" class="hint">正在读取自动同步状态…</div>
  </div>

  <div class="card">
    <h2>1 · 盘点</h2>
    <div id="survey" class="hint">点击下方按钮读取两个数据目录的现状（只读操作）。</div>
    <div class="actions"><button id="btn-survey">读取盘点</button></div>
  </div>

  <div class="card">
    <h2>2 · 迁移范围</h2>
    <div id="options"></div>
    <div class="actions">
      <button id="btn-plan" class="primary">生成计划</button>
      <span class="hint" id="plan-status"></span>
    </div>
  </div>

  <div class="card hidden" id="plan-card">
    <h2>3 · 计划审阅</h2>
    <div id="plan-summary"></div>
    <div class="sep"></div>
    <div class="kv"><span>plan_id</span><span></span></div>
    <div class="code-id" id="plan-id"></div>
    <p class="hint">执行时必须完整粘贴上面的 plan_id。这是与命令版本一致的确认强度，用于防止误点。</p>
    <div class="sep"></div>
    <div id="plan-detail"></div>
  </div>

  <div class="card hidden" id="run-card">
    <h2>4 · 执行</h2>
    <div id="run-gate" class="gate bad">需要先退出两个客户端</div>
    <div class="actions">
      <input type="text" id="confirm-input" placeholder="粘贴完整 plan_id" style="flex:1 1 380px">
      <button id="btn-apply" class="danger">执行迁移</button>
      <button id="btn-backup">先做备份</button>
      <button id="btn-verify">核验</button>
    </div>
    <p class="hint">备份约需数百 MB 空间，默认排除 app / logs / traces 等与迁移无关的大目录。</p>
    <div class="sep"></div>
    <pre id="log" style="max-height:420px">等待执行…</pre>
  </div>

  <div class="card hidden" id="restore-card">
    <h2>5 · 回滚</h2>
    <p class="hint">回滚只撤销本工具新建的会话行与文件，不会恢复被合并改写的记忆原文（已留 .before-bridge-* 备份）。</p>
    <div class="actions">
      <button id="btn-restore">回滚本次执行</button>
      <span class="hint" id="restore-status"></span>
    </div>
  </div>
</div>

<script>
const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = (id) => document.getElementById(id);

const OPTIONS = [
  { key: 'include_changes', label: '会话变更记录', on: true,
    why: 'changes-detail / changes-index / file-history。缺失会导致变更面板打不开。' },
  { key: 'include_memory', label: '长期记忆', on: true,
    why: '合并两边记忆，改写归属到目标账号，改写前留 .before-bridge-* 备份。' },
  { key: 'include_skills', label: '用户技能目录', on: true,
    why: '目录并集。同名技能两边各留各的版本，不互相覆盖。' },
  { key: 'include_claw', label: 'settings.json 渠道绑定', on: true,
    why: '只合并渠道绑定段，不触碰其他设置项。' },
  { key: 'include_plugins', label: '插件缓存', on: false,
    why: 'plugins/cache 体积可能较大，非必需。' },
  { key: 'include_storage', label: '账号个人存储', on: false,
    why: '按账号目录改名合并，已存在不覆盖。' },
  { key: 'include_connectors', label: '连接器开关状态', on: false, risk: true,
    why: '只并开关状态，凭据无法跨 home 解密，仍需重新授权。' },
  { key: 'include_automations', label: '自动化任务', on: false, risk: true,
    why: '复制后两个 App 会各跑一遍，日报类任务会被发两次。落地为暂停状态。' },
  { key: 'overwrite_assets', label: '覆盖已存在的资产文件', on: false, risk: true,
    why: '默认跳过已存在文件。开启后会用源文件覆盖目标同名文件。' },
];

function renderOptions() {
  $('options').innerHTML = OPTIONS.map((o) => `
    <label class="opt">
      <input type="checkbox" data-key="${o.key}" ${o.on ? 'checked' : ''}>
      <span>
        <span class="name ${o.risk ? 'risk' : ''}">${o.label}</span>
        <span class="why">${o.why}</span>
      </span>
    </label>`).join('');
}

function collectOptions() {
  const out = {};
  document.querySelectorAll('#options input[type=checkbox]').forEach((el) => {
    out[el.dataset.key] = el.checked;
  });
  return out;
}

async function api(path, opts) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(path + sep + 't=' + encodeURIComponent(TOKEN), opts);
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text }; }
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

async function stream(path, onEvent) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(path + sep + 't=' + encodeURIComponent(TOKEN));
  if (!res.ok) {
    const text = await res.text();
    let msg = 'HTTP ' + res.status;
    try { msg = JSON.parse(text).error || msg; } catch (e) {}
    throw new Error(msg);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const line = chunk.split('\n').find((l) => l.startsWith('data: '));
      if (line) {
        try { onEvent(JSON.parse(line.slice(6))); } catch (e) {}
      }
    }
  }
}

function appendLog(text) {
  const el = $('log');
  el.textContent += text;
  el.scrollTop = el.scrollHeight;
}

let lastState = null;

async function refreshState() {
  let s;
  try { s = await api('/api/state'); } catch (e) {
    $('gate').className = 'gate bad';
    $('gate').textContent = '无法读取状态：' + e.message;
    return;
  }
  lastState = s;
  $('platform').textContent = s.platform + ' · 状态目录 ' + s.state_dir;

  $('clients').innerHTML = s.clients.map((c) => `
    <div class="card" style="margin:0">
      <div class="host">${c.display}
        <span class="pill ${c.running ? 'bad' : 'ok'}" style="margin-left:6px">
          <span class="dot"></span>${c.running ? '运行中' : '已退出'}
        </span>
      </div>
      <div class="path">${c.home}</div>
      <div class="kv"><span>数据目录</span><span>${c.db_exists ? '正常' : '缺少 workbuddy.db'}</span></div>
      <div class="kv"><span>进程数</span><span>${c.processes.length}</span></div>
    </div>`).join('');

  const gate = $('gate');
  if (s.all_stopped) {
    gate.className = 'gate ok';
    gate.textContent = '两个客户端都已退出，可以执行写操作。';
  } else {
    gate.className = 'gate bad';
    gate.textContent = s.running_names.join('、') + ' 仍在运行。写操作会被拒绝，请先完全退出。';
  }
  renderAutosync(s.autosync);
  updateRunGate();
  $('btn-apply').disabled = !s.all_stopped || !s.has_plan;
  $('btn-backup').disabled = !s.all_stopped;
}

const AUTOSYNC_LABELS = {
  ok: ['ok', '同步成功'],
  no_change: ['ok', '检查过，无变化'],
  skipped_running: ['warn', '客户端仍在运行，已跳过'],
  paused: ['warn', '已暂停'],
  dry_run: ['warn', '演练模式（未写入）'],
  plan_failed: ['bad', '生成计划失败'],
  backup_failed: ['bad', '备份失败'],
  apply_failed: ['bad', '执行失败'],
  verify_failed: ['bad', '核验未通过'],
  error: ['bad', '出错'],
  unknown: ['warn', '状态未知'],
};

function renderAutosync(a) {
  const el = $('autosync');
  if (a && a.supported === false) {
    el.innerHTML = '<div class="gate warn">本平台暂不支持自动同步</div>'
      + `<p class="hint">${a.note || '自动同步依赖 macOS 的 launchd。'}</p>`;
    return;
  }
  if (!a || !a.available) {
    el.innerHTML = '自动同步模块不可用。';
    return;
  }
  if (!a.installed) {
    el.innerHTML = '<div class="gate warn">自动同步代理未安装。'
      + '安装后两个客户端一旦都退出就会自动同步，无需手动执行。</div>'
      + (a.install_available === false
          ? '<p class="hint">当前运行的是打包版，不含代理安装器。'
            + '要启用自动同步，请改用源码运行方式（见 README）。</p>'
          : `<p class="hint">安装：<code>${a.install_hint}</code></p>`);
    return;
  }
  const st = a.status || {};
  const [tone, label] = AUTOSYNC_LABELS[st.last_result] || ['warn', st.last_result || '还没有运行记录'];
  const bits = [];
  if (st.last_sessions !== undefined && st.last_sessions !== null) {
    bits.push(`会话 ${st.last_sessions} 条`);
  }
  if (st.last_human) bits.push(st.last_human);
  if (st.last_duration_s) bits.push(`${st.last_duration_s} 秒`);
  el.innerHTML = `
    <div class="gate ${a.paused ? 'warn' : 'ok'}">
      ${a.paused ? '自动同步已暂停' : '自动同步已开启'}
      &nbsp;·&nbsp; 最近一次：<b>${label}</b>
      ${st.updated_at ? `（${st.updated_at.replace('T', ' ').slice(0, 19)}）` : ''}
      ${bits.length ? `&nbsp;·&nbsp; ${bits.join(' · ')}` : ''}
    </div>
    ${st.last_note ? `<p class="hint">备注：${st.last_note}</p>` : ''}
    <p class="hint">每 120 秒检查一次，两个客户端都退出时才写入；写入前自动备份数据库，
    可在状态目录里回滚。状态根：<code>${a.state_root}</code></p>`;
}

function updateRunGate() {
  const el = $('run-gate');
  if (!lastState) return;
  if (lastState.all_stopped) {
    el.className = 'gate ok';
    el.textContent = '客户端已退出。粘贴 plan_id 后即可执行。';
  } else {
    el.className = 'gate bad';
    el.textContent = lastState.running_names.join('、') + ' 仍在运行，执行按钮已锁定。';
  }
}

function renderSurvey(s) {
  const human = (n) => {
    if (!n) return '0B';
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + u[i];
  };
  const card = (h) => {
    const c = h.counts || {};
    return `<div class="card" style="margin:0">
      <div class="host">${h.label}</div>
      <div class="path">${h.path}</div>
      <div class="kv"><span>当前账号</span><span class="mono">${(h.uid || '').slice(0, 8)}</span></div>
      <div class="kv"><span>会话 / 正文</span><span>${c.sessions || 0} / ${c.conversations || 0}</span></div>
      <div class="kv"><span>自动化</span><span>${c.automations || 0}</span></div>
      <div class="kv"><span>技能</span><span>${(h.skills || []).length} 个</span></div>
      <div class="kv"><span>projects</span><span>${human((h.sizes || {}).projects)}</span></div>
      <div class="kv"><span>changes-detail</span><span>${human((h.sizes || {})['changes-detail'])}</span></div>
    </div>`;
  };
  const sk = s.skills || {};
  $('survey').innerHTML = `<div class="row">${s.homes.map(card).join('')}</div>
    <div class="sep"></div>
    <table>
      <tr><th>技能差异</th><th class="num">数量</th></tr>
      <tr><td>仅左侧独有</td><td class="num">${(sk.only_a || []).length}</td></tr>
      <tr><td>仅右侧独有</td><td class="num">${(sk.only_b || []).length}</td></tr>
      <tr><td>两边同名</td><td class="num">${(sk.shared || []).length}</td></tr>
      <tr><td>合并后每边可用</td><td class="num">${
        new Set([...(sk.only_a || []), ...(sk.only_b || []), ...(sk.shared || [])]).size}</td></tr>
    </table>
    <p class="hint">同名技能两边各留各的版本，不互相覆盖。</p>`;
}

function renderPlan(plan) {
  const t = (plan.summary || {}).totals || {};
  const rows = [['a2b', '左 → 右'], ['b2a', '右 → 左']].map(([side, label]) => {
    const s = (plan.summary || {})[side] || {};
    const c = (plan.counts || {})[side] || {};
    const names = Object.entries(c).map(([k, v]) => `${k} ${v}`).join('、') || '无';
    return `<tr>
      <td>${label}<div class="path">${s.from || ''} → ${s.to || ''}</div></td>
      <td class="num">${s.sessions_to_copy || 0}</td>
      <td class="num">${s.sessions_skipped || 0}</td>
      <td>${names}</td>
    </tr>`;
  }).join('');

  $('plan-summary').innerHTML = `
    <table>
      <tr><th>方向</th><th class="num">待复制</th><th class="num">已存在跳过</th><th>表行数</th></tr>
      ${rows}
      <tr><th>合计</th><th class="num">${t.sessions_to_copy || 0}</th><th></th>
          <th>约 ${t.approx_human || '0B'}</th></tr>
    </table>
    <p class="hint">生成于 ${plan.created_at || ''}。计划冻结了数据指纹，执行前若源数据变化会被拒绝并要求重新生成。</p>`;

  $('plan-id').textContent = plan.plan_id || '';

  const samples = (plan.sample || {}).a2b || [];
  const samples2 = (plan.sample || {}).b2a || [];
  const list = (arr) => arr.length
    ? `<ul style="margin:6px 0 0;padding-left:18px">${arr.map((x) =>
        `<li>${x.title} <span class="path">${(x.cwd || '').split('/').slice(-1)[0]}</span></li>`).join('')}</ul>`
    : '<p class="hint">没有新增会话。</p>';
  $('plan-detail').innerHTML = `
    <h2>将新增的会话</h2>
    <div class="row">
      <div><div class="host">左 → 右（前 ${samples.length} 条）</div>${list(samples)}</div>
      <div><div class="host">右 → 左（前 ${samples2.length} 条）</div>${list(samples2)}</div>
    </div>
    <div class="sep"></div>
    <div class="kv"><span>生效选项</span><span>${Object.entries(plan.options || {})
      .filter(([, v]) => v).map(([k]) => k).join('、') || '无'}</span></div>`;

  $('plan-card').classList.remove('hidden');
  $('run-card').classList.remove('hidden');
  $('plan-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
  updateRunGate();
}

$('btn-survey').onclick = async () => {
  const btn = $('btn-survey');
  btn.disabled = true; btn.textContent = '读取中…';
  try {
    renderSurvey(await api('/api/survey'));
    btn.textContent = '重新读取';
  } catch (e) {
    $('survey').innerHTML = '<div class="gate bad">' + e.message + '</div>';
    btn.textContent = '重试';
  } finally { btn.disabled = false; }
};

$('btn-plan').onclick = async () => {
  const btn = $('btn-plan');
  btn.disabled = true; $('plan-status').textContent = '生成中…';
  try {
    const plan = await api('/api/plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ options: collectOptions() }),
    });
    renderPlan(plan);
    $('plan-status').textContent = '计划已生成';
    $('confirm-input').value = '';
    $('log').textContent = '等待执行…';
    $('restore-card').classList.remove('hidden');
  } catch (e) {
    $('plan-status').innerHTML = '<span style="color:var(--bad)">' + e.message + '</span>';
  } finally { btn.disabled = false; }
};

$('btn-apply').onclick = async () => {
  if (!lastState || !lastState.all_stopped) return;
  const confirm = $('confirm-input').value.trim();
  if (!confirm) { alert('请先粘贴完整 plan_id'); return; }
  if (!window.confirm('确认执行迁移？\n\n这会向两个数据目录写入数据。\n完成后需要重启两个客户端才能看到新会话。')) return;
  const btn = $('btn-apply');
  btn.disabled = true; btn.textContent = '执行中…';
  $('log').textContent = '';
  const url = '/api/apply?plan_id=' + encodeURIComponent($('plan-id').textContent)
            + '&confirm=' + encodeURIComponent(confirm);
  try {
    await stream(url, (ev) => {
      if (ev.type === 'log') appendLog(ev.text);
      if (ev.type === 'done') {
        appendLog('\n[退出码 ' + ev.code + '] ' +
          (ev.code === 0 ? '完成。请重启两个客户端后再查看。' : '未成功，请查看上方日志。'));
      }
    });
  } catch (e) {
    appendLog('\n执行未启动：' + e.message + '\n');
  } finally {
    btn.disabled = false; btn.textContent = '执行迁移';
    refreshState();
  }
};

$('btn-backup').onclick = async () => {
  if (!lastState || !lastState.all_stopped) return;
  if (!window.confirm('开始备份两个数据目录？\n\n默认排除 app / logs / traces 等大目录。')) return;
  const btn = $('btn-backup');
  btn.disabled = true; btn.textContent = '备份中…';
  $('log').textContent = '';
  try {
    await stream('/api/backup', (ev) => {
      if (ev.type === 'log') appendLog(ev.text);
      if (ev.type === 'done') appendLog('\n[退出码 ' + ev.code + ']');
    });
  } catch (e) { appendLog('备份未启动：' + e.message + '\n'); }
  finally { btn.disabled = false; btn.textContent = '先做备份'; }
};

$('btn-verify').onclick = async () => {
  const btn = $('btn-verify');
  btn.disabled = true; btn.textContent = '核验中…'; $('log').textContent = '';
  try {
    const r = await api('/api/verify', { method: 'POST' });
    $('log').textContent = r.log || '(无输出)';
  } catch (e) { $('log').textContent = '核验失败：' + e.message; }
  finally { btn.disabled = false; btn.textContent = '核验'; }
};

$('btn-restore').onclick = async () => {
  const planId = $('plan-id').textContent.trim();
  if (!planId) { alert('没有可回滚的计划'); return; }
  const confirm = window.prompt('回滚会删除本工具新建的会话行与文件。\n\n请输入完整 plan_id 确认：');
  if (confirm === null) return;
  const btn = $('btn-restore');
  btn.disabled = true; $('restore-status').textContent = '回滚中…';
  try {
    const r = await api('/api/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: confirm }),
    });
    $('restore-status').innerHTML = r.code === 0
      ? '<span style="color:var(--ok)">已回滚</span>'
      : '<span style="color:var(--bad)">回滚未成功</span>';
    $('log').textContent = r.log || '(无输出)';
    $('run-card').classList.remove('hidden');
  } catch (e) {
    $('restore-status').innerHTML = '<span style="color:var(--bad)">' + e.message + '</span>';
  } finally { btn.disabled = false; }
};

renderOptions();
refreshState();
setInterval(refreshState, 2000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------


def pick_port(preferred: int) -> int:
    """挑一个可用的 127.0.0.1 端口；``preferred`` 为 0 时交给内核分配。

    注意 ``bind(("127.0.0.1", 0))`` 一定会成功——0 的含义是"随便给一个"，
    所以必须回读 ``getsockname()`` 才能拿到真实端口。直接 return 传进来的
    0 会拼出 ``http://127.0.0.1:0/`` 这种无效地址，浏览器打不开，
    用户看到的就是"双击没反应"。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
            return sock.getsockname()[1]
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_state(args: argparse.Namespace) -> UiState:
    return UiState(
        bridge.make_home(wb_platform.CLIENTS_BY_KEY["wb"], args.home_a),
        bridge.make_home(wb_platform.CLIENTS_BY_KEY["wb_ai"], args.home_b),
        args.state_dir,
        getattr(args, "token", None),
    )


# 打包入口（app_main）会把它设成原生弹窗函数。命令行运行时保持 None：
# 终端里本来就打印了地址，再弹个框是打扰。
NOTIFY_HOOK = None


def _spawn(argv: list[str], timeout: float = 10.0) -> bool:
    """跑一条外部命令，只看它成不成功。超时或命令不存在都算失败。

    输出一律丢弃：打包版没有终端，子进程的噪声不该混进原生错误弹窗。
    """
    import subprocess

    try:
        done = subprocess.run(argv, check=False, timeout=timeout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    return done.returncode == 0


def open_browser(url: str) -> bool:
    """打开默认浏览器，``webbrowser`` 不灵时用系统原生命令兜底。

    打包成 ``.app`` / ``.exe`` 之后，``webbrowser`` 偶尔找不到默认浏览器
    （环境变量被裁掉、没有注册的 handler），返回 ``False`` 而不是抛异常。
    这时退回到系统命令，否则用户双击后的现象就是"没反应"。
    """
    # macOS 优先走 /usr/bin/open：它直接经 LaunchServices 拉起默认浏览器，
    # 而 webbrowser 在 mac 上是 MacOSXOSAScript，要靠 AppleEvent 隔空指挥，
    # 打包成 .app 后实测会 `execution error: AppleEvent 已超时 (-1712)`。
    if sys.platform == "darwin":
        if _spawn(["/usr/bin/open", url]):
            return True

    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass

    try:
        if sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        return _spawn(["xdg-open", url])
    except Exception:
        return False


def _startup_open(url: str) -> None:
    """启动后自动开浏览器。**失败时务必把地址摊给用户**。

    打包版没有终端，而带 token 的地址只打印在 stdout 里——浏览器一旦没打开，
    用户既看不到界面、也拿不到地址，现象和"双击没反应"完全一样。
    所以这里在失败时调宿主注入的原生弹窗，把 URL 亮出来让用户自己复制。
    """
    if open_browser(url):
        return
    hook = NOTIFY_HOOK
    if hook is None:
        return
    try:
        hook(
            "wb-account-sync",
            "服务已经启动，但没能自动打开浏览器。\n\n"
            "请把下面这个地址复制到浏览器打开（其中包含本次访问的令牌，"
            "少了它打不开数据）：\n\n" + url,
        )
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wb-ui",
        description="跨 App 数据目录打通工具的本地浏览器界面（只绑 127.0.0.1）。",
    )
    parser.add_argument("--version", action="version", version=f"wb-ui {VERSION}")
    parser.add_argument("--home-a", default=None, help="WorkBuddy 的数据目录（默认自动探测）")
    parser.add_argument("--home-b", default=None, help="WorkBuddy AI 的数据目录（默认自动探测）")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                        help=f"状态目录（计划与运行记录），默认 {DEFAULT_STATE_DIR}")
    parser.add_argument("--port", type=int, default=0, help="监听端口，默认自动分配")
    parser.add_argument("--token", default=None,
                        help="显式指定访问 token（原生 .app 外壳用；默认每次启动随机生成）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--handshake", action="store_true",
                        help="就绪后向 stdout 输出一行 WBUI_READY {json}，供宿主程序读取")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        state = build_state(args)
    except bridge.BridgeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    port = pick_port(args.port)
    Handler.state = state
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True

    url = f"http://127.0.0.1:{port}/?t={state.token}"
    print(f"wb-home-bridge UI {VERSION}")
    print(f"  平台     : {wb_platform.platform_label()}")
    print(f"  左侧目录 : {state.home_a.path}")
    print(f"  右侧目录 : {state.home_b.path}")
    print(f"  状态目录 : {state.state_dir}")
    print(f"  地址     : {url}")
    print()
    print("本服务只监听 127.0.0.1，且所有接口都要求上面链接里的 token。")
    print("按 Ctrl-C 停止。执行写操作前必须先完全退出两个客户端。")
    if args.handshake:
        # 宿主程序（原生 .app 外壳）读这一行来拿地址，必须在用户可读日志之后、
        # 且是单行 JSON，方便流式解析。
        print("WBUI_READY " + json.dumps(
            {"port": port, "token": state.token, "url": url,
             "home_a": state.home_a.path, "home_b": state.home_b.path},
            ensure_ascii=False,
        ))
    # 输出被重定向到文件时 Python 会用块缓冲，这里必须主动刷出，
    # 否则用户从日志里拿不到带 token 的地址。管道被下游关掉（例如
    # `--handshake | head -1`）不算错误，服务该继续跑。
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        pass

    if not args.no_open:
        threading.Timer(0.4, lambda: _startup_open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
