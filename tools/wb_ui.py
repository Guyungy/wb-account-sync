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
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import acct_probe  # noqa: E402  同目录模块，只读账号扫描
import wb_home_bridge as bridge  # noqa: E402  同目录模块
import wb_platform  # noqa: E402
import wb_account_switch  # noqa: E402

VERSION = "0.1.0"
# 是否运行在 PyInstaller 冻结出来的可执行文件里。打包版没有 tools/ 目录，
# 凡是依赖脚本自身路径的功能（例如 launchd 代理注册）都不能照常提供，
# 界面必须把这类入口改成说明而不是给出跑不通的命令。
IS_FROZEN = bool(getattr(sys, "frozen", False))
DEFAULT_STATE_DIR = "~/.wb-home-bridge"
# 自动同步代理的状态根默认与界面状态目录一致，但可用 WB_AUTOSYNC_ROOT 单独指向
DEFAULT_AUTOSYNC_ROOT = "~/.wb-home-bridge"
# 账号面板的缓存时长（秒）。扫描要开数据库 + 采样日志，一次约 1 秒，
# 用户在面板上连点几次不该跑几遍。
ACCOUNTS_CACHE_TTL = 15.0


# --------------------------------------------------------------------------
# 账号扫描（只读）
# --------------------------------------------------------------------------


def accounts_report(state: "UiState", days: int = 14, refresh: bool = False) -> dict[str, Any]:
    """汇总两个 home 的账号与用量。纯只读：只开 sqlite 的 ``mode=ro`` 并读文件。"""
    days = max(7, min(int(days), 180))
    with state.accounts_lock:
        cached = state.accounts_cache
        if cached and not refresh and cached[1] == days:
            if time.time() - cached[0] < ACCOUNTS_CACHE_TTL:
                payload = dict(cached[2])
                payload["cached"] = True
                return payload
        homes = []
        reports = []
        for home in (state.home_a, state.home_b):
            report = acct_probe.build(home.path, home.label, with_logs=True)
            info = acct_probe.to_dict(report, days=days)
            info["key"] = home.slug
            homes.append(info)
            reports.append(report)
        merged = acct_probe.merge_reports(reports)
        payload = {
            "homes": homes,
            "merged": merged,
            "days": days,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cached": False,
            "totals": {
                "accounts": sum(h["totals"]["accounts"] for h in homes),
                "credits_used": round(
                    sum(h["totals"]["credits_used"] for h in homes), 2
                ),
                "sessions": sum(h["totals"]["sessions"] for h in homes),
            },
        }
        state.accounts_cache = (time.time(), days, payload)
        return payload


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
        # 账号扫描要读数据库 + 采样日志，一次约 1 秒，不适合每次点击都重跑
        self.accounts_cache: tuple[float, int, dict[str, Any]] | None = None
        self.accounts_lock = threading.Lock()
        self.switch_lock = threading.Lock()

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
            elif url.path == "/api/accounts":
                self._send_json(self._api_accounts(qs))
            elif url.path == "/api/switch-accounts":
                self._send_json(wb_account_switch.discover())
            elif url.path == "/api/survey":
                self._api_survey()
            elif url.path == "/api/apply":
                self._api_apply(qs)
            else:
                self._send_json({"error": f"未知端点 {url.path}"}, 404)
        except (bridge.BridgeError, wb_account_switch.SwitchError) as exc:
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
            elif url.path == "/api/quit-clients":
                self._api_quit_clients(body)
            elif url.path == "/api/switch-account":
                if not self.state.switch_lock.acquire(blocking=False):
                    self._send_json({"error": "已有账号切换正在进行"}, 409)
                    return
                try:
                    self._send_json(wb_account_switch.switch(str(body.get("uid") or "")))
                    self.state.accounts_cache = None
                finally:
                    self.state.switch_lock.release()
            else:
                self._send_json({"error": f"未知端点 {url.path}"}, 404)
        except (bridge.BridgeError, wb_account_switch.SwitchError) as exc:
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

    def _api_accounts(self, qs: dict[str, list[str]]) -> dict[str, Any]:
        """账号与用量面板的数据源。只读，绝不写任何客户端数据。"""
        try:
            days = int((qs.get("days") or ["14"])[0])
        except (TypeError, ValueError):
            days = 14
        refresh = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
        return accounts_report(self.state, days=days, refresh=refresh)

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
        # 界面不再要求用户手打 plan_id：本会话的计划 id 服务端本来就知道，
        # 让它去满足引擎的 confirm 校验即可。人要做的是「点确认」，不是「抄字符串」。
        confirm = str(body.get("confirm") or st.plan_id or "")
        if not st.plan_id:
            self._send_json({"error": "本会话还没有计划，无法回滚。"}, 400)
            return
        if st.busy.locked():
            self._send_json({"error": "已有操作在执行中，请等待完成。"}, 409)
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
        # 这一步是**防陈旧**：请求里带的必须是本会话当前的那个计划，
        # 否则说明前端拿的是旧计划，拒绝。
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
                {"error": f"{names} 仍在运行。请先完全退出客户端，或勾选「自动退出两个客户端」。"}, 409
            )
            return

        # confirm 由服务端用自己的 plan_id 填。引擎那条「--confirm 必须等于
        # plan_id」的校验本意是拦人手抄短前缀；界面已经把 id 完整持有，
        # 再让用户手打一遍只是仪式，不增加任何安全性。真正的闸门是上面那条
        # plan_id 一致性检查 + 界面上的一次显式确认点击。
        confirm = st.plan_id
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

    def _api_quit_clients(self, body: dict[str, Any]) -> None:
        """请求退出两个客户端，并等到进程真的消失才返回。

        顺序上必须由调用方在「生成计划」之前调用——客户端退出时会 flush
        自己的状态，退出后再建计划指纹才不会漂移。
        """
        st = self.state
        if st.busy.locked():
            self._send_json({"error": "已有操作在执行中，请等待完成。"}, 409)
            return
        allow_force = bool(body.get("allow_force"))
        try:
            wait = float(body.get("wait") or wb_platform.QUIT_GRACE_SECONDS)
        except (TypeError, ValueError):
            wait = wb_platform.QUIT_GRACE_SECONDS
        wait = max(1.0, min(wait, 120.0))
        try:
            result = wb_platform.quit_clients(wait=wait, allow_force=allow_force)
        except wb_platform.PlatformError as exc:
            self._send_json({"error": f"进程探测失败：{exc}"}, 400)
            return
        self._send_json(result)

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
<title>WorkBuddy 账号管理</title>
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
button.big { padding: 11px 28px; font-size: 15px; }
/* 折叠区：默认视图只留一键同步，分步操作收进这里 */
details.card > summary {
  cursor: pointer; list-style: none; font-size: 15px; font-weight: 500;
  display: flex; align-items: center; gap: 8px;
}
details.card > summary::-webkit-details-marker { display: none; }
details.card > summary::before {
  content: "▸"; color: var(--muted); font-size: 12px; transition: transform .15s;
}
details.card[open] > summary::before { content: "▾"; }
.card.sub { border: 0; border-radius: 0; padding: 0; margin: 0; }
.card.sub + .card.sub { margin-top: 22px; }
#quick-progress:empty { display: none; }
#quick-progress .gate { margin: 8px 0 0; }

/* ---- 账号与用量面板 ---- */
.acct-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }
.acct-box { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; min-width: 0; }
.acct-box .who { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.acct-box .who strong { font-size: 15px; font-weight: 500; word-break: break-all; }
.acct-box .uid { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--muted); }
.acct-stats { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 10px; }
.acct-stats div { font-size: 12px; color: var(--muted); }
.acct-stats b { display: block; font-size: 16px; font-weight: 500; color: var(--text);
                font-variant-numeric: tabular-nums; }
.tag { display: inline-block; border-radius: 5px; padding: 1px 6px; font-size: 11px;
       background: var(--code); color: var(--muted); margin-right: 4px; }
.tag.on { background: var(--accent-soft); color: var(--accent); }
table.acct td.mono, table.acct th.mono { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
table.acct tr.cur td { background: var(--accent-soft); }
table.acct td .dim { color: var(--faint); }
.chart { display: flex; align-items: flex-end; gap: 3px; height: 110px; margin: 10px 0 4px; }
.chart .col { flex: 1 1 0; display: flex; align-items: flex-end; gap: 1px; height: 100%;
              min-width: 0; }
.chart .col .bar { flex: 1 1 0; border-radius: 2px 2px 0 0; min-height: 2px; }
.chart .col .bar.a { background: var(--accent); }
.chart .col .bar.b { background: var(--ok); }
.chart .col.empty .bar { background: var(--line); min-height: 2px; }
.chart-x { display: flex; gap: 3px; font-size: 10px; color: var(--faint); }
.chart-x span { flex: 1 1 0; text-align: center; min-width: 0; overflow: hidden; white-space: nowrap; }
.legend { display: flex; gap: 14px; font-size: 12px; color: var(--muted); margin-top: 6px; }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; }
.notice { border-radius: 8px; padding: 10px 12px; font-size: 12.5px; margin-top: 12px;
          background: var(--warn-soft); color: var(--warn); }
/* 桌面首页：账号是主操作，历史数据与同步作为二级页面。 */
:root { color-scheme:light; --bg:#f5f6fa; --panel:#fff; --line:#e8eaf2;
  --text:#20243a; --muted:#7b8397; --faint:#a0a6b7; --accent:#655af5;
  --accent-soft:#efedff; --ok:#1d9e75; --ok-soft:#e1f5ee;
  --warn:#ba7517; --warn-soft:#faeeda; --bad:#a32d2d; --bad-soft:#fcebeb; --code:#f1f2f6; }
body { background: #f5f6fa; color: #20243a; font-size: 14px; }
.wrap { max-width: 1000px; padding: 30px 32px 64px; }
.app-head { display:flex; align-items:center; justify-content:space-between; gap:20px; margin-bottom:30px; }
.brand { display:flex; align-items:center; gap:13px; }
.brand-mark { width:42px; height:42px; display:grid; place-items:center; border-radius:13px;
  color:white; font-size:22px; font-weight:700; background:linear-gradient(140deg,#635bff,#9a6cff);
  box-shadow:0 8px 22px #635bff38; }
.brand h1 { margin:0; font-size:17px; font-weight:700; letter-spacing:.01em; }
.brand small { color:#8990a5; font-size:11px; }
.nav { display:flex; gap:4px; padding:4px; background:#e9ebf2; border-radius:11px; }
.nav button { border:0; background:transparent; color:#737b91; padding:8px 17px; border-radius:8px; }
.nav button.active { background:#fff; color:#343a5d; box-shadow:0 2px 8px #24284615; }
.page-kicker { color:#6659dd; font-size:12px; font-weight:700; letter-spacing:.1em; }
.page-title { font-size:29px; font-weight:750; letter-spacing:-.035em; margin:5px 0 3px; }
.page-subtitle { color:#81899c; margin:0 0 25px; }
.card { border-color:#e8eaf2; border-radius:17px; box-shadow:0 10px 36px #2730640a; }
.switch-panel { padding:24px; }
.switch-panel h2 { font-size:17px; font-weight:700; margin:0; }
.switch-panel .panel-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:18px; }
.panel-count { color:#81899c; font-size:12px; }
.switch-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
.switch-card { min-width:0; border:1px solid #e8eaf2; background:#fff; border-radius:14px;
  padding:18px; display:flex; flex-direction:column; gap:14px; transition:box-shadow .15s,border-color .15s; }
.switch-card:hover { border-color:#aaa4f9; box-shadow:0 7px 24px #635bff14; }
.switch-card.current { border-color:#a9a1ff; background:linear-gradient(145deg,#fbfaff,#f6f4ff); }
.switch-top { display:flex; gap:12px; align-items:center; min-width:0; }
.account-avatar { flex:none; width:42px; height:42px; border-radius:12px; display:grid; place-items:center;
  color:#fff; font-size:16px; font-weight:700; background:#7166d9; }
.switch-card:nth-child(2) .account-avatar { background:#48a3a5; }
.switch-card:nth-child(3) .account-avatar { background:#ec9a6c; }
.switch-card:nth-child(4) .account-avatar { background:#668cdd; }
.switch-ident { min-width:0; flex:1; }
.switch-name { display:block; font-size:16px; font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.switch-id { display:block; color:#a0a6b7; font-size:11px; margin-top:2px; }
.switch-foot { display:flex; justify-content:space-between; align-items:center; gap:10px; }
.switch-foot .current-label { color:#6256d8; background:#eceaff; border-radius:100px; padding:5px 10px; font-size:12px; }
.switch-foot .saved-label { color:#a0a6b7; font-size:12px; }
.switch-one { background:#655af5; border:0; color:#fff; border-radius:9px; padding:8px 15px; }
.switch-one:hover { background:#5144e7; }
.history-toggle { margin-top:18px; }
.history-toggle > summary { cursor:pointer; color:#5b55ba; font-weight:600; list-style:none; padding:10px 0; }
.history-toggle > summary::-webkit-details-marker { display:none; }
.history-toggle > summary::after { content:'  ›'; }
.history-toggle[open] > summary::after { content:'  ⌄'; }
.history-content { padding-top:10px; }
.tab-page[hidden] { display:none !important; }
@media(max-width:680px) { .wrap{padding:18px 16px 50px} .app-head{align-items:flex-start; flex-direction:column}
  .switch-grid{grid-template-columns:1fr} .page-title{font-size:25px} }
</style>
</head>
<body>
<div class="wrap">
  <header class="app-head"><div class="brand"><span class="brand-mark">W</span>
    <div><h1>WorkBuddy 账号管理</h1><small>本机账号与历史记录</small></div></div>
    <nav class="nav"><button class="active" data-tab="accounts">账号</button><button data-tab="tools">数据同步</button></nav>
  </header>

  <section id="tab-accounts" class="tab-page">
  <div class="page-kicker">ACCOUNT MANAGER</div>
  <h2 class="page-title">你的账号，一处管理</h2>
  <p class="page-subtitle">点击切换账号，WorkBuddy 会自动重启；本机历史会话继续保留。</p>

  <div class="card switch-panel">
    <div class="panel-head"><h2>我的账号</h2><span class="panel-count" id="switch-count"></span></div>
    <div id="switch-body" class="hint">正在读取可切换账号…</div>
    <div class="hint" id="switch-status"></div>
  </div>
  <div class="card">
    <h2>历史记录与用量</h2>
    <div class="hint">查看两个客户端保存的会话、累计消耗和账号记录。</div>
    <details class="history-toggle"><summary>展开详细数据</summary><div class="history-content">
    <div id="accounts-body" class="hint">正在读取本机账号…</div>
    <div class="actions">
      <button id="btn-accounts">重新读取</button>
      <span class="hint" id="accounts-status"></span>
    </div>
    </div></details>
  </div>
  </section>

  <section id="tab-tools" class="tab-page" hidden>
  <div class="page-kicker">DATA SYNC</div>
  <h2 class="page-title">数据同步</h2>
  <p class="page-subtitle">在 WorkBuddy 与 WorkBuddy AI 之间备份、迁移和核验历史记录。</p>

  <div class="card">
    <h2>一键同步</h2>
    <div id="gate" class="gate warn">正在检查客户端进程…</div>
    <div class="row" id="clients"></div>

    <div class="sep"></div>
    <div id="options"></div>

    <div class="sep"></div>
    <label class="opt">
      <input type="checkbox" id="quick-quit" checked>
      <span><span class="name">需要时自动退出两个客户端</span>
      <span class="why">按顺序做：退出客户端 → 盘点 → 计划 → 备份 → 执行 → 核验。
      先退出再建计划，指纹才不会因为客户端退出时落盘而漂移。不勾选则要求你自己先退干净。</span></span>
    </label>
    <label class="opt">
      <input type="checkbox" id="quick-force">
      <span><span class="name risk">25 秒没退干净就强制结束进程</span>
      <span class="why">客户端来不及落盘，可能丢未保存的状态。默认关闭。</span></span>
    </label>
    <label class="opt">
      <input type="checkbox" id="quick-backup" checked>
      <span><span class="name">执行前自动备份</span>
      <span class="why">备份到状态目录，可回滚。空间不足会中止，不会硬写。</span></span>
    </label>

    <div class="actions">
      <button id="btn-quick" class="primary big">开始同步</button>
    </div>
    <div id="quick-progress"></div>
    <p class="hint" id="platform"></p>
  </div>

  <div class="card">
    <h2>自动同步</h2>
    <div id="autosync" class="hint">正在读取自动同步状态…</div>
  </div>

  <div class="card">
    <h2>执行日志</h2>
    <pre id="log" style="max-height:420px">等待执行…</pre>
  </div>

  <details class="card" id="manual">
    <summary>手动模式（高级）· 分步执行 / 备份 / 核验 / 回滚</summary>
    <div class="sep"></div>

  <div class="card sub">
    <h2>1 · 盘点</h2>
    <div id="survey" class="hint">点击下方按钮读取两个数据目录的现状（只读操作）。</div>
    <div class="actions"><button id="btn-survey">读取盘点</button></div>
  </div>

  <div class="card sub">
    <h2>2 · 迁移范围</h2>
    <p class="hint">范围就是上面一键同步卡片里勾选的那些选项，改完直接生成计划。</p>
    <div class="actions">
      <button id="btn-plan" class="primary">生成计划</button>
      <span class="hint" id="plan-status"></span>
    </div>
  </div>

  <div class="card sub hidden" id="plan-card">
    <h2>3 · 计划审阅</h2>
    <div id="plan-summary"></div>
    <div class="sep"></div>
    <div class="kv"><span>plan_id</span><span></span></div>
    <div class="code-id" id="plan-id"></div>
    <p class="hint">plan_id 只是给你在命令行里复现同一份计划用的，界面里不需要手输，也不要用它做确认。</p>
    <div class="sep"></div>
    <div id="plan-detail"></div>
  </div>

  <div class="card sub hidden" id="run-card">
    <h2>4 · 执行</h2>
    <div id="run-gate" class="gate bad">需要先退出两个客户端</div>
    <div class="actions">
      <button id="btn-apply" class="danger">执行迁移</button>
      <button id="btn-backup">先做备份</button>
      <button id="btn-verify">核验</button>
    </div>
    <p class="hint">备份约需数百 MB 空间，默认排除 app / logs / traces 等与迁移无关的大目录。
    执行与备份的实时输出在上面「执行日志」里。</p>
  </div>

  <div class="card sub hidden" id="restore-card">
    <h2>5 · 回滚</h2>
    <p class="hint">回滚只撤销本工具新建的会话行与文件，不会恢复被合并改写的记忆原文（已留 .before-bridge-* 备份）。</p>
    <div class="actions">
      <button id="btn-restore">回滚本次执行</button>
      <span class="hint" id="restore-status"></span>
    </div>
  </div>

  </details>
  </section>
</div>

<script>
const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = (id) => document.getElementById(id);
document.querySelectorAll('.nav button').forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll('.nav button').forEach((item) => item.classList.toggle('active', item === button));
    document.querySelectorAll('.tab-page').forEach((page) => { page.hidden = page.id !== 'tab-' + button.dataset.tab; });
  };
});

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
  if (!res.ok) {
    const err = new Error(data.error || ('HTTP ' + res.status));
    err.status = res.status;   // 调用方要靠状态码区分"不支持"和"真出错"
    throw err;
  }
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
// 一键同步跑起来之后，2 秒一次的 refreshState 不能把按钮重新点亮。
let quickBusy = false;

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
  const autoQuit = $('quick-quit').checked;
  if (s.all_stopped) {
    gate.className = 'gate ok';
    gate.textContent = '两个客户端都已退出，可以执行写操作。';
  } else if (autoQuit) {
    gate.className = 'gate warn';
    gate.textContent = s.running_names.join('、') + ' 正在运行。开始同步时会先请求退出它们，等进程消失后再写入。';
  } else {
    gate.className = 'gate bad';
    gate.textContent = s.running_names.join('、') + ' 仍在运行，写操作会被拒绝。'
      + '勾选「需要时自动退出两个客户端」，或自己先退干净。';
  }
  renderAutosync(s.autosync);
  updateRunGate();
  const canWrite = s.all_stopped || autoQuit;
  $('btn-apply').disabled = quickBusy || !s.has_plan || !canWrite;
  $('btn-backup').disabled = quickBusy || !canWrite;
  $('btn-restore').disabled = quickBusy;
  $('btn-quick').disabled = quickBusy;
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
    el.textContent = '客户端已退出，可以直接执行。';
  } else if ($('quick-quit').checked) {
    el.className = 'gate warn';
    el.textContent = lastState.running_names.join('、') + ' 正在运行，点执行时会先自动退出它们。';
  } else {
    el.className = 'gate bad';
    el.textContent = lastState.running_names.join('、') + ' 仍在运行，执行按钮已锁定。';
  }
}

// -- 一键同步的进度输出 ---------------------------------------------------
// 步骤结果只在这种地方出现一次：面板上给结论，原始输出留给下面「执行日志」。
function prog(text, tone) {
  const box = $('quick-progress');
  const div = document.createElement('div');
  div.className = 'gate ' + (tone || 'warn');
  div.textContent = text;
  box.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function clearProg() {
  $('quick-progress').innerHTML = '';
}

// 保证两个客户端都已退出。autoQuit 勾着就替用户退出并等进程消失，
// 否则直接拒绝——不猜用户的意思，也不偷偷跳过这道闸。
async function ensureClientsStopped() {
  const st = await api('/api/state');
  if (st.all_stopped) return;
  const names = st.running_names.join('、');
  if (!$('quick-quit').checked) {
    throw new Error(names + ' 仍在运行。请先退出客户端，或勾选「需要时自动退出两个客户端」。');
  }
  if (!window.confirm(
      '即将请求退出：' + names + '\n\n'
      + '正在这些客户端里进行的会话和任务会中断。\n'
      + '本工具会先请求优雅退出，等进程真的消失后才开始写入。\n\n'
      + '继续？')) {
    throw new Error('已取消（未写入任何数据）。');
  }
  prog('正在请求退出 ' + names + '…');
  const r = await api('/api/quit-clients', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ allow_force: $('quick-force').checked }),
  });
  if (!r.ok) {
    throw new Error('仍有客户端在运行：' + r.remaining.map((x) => x.display).join('、')
      + '。已中止，未写入任何数据。');
  }
  prog('客户端已全部退出。' + (r.forced.length ? '（其中 ' + r.forced.length + ' 个是强制结束的）' : ''), 'ok');
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

// reveal=true 才滚动到审阅卡片（手动模式）；一键同步只借它填数据，不打断当前视图。
function renderPlan(plan, reveal = true) {
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
  if (reveal) {
    $('manual').open = true;
    $('plan-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  updateRunGate();
}

function directionNote(a, b) {
  const sa = (a.counts || {}).sessions || 0;
  const sb = (b.counts || {}).sessions || 0;
  if (sa === sb) return `自动判定方向：两边会话数相同（各 ${sa} 条）。`;
  const [main, other, hi, lo] = sa > sb ? [a, b, sa, sb] : [b, a, sb, sa];
  return `自动判定方向：${main.label} 更全（${hi} 条）对 ${other.label}（${lo} 条），`
    + `差额 ${hi - lo} 条是主要补充方向。本工具两边并集，不需要你选方向。`;
}

// -- 一键同步 ---------------------------------------------------------------
// 顺序是刻意的：先退出客户端再建计划。客户端退出时会 flush 自己的状态，
// 反过来先建计划再退客户端，指纹会漂移，执行时会被引擎拒绝。
$('btn-quick').onclick = async () => {
  if (quickBusy) return;
  const btn = $('btn-quick');
  quickBusy = true;
  btn.disabled = true; btn.textContent = '同步中…';
  clearProg();
  $('log').textContent = '';
  $('plan-status').textContent = '';
  $('restore-status').textContent = '';
  let applyCode = null;
  try {
    // 1 · 客户端
    await ensureClientsStopped();

    // 2 · 盘点
    const survey = await api('/api/survey');
    renderSurvey(survey);
    const [ha, hb] = survey.homes;
    prog(`盘点：${ha.label} 会话 ${(ha.counts || {}).sessions || 0} 条 · `
      + `${hb.label} 会话 ${(hb.counts || {}).sessions || 0} 条`, 'ok');
    prog(directionNote(ha, hb));

    // 3 · 计划
    const plan = await api('/api/plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ options: collectOptions() }),
    });
    renderPlan(plan, false);
    $('restore-card').classList.remove('hidden');
    const t = (plan.summary || {}).totals || {};
    const sA = (plan.summary || {}).a2b || {};
    const sB = (plan.summary || {}).b2a || {};
    const todo = t.sessions_to_copy || 0;
    prog(`计划：待新增 ${todo} 条会话（左→右 ${sA.sessions_to_copy || 0} · `
      + `右→左 ${sB.sessions_to_copy || 0}），约 ${t.approx_human || '0B'}`, todo ? 'ok' : 'ok');
    if (!todo) {
      prog('两边已经一致，没有需要新增的内容。已停止，未写入任何数据。', 'ok');
      return;
    }

    // 4 · 备份
    if ($('quick-backup').checked) {
      prog('正在备份两个数据目录…');
      let ok = null;
      await stream('/api/backup', (ev) => {
        if (ev.type === 'log') appendLog(ev.text);
        if (ev.type === 'done') ok = ev.code === 0;
      });
      if (ok === false) prog('备份失败，已中止，未写入任何数据。', 'bad');
      if (ok === false) return;
      prog('备份完成。', 'ok');
    } else {
      prog('已跳过备份（勾选项未开）。', 'warn');
    }

    // 5 · 执行
    prog('正在执行迁移…');
    await stream('/api/apply?plan_id=' + encodeURIComponent(plan.plan_id), (ev) => {
      if (ev.type === 'log') appendLog(ev.text);
      if (ev.type === 'done') applyCode = ev.code;
    });
    if (applyCode !== 0) {
      prog(`迁移未成功（退出码 ${applyCode}）。日志在上方「执行日志」里，可回滚。`, 'bad');
      return;
    }
    prog('迁移完成。', 'ok');

    // 6 · 核验
    prog('正在核验…');
    const v = await api('/api/verify', { method: 'POST' });
    appendLog('\n--- 核验 ---\n' + (v.log || '(无输出)'));
    if (v.code === 0) {
      prog('核验通过：要新增的内容都已到位。重启两个客户端就能看到新会话。', 'ok');
    } else {
      prog(`核验未通过（退出码 ${v.code}）。详见执行日志，可用下方回滚撤销。`, 'bad');
    }
  } catch (e) {
    prog('已中止：' + e.message, 'bad');
  } finally {
    quickBusy = false;
    btn.disabled = false; btn.textContent = '开始同步';
    refreshState();
  }
};

// ---- 账号与用量面板 ----

const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmtNum = (n) => (n == null ? '-' : Number(n).toLocaleString('zh-CN'));
const fmtCredits = (n) => (n == null ? '-' : Number(n).toLocaleString('zh-CN',
  { maximumFractionDigits: 1 }));

function accountName(a) {
  if (!a) return '未识别';
  return a.nickname || a.uid_short;
}

// 一个 home 的当前账号摘要
function currentAccountBox(h) {
  const cur = (h.accounts || []).find((a) => a.is_current);
  const bits = [];
  if (cur) {
    if (cur.type) bits.push(cur.type);
    if (cur.edition) bits.push(cur.edition);
    if (cur.is_pro) bits.push('Pro');
    if (cur.enterprise_id) bits.push('企业');
  }
  const t = h.totals || {};
  return `<div class="acct-box">
    <div class="who">
      <strong>${esc(accountName(cur))}</strong>
      ${cur ? '<span class="pill ok"><i class="dot"></i>当前登录</span>'
            : '<span class="pill bad">未识别</span>'}
    </div>
    <div class="uid">${esc(h.name)} · ${esc(h.path)}</div>
    ${cur ? `<div class="kv"><span>uid</span><span class="mono">${esc(cur.uid)}</span></div>` : ''}
    ${bits.length ? `<div class="kv"><span>类型</span><span>${esc(bits.join(' / '))}</span></div>` : ''}
    ${cur ? `<div class="kv"><span>快照</span><span>${esc(h.snapshot_at)}（${esc(h.snapshot_age)}）</span></div>` : ''}
    <div class="acct-stats">
      <div><b>${fmtNum(t.sessions)}</b>会话</div>
      <div><b>${fmtNum(t.automations)}</b>自动化</div>
      <div><b>${fmtCredits(t.credits_used)}</b>累计积分</div>
      <div><b>${fmtNum(t.accounts)}</b>账号</div>
    </div>
    <div class="hint" style="margin-top:8px">以上都是<b>这个客户端内</b>的记录；
    两个客户端有重叠会话，合计数字见下方「整机口径」。</div>
  </div>`;
}

// 账号明细表：本机出现过的所有账号，含已切换走的
function accountTable(h) {
  const rows = (h.accounts || []).map((a) => {
    const tags = [];
    if (a.has_memory) tags.push('<span class="tag on">记忆</span>');
    if (a.has_connectors) tags.push('<span class="tag">连接器</span>');
    if (a.has_personal_storage) tags.push('<span class="tag">存储</span>');
    if (a.automations) tags.push(`<span class="tag">自动化 ${a.automations}</span>`);
    if (a.channels && a.channels.length) tags.push(`<span class="tag on">${esc(a.channels.join('/'))}</span>`);
    const name = a.nickname
      ? esc(a.nickname)
      : '<span class="dim">—</span>';
    return `<tr class="${a.is_current ? 'cur' : ''}">
      <td class="mono">${a.is_current ? '★ ' : ''}${esc(a.uid_short)}</td>
      <td>${name}</td>
      <td class="num">${fmtNum(a.sessions)}</td>
      <td class="num">${fmtCredits(a.credits_used)}</td>
      <td>${esc(a.last_session || '-')}</td>
      <td>${tags.join('') || '<span class="dim">—</span>'}</td>
    </tr>`;
  }).join('');
  const unnamed = (h.accounts || []).filter((a) => !a.nickname).length;
  const stale = (h.accounts || []).filter((a) => !a.is_current && a.sessions).length;
  return `<table class="acct">
    <thead><tr>
      <th class="mono">uid</th><th>账号</th>
      <th style="text-align:right">会话</th><th style="text-align:right">累计积分</th>
      <th>最后活动</th><th>本机资产</th>
    </tr></thead>
    <tbody>${rows || '<tr><td colspan="6" class="dim">没有读到账号</td></tr>'}</tbody>
  </table>
  ${stale ? `<div class="hint">有 ${stale} 个账号不是当前登录的，但名下的会话还留在本机。
    想让它们出现在当前账号下，用下面的「一键同步」（同一客户端内改归属那一档）。</div>` : ''}
  ${unnamed ? `<div class="hint">有 ${unnamed} 个账号昵称为「—」：客户端只给当前账号
  记昵称，切换走之后本机就不再留名字了，只能靠 uid 区分。</div>` : ''}`;
}

// 近 N 天用量柱状图：同一天两个 home 并排
function renderTrend(homes) {
  const series = (homes[0] || {}).trend || [];
  if (!series.length) return '';
  const values = series.map((_, i) => homes.reduce(
    (sum, h) => sum + (((h.trend || [])[i] || {}).credits || 0), 0));
  const max = Math.max(1, ...values);
  const cols = series.map((point, i) => {
    const bars = homes.map((h) => {
      const v = ((h.trend || [])[i] || {}).credits || 0;
      const cls = h.key === 'wb' ? 'a' : 'b';
      return v ? `<div class="bar ${cls}" style="height:${Math.max(2, v / max * 100).toFixed(1)}%"></div>`
               : `<div class="bar ${cls}" style="height:0"></div>`;
    }).join('');
    const tip = `${point.date}｜` + homes.map((h) => {
      const v = ((h.trend || [])[i] || {}).credits || 0;
      return `${h.name} ${fmtCredits(v)}`;
    }).join('　') + `｜合计 ${fmtCredits(values[i])}`;
    return `<div class="col ${values[i] ? '' : 'empty'}" title="${esc(tip)}">${bars}</div>`;
  }).join('');
  const label = series.map((point, i) => (
    `<span>${i % 5 === 0 || i === series.length - 1 ? point.date.slice(5) : ''}</span>`
  )).join('');
  return `<div class="chart">${cols}</div><div class="chart-x">${label}</div>
    <div class="legend">
      ${homes.map((h) => `<span><i style="background:var(${h.key === 'wb' ? '--accent' : '--ok'})"></i>${esc(h.name)}</span>`).join('')}
      <span>柱子是各客户端自己的记录，同一会话被同步到两边时当天会各计一次</span>
    </div>`;
}

// 跨客户端去重后的整机视角
function mergedTable(d) {
  const m = d.merged || {};
  const rows = (m.accounts || []).map((a) => `<tr>
      <td class="mono">${esc(a.uid_short)}</td>
      <td>${a.nickname ? esc(a.nickname) : '<span class="dim">昵称未留痕</span>'}</td>
      <td class="num">${fmtCredits(a.credits_used)}</td>
      <td class="num">${fmtNum(a.sessions)}</td>
      <td>${(a.homes || []).map((n) => `<span class="tag on">${esc(n)}</span>`).join('')}</td>
    </tr>`).join('');
  return `<div class="hint">同一个会话被同步到另一个客户端后会在两边各存一份
    （顺带把归属改写成目标账号）。直接相加会把同一笔消耗算两遍——
    本机有 <b>${fmtNum(m.duplicated_sessions)}</b> 条记录属于这种情况，
    所以下面这张表按 session 去重，才是整机的真实消耗。</div>
    <table class="acct" style="margin-top:10px">
      <thead><tr>
        <th class="mono">uid</th><th>账号</th>
        <th style="text-align:right">整机累计积分</th>
        <th style="text-align:right">会话</th>
        <th>出现于</th>
      </tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="dim">没有用量记录</td></tr>'}</tbody>
    </table>`;
}

function renderAccounts(d) {
  const homes = d.homes || [];
  const merged = d.merged || {};
  const days = d.days || 30;
  let html = `<div class="acct-grid">${homes.map(currentAccountBox).join('')}</div>`;
  html += `<div class="sep"></div>
    <div class="hint">整机去重后：累计积分消耗 <b>${fmtCredits(merged.credits_used)}</b>，
    会话 <b>${fmtNum(merged.sessions)}</b> 条，涉及 <b>${fmtNum((merged.accounts || []).length)}</b> 个账号。</div>`;
  homes.forEach((h) => {
    html += `<div class="sep"></div>
      <div class="host">${esc(h.name)} · 该客户端内的账号</div>
      ${accountTable(h)}
      ${(h.errors || []).map((e) => `<div class="hint" style="color:var(--warn)">! ${esc(e)}</div>`).join('')}`;
  });
  html += `<div class="sep"></div>
    <div class="host">整机口径（跨客户端去重）</div>
    ${mergedTable(d)}`;
  html += `<div class="sep"></div>
    <div class="host">近 ${days} 天积分消耗</div>
    ${renderTrend(homes) || '<div class="hint">没有读到用量记录。</div>'}
    <div class="notice"><b>关于「剩余积分」：</b>此面板目前显示本机记录的
    <b>已消耗</b>总量；实时剩余额度尚未接入此面板，请打开 WorkBuddy 账户页查看。</div>
    <div class="hint">读取时间 ${esc(d.generated_at)}${d.cached ? '（命中缓存）' : ''} ·
    全程只读，未写入任何客户端数据。</div>`;
  $('accounts-body').innerHTML = html;
}

async function loadAccounts(refresh) {
  const btn = $('btn-accounts');
  let unsupported = false;   // finally 里要用，不能只在 catch 里声明
  btn.disabled = true;
  $('accounts-status').textContent = '读取中…';
  try {
    const q = '/api/accounts' + (refresh ? '?refresh=1' : '');
    renderAccounts(await api(q));
    $('accounts-status').textContent = '';
    btn.textContent = '重新读取';
  } catch (e) {
    // Go 版目前没有这个端点（501/404）。这不是故障，别把用户吓一跳。
    unsupported = e.status === 501 || e.status === 404;
    $('accounts-body').innerHTML = unsupported
      ? `<div class="gate warn">当前这个实现还没有账号面板。
         Python 版（<span class="mono">python3 tools/wb_ui.py</span>）里可以看账号与用量。</div>`
      : '<div class="gate bad">读取失败：' + esc(e.message) + '</div>';
    $('accounts-status').textContent = '';
    btn.textContent = unsupported ? '重新读取' : '重试';
  } finally {
    btn.disabled = unsupported;
  }
}

$('btn-accounts').onclick = () => loadAccounts(true);

async function loadSwitchAccounts() {
  try {
    const data = await api('/api/switch-accounts');
    const accounts = data.accounts || [];
    $('switch-count').textContent = accounts.length + ' 个已保存账号';
    $('switch-body').innerHTML = accounts.length
      ? '<div class="switch-grid">' + accounts.map((a, i) => `<div class="switch-card ${a.current ? 'current' : ''}">
          <div class="switch-top"><span class="account-avatar">${String(i + 1).padStart(2, '0')}</span>
            <span class="switch-ident"><span class="switch-name">${esc(a.nickname || '未命名账号')}</span>
              <span class="switch-id">ID ${esc(a.uid.slice(0, 8))}</span></span></div>
          <div class="switch-foot"><span class="${a.current ? 'current-label' : 'saved-label'}">${a.current ? '● 当前使用' : '已保存登录状态'}</span>
            <button class="switch-one" data-uid="${esc(a.uid)}" ${a.current ? 'disabled' : ''}>${a.current ? '使用中' : '切换账号'}</button></div>
        </div>`).join('') + '</div>'
      : '<div class="hint">尚无可切换账号，请先登录 WorkBuddy。</div>';
    document.querySelectorAll('.switch-one').forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        $('switch-status').textContent = '正在保存当前账号并重启 WorkBuddy…';
        try {
          await api('/api/switch-account', {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({uid:btn.dataset.uid})});
          $('switch-status').textContent = '切换完成，历史会话仍保存在本机。';
          await loadSwitchAccounts();
          await loadAccounts(true);
        } catch (e) {
          $('switch-status').textContent = '切换失败：' + e.message;
          btn.disabled = false;
        }
      };
    });
  } catch (e) {
    if (e.status === 404 || e.status === 501) {
      document.querySelector('.nav [data-tab="accounts"]').hidden = true;
      document.querySelector('.nav [data-tab="tools"]').click();
    } else {
      $('switch-body').textContent = '账号读取失败：' + e.message;
    }
  }
}
loadSwitchAccounts();

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
    $('log').textContent = '等待执行…';
    $('restore-card').classList.remove('hidden');
  } catch (e) {
    $('plan-status').innerHTML = '<span style="color:var(--bad)">' + e.message + '</span>';
  } finally { btn.disabled = false; }
};

$('btn-apply').onclick = async () => {
  const btn = $('btn-apply');
  if (!window.confirm('确认执行迁移？\n\n这会向两个数据目录写入数据。\n完成后需要重启两个客户端才能看到新会话。')) return;
  btn.disabled = true; btn.textContent = '执行中…';
  $('log').textContent = '';
  try {
    await ensureClientsStopped();
    await stream('/api/apply?plan_id=' + encodeURIComponent($('plan-id').textContent), (ev) => {
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
  if (!window.confirm('开始备份两个数据目录？\n\n默认排除 app / logs / traces 等大目录。')) return;
  const btn = $('btn-backup');
  btn.disabled = true; btn.textContent = '备份中…';
  $('log').textContent = '';
  try {
    await ensureClientsStopped();
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
  if (!$('plan-id').textContent.trim()) { alert('没有可回滚的计划'); return; }
  if (!window.confirm('回滚会删除本工具新建的会话行与文件。\n\n'
      + '被合并改写的记忆原文不会自动恢复（已留 .before-bridge-* 备份）。\n\n继续？')) return;
  const btn = $('btn-restore');
  btn.disabled = true; $('restore-status').textContent = '回滚中…';
  try {
    // 服务端自己知道本会话的 plan_id，不需要用户手打确认串。
    const r = await api('/api/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
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

// 勾选状态一变就重画闸门文案，别等下一次 2 秒轮询。
['quick-quit', 'quick-force', 'quick-backup'].forEach((id) => {
  $(id).addEventListener('change', () => { if (lastState) refreshState(); });
});

renderOptions();
refreshState();
setInterval(refreshState, 2000);
// 账号面板要开库 + 采样日志，约 1 秒，异步跑，不挡页面其余部分
loadAccounts(false);
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


def open_window(url: str, title: str = "WorkBuddy 账号管理") -> bool:
    """用系统自带的 WebView 开一个**属于本应用自己的窗口**。

    界面仍然是那个本地页面，但不再往外跳浏览器：macOS 走 WKWebView、
    Windows 走 WebView2，都是系统组件，不用额外装运行时。

    必须在**主线程**调用——Cocoa 的窗口只能在主线程创建，所以 HTTP 服务
    要挪到后台线程去跑（见 ``main``）。

    返回 ``True``：窗口确实开起来了（函数阻塞到用户关掉它）。
    返回 ``False``：**没能开窗**，调用方应退回浏览器。
    两种情况要分清楚——否则建窗失败时用户会对着空气发呆。
    """
    try:
        import webview
    except Exception:
        return False
    try:
        webview.create_window(title, url, width=1120, height=800,
                              min_size=(880, 600))
    except Exception:
        return False

    # 窗口真起来了才会回调。某些环境（没有 GUI 会话、远程 shell）下
    # ``start()`` 会二话不说直接返回——那**不能**当成"窗口正常关闭"，
    # 否则用户看到的就是一闪而过、或者干脆什么都没有，又回到
    # "双击没反应"。确认没起来就返回 False，让调用方退回浏览器。
    shown: list[bool] = []
    try:
        webview.start(lambda *a: shown.append(True))
    except Exception:
        pass
    return bool(shown)


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
    parser.add_argument("--window", action="store_true",
                        help="用应用自己的窗口显示界面（需要 pywebview）；不可用时退回浏览器")
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

    if args.window:
        # 窗口只能在主线程建（Cocoa 的硬性要求），HTTP 服务挪到后台线程。
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        if open_window(url):
            print("窗口已关闭。")
            httpd.server_close()
            return 0
        # 开窗失败：别让用户对着空气发呆，退回浏览器。
        print("原生窗口不可用，改用浏览器打开。")
        if not args.no_open:
            _startup_open(url)
        try:
            threading.Event().wait()  # 服务已在后台线程跑，主线程挂着等 Ctrl-C
        except KeyboardInterrupt:
            print("\n已停止。")
        finally:
            httpd.server_close()
        return 0

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
