#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb-home-bridge — 跨 App 数据目录（home）双向打通工具。

用途：让 ``/Applications/WorkBuddy.app``（home ``~/.workbuddy``）与
``/Applications/WorkBuddy AI.app``（home ``~/.workbuddy-ai``）两个相互独立的
客户端**互相看到对方的全部历史会话**，并共享长期记忆与技能。

本机实测前提（2026-09-16）：

* 两个 App 独立程序、独立数据目录、零共享通道。
* 两边数据库表结构同构，``sessions`` 列完全一致，无触发器、无 FTS。
* 会话正文不在数据库里，而在 ``projects/<cwd-slug>/<conversationId>.jsonl``。
* ``sessions.id`` == ``projects/<slug>/<id>.jsonl`` 的文件名（实测 82/84、37/37 命中）。
* 两边同名项目桶中同 id 重叠为 0 → 目录合并天然不冲突。
* ``cwd`` 指向本机真实路径，与是哪个 App 创建的无关 → 不需要路径映射。
* ``traces/`` 按 workerPid 分桶，跨 App 复制无意义 → 不搬（省 ~470MB）。
* ``blobs/`` 内容寻址（hash 命名）→ 目录并集即可。

跨 home 与同 home 跨账号的本质区别：两个 SQLite 各自独立，同一 session id 可以在
两边各存一份、各自的 ``user_id`` 指向各自当前账号，所以**两边都保留是可行的**；
而同 home 内一条会话只有一个 ``user_id``，只能搬走。

安全约束：

* 只 INSERT 目标库中不存在的行，绝不 UPDATE / DELETE 目标已有行。
* 文件只新增，已存在即跳过（``--overwrite-assets`` 可覆盖）。
* 自动化默认不复制（复制会导致同一任务在两个 App 各跑一遍 → 重复投递）。
* 连接器凭据默认不复制（凭据用各自 home 的 ``.master.key`` 加密，跨 home 无法解密）。
* 需要两个 App 都完全退出后才允许写入。

仅标准库，Python 3.10+。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import wb_platform  # noqa: E402  同目录模块，需先注入 sys.path

VERSION = "0.1.0"

DB_NAME = "workbuddy.db"
ACCOUNT_SNAPSHOT = os.path.join("storage", "skeleton", "account-snapshot.json")

# 按会话 id 归档的内容资产
PER_SESSION_TREES = (
    ("tasks/{cid}", False),
    ("changes-detail/{cid}", True),
    ("changes-index/{cid}", True),
    ("file-history/{cid}", True),
)
PER_SESSION_FILES = ("artifact-index/{cid}.json",)
PROJECT_SESSION_SUFFIXES = (".jsonl", ".meta.json", ".file-rollback.ndjson")

# 跨 home 直接并集的目录（内容寻址或纯新增）
BASE_UNION_TREES = ("blobs",)
OPTIONAL_UNION_TREES = {"skills": "include_skills", "plugins/cache": "include_plugins"}

# 每个方向的指纹 & 行传输覆盖的表
DB_SESSION_TABLES = ("sessions", "session_usage")
DB_SIDE_TABLES = ("workspaces", "buddy_snapshots")
AUTOMATION_TABLES = (
    "automations",
    "automation_runs",
    "automation_runtime_state",
)

CONNECTOR_SHARED_FILES = ("connector-states.json", "mcp.json")


class BridgeError(Exception):
    """受控失败：参数错误、安全检查拒绝、状态漂移。"""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def dir_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def tree_manifest(path: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if not os.path.isdir(path):
        return out
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            try:
                out[os.path.relpath(full, path)] = os.stat(full).st_size
            except OSError:
                continue
    return out


# --------------------------------------------------------------------------
# 进程 / 环境检查
# --------------------------------------------------------------------------


def running_client_processes() -> list[tuple[int, str]]:
    """返回正在运行的两个 WorkBuddy 客户端进程。

    探测方式按平台分流（见 ``wb_platform``）：macOS 主进程的可执行文件名是
    ``Electron``、Windows 是 ``WorkBuddy.exe``，按进程名一刀切会静默失效。
    """
    try:
        statuses = wb_platform.client_statuses()
    except wb_platform.PlatformError as exc:  # pragma: no cover
        raise BridgeError(f"无法检查客户端进程：{exc}") from exc

    hits: list[tuple[int, str]] = []
    for item in statuses:
        for proc in item["processes"]:
            hits.append((proc["pid"], proc["cmd"]))
    return hits


def require_clients_stopped(allow_running: bool) -> None:
    hits = running_client_processes()
    if not hits:
        return
    detail = "\n".join(f"  pid {pid}  {cmd[:110]}" for pid, cmd in hits[:8])
    if allow_running:
        eprint("警告：检测到客户端仍在运行，已按 --allow-client-running 继续。")
        eprint(detail)
        eprint("新写入的数据要等客户端重启后才会出现在界面上。")
        return
    raise BridgeError(
        "检测到 WorkBuddy 客户端仍在运行，拒绝写入：\n"
        f"{detail}\n\n"
        f"请依次执行：\n{wb_platform.stop_instructions()}\n"
        "若确实需要在客户端运行时执行，请显式追加 --allow-client-running。"
    )


# --------------------------------------------------------------------------
# home 描述
# --------------------------------------------------------------------------


@dataclass
class Home:
    label: str
    slug: str
    path: str
    app: str

    @property
    def db_path(self) -> str:
        return os.path.join(self.path, DB_NAME)

    @property
    def snapshot_path(self) -> str:
        return os.path.join(self.path, ACCOUNT_SNAPSHOT)

    def require_valid(self) -> None:
        if not os.path.isdir(self.path):
            raise BridgeError(f"[{self.label}] 数据目录不存在：{self.path}")
        if not os.path.isfile(self.db_path):
            raise BridgeError(f"[{self.label}] 缺少数据库：{self.db_path}")

    def _snapshot(self) -> dict[str, Any]:
        if not os.path.isfile(self.snapshot_path):
            raise BridgeError(
                f"[{self.label}] 缺少账号快照：{self.snapshot_path}\n"
                "请先启动该客户端并完成登录，再运行本工具。"
            )
        try:
            with open(self.snapshot_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as exc:
            raise BridgeError(f"[{self.label}] 无法解析账号快照：{exc}") from exc

    def current_uid(self) -> str:
        uid = (self._snapshot().get("primary") or {}).get("uid")
        if not uid:
            raise BridgeError(f"[{self.label}] 账号快照中没有 primary.uid")
        return uid

    def nickname(self) -> str:
        try:
            return (self._snapshot().get("primary") or {}).get("nickname") or ""
        except BridgeError:
            return ""

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=60.0)
        con.execute("PRAGMA busy_timeout=60000")
        con.row_factory = sqlite3.Row
        return con


def app_label(spec: wb_platform.ClientSpec) -> str:
    """客户端可执行文件标识，按平台给出，仅用于展示。"""
    if wb_platform.IS_WIN:
        return spec.win_image
    if wb_platform.IS_MAC:
        return spec.mac_app
    return spec.linux_image


def resolve_home(explicit: str | None, spec: wb_platform.ClientSpec, flag: str) -> str:
    """显式指定优先；否则按平台探测。探测不到时提示用户显式指定。"""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    path, confirmed = wb_platform.client_home(spec)
    if not confirmed:
        eprint(f"提示：未能确认 {spec.display} 的数据目录，暂用 {path}。"
               f"若该路径不对，请显式指定 {flag}。")
    return path


def make_home(spec: wb_platform.ClientSpec, explicit: str | None = None) -> Home:
    """按平台规范构造一个 Home：显式路径优先，否则自动探测。"""
    flag = "--home-a" if spec.key == "wb" else "--home-b"
    return Home(spec.display, spec.key, resolve_home(explicit, spec, flag), app_label(spec))


def build_homes(args: argparse.Namespace) -> tuple[Home, Home]:
    a = make_home(wb_platform.CLIENTS_BY_KEY["wb"], args.home_a)
    b = make_home(wb_platform.CLIENTS_BY_KEY["wb_ai"], args.home_b)
    if a.path == b.path:
        raise BridgeError("两个 home 不能是同一个目录。")
    if a.path.startswith(b.path + os.sep) or b.path.startswith(a.path + os.sep):
        raise BridgeError("两个 home 不能互相嵌套。")
    return a, b


# --------------------------------------------------------------------------
# 只读盘点
# --------------------------------------------------------------------------


def table_count(con: sqlite3.Connection, table: str) -> int | None:
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except sqlite3.OperationalError:
        return None


def project_slug_for(home: Home, cid: str) -> list[str]:
    base = os.path.join(home.path, "projects")
    if not os.path.isdir(base):
        return []
    return [
        slug
        for slug in sorted(os.listdir(base))
        if os.path.isfile(os.path.join(base, slug, cid + ".jsonl"))
    ]


def conversations_missing_db_row(home: Home) -> list[str]:
    base = os.path.join(home.path, "projects")
    if not os.path.isdir(base):
        return []
    con = home.connect()
    try:
        known = {r[0] for r in con.execute("SELECT id FROM sessions")}
    finally:
        con.close()
    orphans = []
    for slug in os.listdir(base):
        d = os.path.join(base, slug)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.endswith(".jsonl") and name[:-6] not in known:
                orphans.append(name[:-6])
    return sorted(set(orphans))


def skills_of(home: Home) -> set[str]:
    base = os.path.join(home.path, "skills")
    if not os.path.isdir(base):
        return set()
    return {
        n for n in os.listdir(base)
        if not n.startswith(".") and os.path.isdir(os.path.join(base, n))
    }


def survey(home: Home) -> dict[str, Any]:
    home.require_valid()
    info: dict[str, Any] = {
        "label": home.label,
        "app": home.app,
        "path": home.path,
        "uid": None,
        "nickname": "",
        "counts": {},
        "sizes": {},
        "invalid_cwd": [],
        "warnings": [],
    }
    try:
        info["uid"] = home.current_uid()
        info["nickname"] = home.nickname()
    except BridgeError as exc:
        info["warnings"].append(str(exc))

    con = home.connect()
    try:
        for table in DB_SESSION_TABLES + DB_SIDE_TABLES + AUTOMATION_TABLES:
            n = table_count(con, table)
            if n is not None:
                info["counts"][table] = n
        by_user: dict[str, int] = {}
        bad_cwd: dict[str, int] = {}
        for row in con.execute("SELECT cwd, user_id FROM sessions WHERE deleted_at IS NULL"):
            by_user[row["user_id"]] = by_user.get(row["user_id"], 0) + 1
            if not os.path.isdir(row["cwd"]):
                bad_cwd[row["cwd"]] = bad_cwd.get(row["cwd"], 0) + 1
        info["sessions_by_user"] = by_user
        info["invalid_cwd"] = sorted(bad_cwd.items(), key=lambda kv: -kv[1])
    finally:
        con.close()

    for name in ("projects", "tasks", "traces", "blobs", "changes-detail", "changes-index",
                 "file-history", "artifact-index", "memory", "skills", "connectors", "storage"):
        info["sizes"][name] = dir_size(os.path.join(home.path, name))

    base = os.path.join(home.path, "projects")
    if os.path.isdir(base):
        slugs = [s for s in os.listdir(base) if os.path.isdir(os.path.join(base, s))]
        convs = sum(
            1 for s in slugs for f in os.listdir(os.path.join(base, s)) if f.endswith(".jsonl")
        )
        info["counts"]["projects_buckets"] = len(slugs)
        info["counts"]["conversations"] = convs

    orphans = conversations_missing_db_row(home)
    if orphans:
        info["orphan_conversations"] = orphans
        info["warnings"].append(
            f"{len(orphans)} 段对话有正文文件但数据库无对应会话行，本工具不处理。"
        )
    info["skills"] = sorted(skills_of(home))
    return info


def print_survey(info: dict[str, Any]) -> None:
    print(f"=== {info['label']}  ({info['app']})")
    print(f"  数据目录 : {info['path']}")
    print(f"  当前账号 : {info['uid']}  {info['nickname']}")
    c = info.get("counts", {})
    print(f"  会话     : {c.get('sessions', 0)}   对话正文 : {c.get('conversations', 0)}"
          f"   项目桶 : {c.get('projects_buckets', 0)}")
    print(f"  自动化   : {c.get('automations', 0)}")
    by_user = info.get("sessions_by_user") or {}
    if len(by_user) > 1:
        print("  按账号   : " + "  ".join(f"{u[:8]}={n}" for u, n in sorted(by_user.items())))
    sizes = info.get("sizes", {})
    print("  体积     : " + "  ".join(f"{k}={human(v)}" for k, v in sizes.items() if v))
    sk = info.get("skills") or []
    if sk:
        print(f"  技能     : {len(sk)} 个")
    for cwd, n in (info.get("invalid_cwd") or [])[:8]:
        print(f"  ! cwd 已不存在: {cwd}  ({n} 条会话)")
    for warn in info.get("warnings") or []:
        print(f"  ! {warn}")


# --------------------------------------------------------------------------
# 计划
# --------------------------------------------------------------------------


@dataclass
class PlanEntry:
    kind: str          # file | tree
    src: str
    dst: str
    mode: str          # copy_if_missing | copy_tree_if_missing | merge_tree
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = {"kind": self.kind, "mode": self.mode, "src": self.src, "dst": self.dst}
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class Plan:
    version: str
    home_a: str
    home_b: str
    uid_a: str
    uid_b: str
    created_at: str
    options: dict[str, bool]
    rows: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)
    skipped: dict[str, dict[str, int]] = field(default_factory=dict)
    fingerprints: dict[str, dict[str, str]] = field(default_factory=dict)
    entries: list[PlanEntry] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    plan_id: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "home_a": self.home_a,
            "home_b": self.home_b,
            "uid_a": self.uid_a,
            "uid_b": self.uid_b,
            "options": self.options,
            "source_fingerprints": self.fingerprints,
            "entries_fingerprint": sha256_text(
                json.dumps([e.as_dict() for e in self.entries], ensure_ascii=False, sort_keys=True)
            ),
        }

    def finalize(self) -> None:
        self.plan_id = sha256_text(
            json.dumps(self.body(), ensure_ascii=False, sort_keys=True)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "created_at": self.created_at,
            "homes": {
                "a": {"label": self.home_a, "path": self.home_a, "uid": self.uid_a},
                "b": {"label": self.home_b, "path": self.home_b, "uid": self.uid_b},
            },
            "options": self.options,
            "summary": self.summary,
            "source_fingerprints": self.fingerprints,
            "rows": self.rows,
            "skipped": self.skipped,
            "entries": [e.as_dict() for e in self.entries],
        }


def _target_columns(con: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.OperationalError:
        return []


def collect_rows(
    src: Home,
    dst: Home,
    dst_uid: str,
    options: dict[str, bool],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int], dict[str, str]]:
    """收集要 INSERT 到 dst 的行 + 源侧完整指纹 + 跳过计数。"""
    rows: dict[str, list[dict[str, Any]]] = {}
    skipped: dict[str, int] = {}
    fingerprints: dict[str, str] = {}

    src_con, dst_con = src.connect(), dst.connect()
    try:
        src_sessions = {
            r["id"]: dict(r)
            for r in src_con.execute("SELECT * FROM sessions WHERE deleted_at IS NULL")
        }
        fingerprints["sessions_all"] = sha256_text(
            json.dumps(src_sessions, ensure_ascii=False, sort_keys=True)
        )
        dst_ids = {r[0] for r in dst_con.execute("SELECT id FROM sessions")}
        to_copy = {sid: r for sid, r in src_sessions.items() if sid not in dst_ids}
        skipped["sessions"] = len(src_sessions) - len(to_copy)
        rows["sessions"] = []
        for sid, row in sorted(to_copy.items()):
            row = dict(row)
            row["user_id"] = dst_uid
            rows["sessions"].append(row)
        copied = set(to_copy)

        # session_usage 跟随
        dst_usage = {r[0] for r in dst_con.execute("SELECT session_id FROM session_usage")}
        rows["session_usage"] = [
            dict(r)
            for r in src_con.execute("SELECT * FROM session_usage")
            if r["session_id"] in copied and r["session_id"] not in dst_usage
        ]

        # workspaces 并集
        dst_ws = {r[0] for r in dst_con.execute("SELECT path FROM workspaces")}
        rows["workspaces"] = [
            dict(r)
            for r in src_con.execute("SELECT * FROM workspaces")
            if r["path"] not in dst_ws
        ]

        # buddy_snapshots：只搬被引用且目标缺失的（PK 是 snapshot_id，实测两库均为 0 行）
        rows["buddy_snapshots"] = []
        snap_cols = _target_columns(dst_con, "buddy_snapshots")
        if "snapshot_id" in snap_cols:
            dst_snap = {r[0] for r in dst_con.execute("SELECT snapshot_id FROM buddy_snapshots")}
            referenced = {
                r["buddy_snapshot_id"] for r in rows["sessions"] if r.get("buddy_snapshot_id")
            }
            if referenced:
                rows["buddy_snapshots"] = [
                    dict(r)
                    for r in src_con.execute("SELECT * FROM buddy_snapshots")
                    if r["snapshot_id"] in referenced and r["snapshot_id"] not in dst_snap
                ]

        if options.get("include_automations"):
            auto_all = [dict(r) for r in src_con.execute(
                "SELECT * FROM automations WHERE deleted_at IS NULL")]
            fingerprints["automations_all"] = sha256_text(
                json.dumps(auto_all, ensure_ascii=False, sort_keys=True))
            dst_auto = {r[0] for r in dst_con.execute("SELECT id FROM automations")}
            rows["automations"] = []
            for row in auto_all:
                if row["id"] in dst_auto:
                    continue
                row["owner_user_id"] = dst_uid
                row["owner_status"] = "confirmed"
                row["status"] = "PAUSED"  # 防两个 App 各跑一遍
                row["next_run_at"] = None
                rows["automations"].append(row)
            ids = {r["id"] for r in rows["automations"]}

            dst_runs = {r[0] for r in dst_con.execute("SELECT thread_id FROM automation_runs")}
            rows["automation_runs"] = [
                dict(r)
                for r in src_con.execute("SELECT * FROM automation_runs")
                if r["automation_id"] in ids and r["thread_id"] not in dst_runs
            ]
            dst_state = {
                r[0] for r in dst_con.execute("SELECT automation_id FROM automation_runtime_state")
            }
            rows["automation_runtime_state"] = []
            for r in src_con.execute("SELECT * FROM automation_runtime_state"):
                if r["automation_id"] in ids and r["automation_id"] not in dst_state:
                    row = dict(r)
                    row["running"] = 0
                    rows["automation_runtime_state"].append(row)

        # 裁剪到目标库实际存在的列
        cleaned: dict[str, list[dict[str, Any]]] = {}
        for table, table_rows in rows.items():
            cols = _target_columns(dst_con, table)
            if not cols:
                continue
            cleaned[table] = [{c: r[c] for c in cols if c in r} for r in table_rows]
        cleaned.setdefault("sessions", [])
        return cleaned, skipped, fingerprints
    finally:
        src_con.close()
        dst_con.close()


def build_direction_entries(
    src: Home,
    dst: Home,
    src_uid: str,
    dst_uid: str,
    session_ids: Iterable[str],
    options: dict[str, bool],
) -> tuple[list[PlanEntry], dict[str, Any]]:
    entries: list[PlanEntry] = []
    stats = {"sessions": 0, "missing_assets": 0, "cwd_invalid": 0}

    def add_file(src_rel: str, dst_rel: str, note: str) -> None:
        s = os.path.join(src.path, src_rel)
        if os.path.exists(s):
            entries.append(
                PlanEntry("file", s, os.path.join(dst.path, dst_rel), "copy_if_missing", note)
            )
        else:
            stats["missing_assets"] += 1

    def add_tree(src_rel: str, dst_rel: str, note: str) -> None:
        s = os.path.join(src.path, src_rel)
        if os.path.isdir(s):
            entries.append(
                PlanEntry("tree", s, os.path.join(dst.path, dst_rel),
                          "copy_tree_if_missing", note)
            )

    con = src.connect()
    try:
        meta = {
            r["id"]: {"cwd": r["cwd"]}
            for r in con.execute("SELECT id, cwd FROM sessions WHERE deleted_at IS NULL")
        }
    finally:
        con.close()

    for cid in sorted(set(session_ids)):
        info = meta.get(cid)
        if not info:
            continue
        stats["sessions"] += 1
        if not os.path.isdir(info["cwd"]):
            stats["cwd_invalid"] += 1

        for slug in project_slug_for(src, cid):
            for suffix in PROJECT_SESSION_SUFFIXES:
                add_file(os.path.join("projects", slug, cid + suffix),
                         os.path.join("projects", slug, cid + suffix), "conversation")
            add_tree(os.path.join("projects", slug, cid),
                     os.path.join("projects", slug, cid), "tool-results")

        for tmpl, needs_changes in PER_SESSION_TREES:
            if needs_changes and not options.get("include_changes"):
                continue
            rel = tmpl.format(cid=cid)
            add_tree(rel, rel, "session asset")
        for tmpl in PER_SESSION_FILES:
            rel = tmpl.format(cid=cid)
            add_file(rel, rel, "session asset")

    # 全局并集
    for name in BASE_UNION_TREES:
        add_tree(name, name, "content-addressed union")
    for name, flag in OPTIONAL_UNION_TREES.items():
        if options.get(flag):
            add_tree(name, name, "union")

    # 连接器技能定义：仅在显式要求共享连接器时合并（授权本身无法跨 home 转移）
    if options.get("include_connectors"):
        add_tree("connectors/skills", "connectors/skills", "connector skills")

    # 账号个人存储：源 uid 目录 → 目标 uid 目录
    if options.get("include_storage"):
        for suffix in ("", "-personal"):
            add_tree(f"storage/user-{src_uid}{suffix}", f"storage/user-{dst_uid}{suffix}",
                     "account storage (merge-if-missing)")

    # 连接器：只并状态与 mcp 配置，绝不搬 .master.key / 凭据
    if options.get("include_connectors"):
        for name in CONNECTOR_SHARED_FILES:
            add_file(os.path.join("connectors", src_uid, name),
                     os.path.join("connectors", dst_uid, name),
                     "connector state (no credentials)")

    return entries, stats


def build_plan(args: argparse.Namespace, a: Home, b: Home) -> Plan:
    a.require_valid()
    b.require_valid()
    uid_a, uid_b = a.current_uid(), b.current_uid()

    options = {
        "include_changes": not args.no_changes,
        "include_skills": not args.no_skills,
        "include_plugins": bool(args.include_plugins),
        "include_automations": bool(args.include_automations),
        "include_storage": bool(args.include_storage),
        "include_connectors": bool(args.include_connectors),
        "include_claw": not args.no_claw,
        "include_memory": not args.no_memory,
        "overwrite_assets": bool(args.overwrite_assets),
    }

    plan = Plan(
        version=VERSION,
        home_a=a.path,
        home_b=b.path,
        uid_a=uid_a,
        uid_b=uid_b,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        options=options,
    )

    approx = 0
    total_sessions = 0
    for side, src, dst, dst_uid in (("a2b", a, b, uid_b), ("b2a", b, a, uid_a)):
        rows, skipped, fingerprints = collect_rows(src, dst, dst_uid, options)
        plan.rows[side] = rows
        plan.skipped[side] = skipped
        plan.fingerprints[side] = fingerprints

        cids = [r["id"] for r in rows.get("sessions", [])]
        total_sessions += len(cids)
        src_uid = uid_a if side == "a2b" else uid_b  # 用于定位 storage/user-<uid> 与 connectors/<uid>
        entries, stats = build_direction_entries(src, dst, src_uid, dst_uid, cids, options)
        for e in entries:
            if e.kind == "file" and os.path.isfile(e.src):
                approx += os.path.getsize(e.src)
            elif e.kind == "tree":
                approx += dir_size(e.src)
        plan.entries.extend(entries)

        plan.summary[side] = {
            "from": src.label,
            "to": dst.label,
            "sessions_to_copy": len(cids),
            "sessions_skipped": skipped.get("sessions", 0),
            "file_entries": sum(1 for e in entries if e.kind == "file"),
            "tree_entries": sum(1 for e in entries if e.kind == "tree"),
            "missing_assets": stats["missing_assets"],
            "cwd_invalid": stats["cwd_invalid"],
        }

    plan.summary["totals"] = {
        "sessions_to_copy": total_sessions,
        "approx_bytes": approx,
        "approx_human": human(approx),
    }
    plan.finalize()
    return plan


# --------------------------------------------------------------------------
# 备份
# --------------------------------------------------------------------------


BACKUP_DIRS = ("projects", "tasks", "blobs", "changes-detail", "changes-index",
               "file-history", "artifact-index", "memory", "skills", "connectors",
               "storage", "local_storage", "workspace", "plans", "projects-state")
# 体积大且与迁移无关：默认不备份，需要时用 --include-heavy 显式加入
HEAVY_DIRS = ("app", "logs", "traces", "shell-snapshots", "cache", "changes-tmp")
BACKUP_FILES = ("settings.json", "mcp.json", "models.json", "user-state.json",
                "workspace-state.json", "workspace-display-names.json",
                "last-launch.json", "qimei-cache.json")


def do_backup(args: argparse.Namespace, homes: list[Home]) -> int:
    dest = os.path.abspath(os.path.expanduser(args.dest))
    os.makedirs(dest, exist_ok=True)
    label = args.label or time.strftime("%Y%m%d-%H%M%S")
    root = os.path.join(dest, f"{label}-home-bridge")
    if os.path.exists(root):
        raise BridgeError(f"备份目标已存在，不覆盖：{root}")
    os.makedirs(root, mode=0o700)

    dirs = list(BACKUP_DIRS) + (list(HEAVY_DIRS) if args.include_heavy else [])

    est = 0
    for home in homes:
        home.require_valid()
        est += os.path.getsize(home.db_path)
        for name in dirs:
            est += dir_size(os.path.join(home.path, name))
    require_space(dest, est, "创建备份", headroom=2 * 1024 ** 3)

    for home in homes:
        home.require_valid()
        target = os.path.join(root, home.slug)
        os.makedirs(target, exist_ok=True)
        print(f"备份 {home.label} → {target}")

        con = home.connect()
        try:
            bck = sqlite3.connect(os.path.join(target, DB_NAME))
            with bck:
                con.backup(bck)
            bck.close()
        finally:
            con.close()

        for name in dirs:
            src = os.path.join(home.path, name)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(target, name), symlinks=True,
                                copy_function=shutil.copy2)
        for name in BACKUP_FILES:
            src = os.path.join(home.path, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(target, name))
        print(f"  完成：{human(dir_size(target))}")

    with open(os.path.join(root, "backup-manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "tool": f"wb-home-bridge {VERSION}",
                "include_heavy": bool(args.include_heavy),
                "excluded_by_default": [] if args.include_heavy else list(HEAVY_DIRS),
                "homes": [{"label": h.label, "path": h.path, "uid": h.current_uid()}
                          for h in homes],
            },
            fh, ensure_ascii=False, indent=2,
        )
    print(f"\n备份根目录：{root}")
    if not args.include_heavy:
        print(f"已排除（体积大且与迁移无关）：{', '.join(HEAVY_DIRS)}")
        print("需要连日志一起留档时，追加 --include-heavy 重跑。")
    print("注意：这是手工快照，不是本工具的 undo journal；整库回滚需手工恢复。")
    return 0


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


def require_space(path: str, needed: int, what: str, headroom: int = 1024 ** 3) -> None:
    """检查目标卷剩余空间。本机内部数据卷极易被打满，宁可提前拒绝。"""
    probe = path
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    if not probe:
        return
    free = shutil.disk_usage(probe).free
    if free < needed + headroom:
        raise BridgeError(
            f"磁盘空间不足，拒绝{what}。\n"
            f"  所在卷     : {probe}\n"
            f"  需要约     : {human(needed)}（另需 {human(headroom)} 余量）\n"
            f"  当前可用   : {human(free)}\n"
            "请先清理空间，或改用 --no-changes 减少体积。"
        )


def state_dir_guard(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.exists(path):
        st = os.stat(path)
        if not os.path.isdir(path):
            raise BridgeError(f"--state-dir 不是目录：{path}")
        if os.listdir(path) and (st.st_mode & 0o777) != 0o700:
            raise BridgeError(
                f"--state-dir 已存在且有内容，但权限不是 0700：{path}\n"
                "请提供一个新目录，或使用归属本工具的 0700 目录。"
            )
    else:
        os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def copy_file(src: str, dst: str, overwrite: bool) -> str:
    existed = os.path.exists(dst)
    if existed and not overwrite:
        return "skip"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return "overwrite" if existed else "create"


def merge_tree(src: str, dst: str, overwrite: bool) -> tuple[int, int]:
    created = skipped = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            s = os.path.join(root, name)
            d = os.path.join(target_root, name)
            if os.path.exists(d) and not overwrite:
                skipped += 1
                continue
            try:
                shutil.copy2(s, d)
                created += 1
            except OSError as exc:
                eprint(f"  ! 跳过 {s}：{exc}")
                skipped += 1
        for name in dirs:
            os.makedirs(os.path.join(target_root, name), exist_ok=True)
    return created, skipped


def copy_tree(src: str, dst: str, overwrite: bool) -> str:
    existed = os.path.isdir(dst)
    if existed and not overwrite:
        merge_tree(src, dst, overwrite)
        return "merge"
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True, copy_function=shutil.copy2)
    return "create"


def insert_rows(home: Home, table: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    con = home.connect()
    inserted = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        for row in rows:
            cols = ",".join(f'"{c}"' for c in row)
            marks = ",".join("?" for _ in row)
            try:
                cur = con.execute(
                    f'INSERT OR IGNORE INTO "{table}" ({cols}) VALUES ({marks})',
                    list(row.values()),
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += cur.rowcount
            except sqlite3.IntegrityError as exc:
                eprint(f"  ! {table} 插入被拒（{exc}）")
        con.commit()
    except sqlite3.Error:
        con.rollback()
        raise
    finally:
        con.close()
    return inserted


def _memory_lines(raw: str) -> list[str]:
    m = re.search(r"RAW_JSON_START(.*?)RAW_JSON_END", raw, re.S)
    body = raw[: m.start()] if m else raw
    body = re.sub(r"^#[^\n]*\n", "", body)
    body = re.sub(r"^>[^\n]*$", "", body, flags=re.M)
    body = re.sub(r"^##\s*Memory Block\s*$", "", body, flags=re.M)
    body = body.replace("---", "")
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def memory_merge(src_file: str, dst_file: str, dst_uid: str) -> tuple[str, int]:
    """把源记忆并入目标记忆：非空行去重并集，改写 uid，保持文件结构。"""
    src_raw = open(src_file, "r", encoding="utf-8").read()
    dst_raw = open(dst_file, "r", encoding="utf-8").read() if os.path.exists(dst_file) else ""
    merged = _memory_lines(dst_raw)
    added = 0
    for ln in _memory_lines(src_raw):
        if ln not in merged:
            merged.append(ln)
            added += 1
    body = "\n\n".join(merged)
    now = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    text = (
        "# User Memory Profile\n"
        f"> Last updated: {now}\n"
        f"> Version: {max(1, len(merged) and 1 or 0) if merged else 0}\n"
        "\n"
        "## Memory Block\n"
        "\n"
        f"{body}\n"
        "\n"
        "\n"
        "---\n"
        "\n"
        "<!-- RAW_JSON_START\n"
        + json.dumps({"uid": dst_uid, "memoryBlock": body, "updatedAt": now},
                     ensure_ascii=False, indent=2)
        + "\nRAW_JSON_END -->\n"
    )
    with open(dst_file, "w", encoding="utf-8") as fh:
        fh.write(text)
    return "merged", added


def do_apply(args: argparse.Namespace, a: Home, b: Home) -> int:
    with open(args.plan, "r", encoding="utf-8") as fh:
        saved = json.load(fh)
    if args.confirm != saved.get("plan_id"):
        raise BridgeError(
            "确认串不匹配。--confirm 必须是计划的完整 plan_id，不支持短前缀。\n"
            f"计划 plan_id: {saved.get('plan_id')}"
        )
    if saved["homes"]["a"]["path"] != a.path or saved["homes"]["b"]["path"] != b.path:
        raise BridgeError("计划记录的两个 home 与当前参数不一致。")

    # 用计划里记录的执行选项重建，避免 apply 与 plan 选项不一致导致误判漂移
    opts = saved["options"]
    for key, attr in (("include_changes", "no_changes"), ("include_skills", "no_skills"),
                      ("include_memory", "no_memory"), ("include_claw", "no_claw")):
        setattr(args, attr, not opts.get(key, True))
    for key in ("include_plugins", "include_automations", "include_storage",
                "include_connectors", "overwrite_assets"):
        setattr(args, key, bool(opts.get(key)))
    args.output = None

    fresh = build_plan(args, a, b)
    if fresh.plan_id != saved["plan_id"]:
        raise BridgeError(
            "计划已漂移（源数据或账号快照在生成计划后发生了变化），拒绝执行。\n"
            f"原计划: {saved['plan_id']}\n当前值: {fresh.plan_id}\n"
            "请重新生成计划并重新审阅。"
        )
    plan = fresh.as_dict()

    approx = int(plan["summary"]["totals"]["approx_bytes"])
    # 并集目录可能已存在部分内容，按一半估算实际增量
    require_space(a.path, approx, "执行迁移")

    state_dir = state_dir_guard(args.state_dir)
    run_dir = os.path.join(state_dir, "runs", saved["plan_id"])
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    journal_path = os.path.join(run_dir, "journal.jsonl")
    undo_path = os.path.join(run_dir, "undo.json")

    undo: dict[str, Any] = {"plan_id": saved["plan_id"], "db": [], "files": [],
                            "trees": [], "merged_dirs": []}
    if os.path.exists(undo_path):
        with open(undo_path, "r", encoding="utf-8") as fh:
            undo = json.load(fh)
        eprint(f"注意：该计划已有运行记录，将按幂等语义继续（{run_dir}）。")

    def log(rec: dict[str, Any]) -> None:
        rec["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def save_undo() -> None:
        tmp = undo_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(undo, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, undo_path)

    # 1) 数据库行
    print("== 数据库行 ==")
    for side in ("a2b", "b2a"):
        target = b if side == "a2b" else a
        for table, rows in plan["rows"].get(side, {}).items():
            pk = {"sessions": "id", "automations": "id", "session_usage": "session_id",
                  "workspaces": "path", "buddy_snapshots": "id",
                  "automation_runs": "thread_id",
                  "automation_runtime_state": "automation_id"}.get(table)
            if rows and pk:
                ids = [r.get(pk) for r in rows if r.get(pk) is not None]
                if ids:
                    undo["db"].append({"home": target.path, "table": table, "pk": pk, "ids": ids})
            n = insert_rows(target, table, rows)
            if rows:
                print(f"  [{side}] {table}: 计划 {len(rows)} 行，实插 {n} 行")
                log({"op": "db_insert", "side": side, "home": target.path,
                     "table": table, "planned": len(rows), "inserted": n})
        save_undo()

    # 2) 文件
    print("== 文件资产 ==")
    overwrite = bool(plan["options"].get("overwrite_assets"))
    created_files = 0
    for entry in plan["entries"]:
        src, dst, mode = entry["src"], entry["dst"], entry["mode"]
        if mode == "copy_if_missing":
            if not os.path.isfile(src):
                continue
            existed = os.path.exists(dst)
            status = copy_file(src, dst, overwrite)
            if status in ("create", "overwrite"):
                created_files += 1
                if not existed:
                    undo["files"].append(dst)
                log({"op": "file", "status": status, "dst": dst})
        elif mode == "copy_tree_if_missing":
            if not os.path.isdir(src):
                continue
            existed = os.path.isdir(dst)
            status = copy_tree(src, dst, overwrite)
            if not existed:
                undo["trees"].append(dst)
            log({"op": "tree", "status": status, "dst": dst})
        elif mode == "merge_tree":
            if not os.path.isdir(src):
                continue
            created, skipped = merge_tree(src, dst, overwrite)
            created_files += created
            undo.setdefault("merged_dirs", []).append(dst)
            log({"op": "merge_tree", "dst": dst, "created": created, "skipped": skipped})
    save_undo()
    print(f"  新增文件 {created_files} 个")

    # 3) 记忆合并
    if plan["options"].get("include_memory"):
        print("== 长期记忆 ==")
        for side, src_home, dst_home, dst_uid in (
            ("a2b", a, b, b.current_uid()),
            ("b2a", b, a, a.current_uid()),
        ):
            src_dir = os.path.join(src_home.path, "memory")
            dst_dir = os.path.join(dst_home.path, "memory")
            os.makedirs(dst_dir, exist_ok=True)
            dst_file = os.path.join(dst_dir, f"{dst_uid}_memory.md")
            candidates = [
                os.path.join(src_dir, n)
                for n in os.listdir(src_dir)
                if n.endswith("_memory.md") and os.path.isfile(os.path.join(src_dir, n))
            ]
            if not candidates:
                print(f"  [{side}] 源无记忆文件，跳过")
                continue
            best = max(candidates, key=os.path.getsize)
            if os.path.getsize(best) == 0:
                print(f"  [{side}] 源记忆为空，跳过")
                continue
            if os.path.exists(dst_file):
                shutil.copy2(dst_file, dst_file + f".before-bridge-{int(time.time())}")
            else:
                undo["files"].append(dst_file)
            status, added = memory_merge(best, dst_file, dst_uid)
            save_undo()
            print(f"  [{side}] {os.path.basename(best)} → {os.path.basename(dst_file)}，新增 {added} 段")
            log({"op": "memory", "side": side, "src": best, "dst": dst_file,
                 "status": status, "added": added})

    # 4) claw.users 渠道绑定
    if plan["options"].get("include_claw"):
        for side, src_home, dst_home in (("a2b", a, b), ("b2a", b, a)):
            sp = os.path.join(src_home.path, "settings.json")
            dp = os.path.join(dst_home.path, "settings.json")
            if not (os.path.isfile(sp) and os.path.isfile(dp)):
                continue
            with open(sp, "r", encoding="utf-8") as fh:
                s = json.load(fh)
            with open(dp, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            s_users = (s.get("claw") or {}).get("users") or {}
            d_users = d.setdefault("claw", {}).setdefault("users", {})
            added = [uid for uid in s_users if uid not in d_users]
            if added:
                shutil.copy2(dp, dp + f".before-bridge-{int(time.time())}")
                for uid in added:
                    d_users[uid] = s_users[uid]
                with open(dp, "w", encoding="utf-8") as fh:
                    json.dump(d, fh, ensure_ascii=False, indent=2)
                print(f"  [{side}] settings.json 渠道绑定：新增 {len(added)} 个账号条目")
                log({"op": "claw_users", "side": side, "dst": dp, "added": added})

    undo["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_undo()
    print(f"\n完成。运行目录：{run_dir}")
    print("请重启两个 App 后再查看（客户端有内存缓存，不重启看不到新会话）。")
    return 0


def do_restore(args: argparse.Namespace) -> int:
    run_dir = os.path.abspath(os.path.expanduser(args.run_dir))
    undo_path = os.path.join(run_dir, "undo.json")
    if not os.path.isfile(undo_path):
        raise BridgeError(f"找不到 undo 记录：{undo_path}")
    with open(undo_path, "r", encoding="utf-8") as fh:
        undo = json.load(fh)
    if args.confirm != undo.get("plan_id"):
        raise BridgeError("确认串不匹配。--confirm 必须是该 run 的完整 plan_id。")

    removed = 0
    for path in sorted(undo.get("files", []), key=len, reverse=True):
        if os.path.isfile(path) and not path.endswith(".before-bridge-" + path.rsplit("-", 1)[-1]):
            try:
                os.remove(path)
                removed += 1
            except OSError as exc:
                eprint(f"  ! 无法删除 {path}：{exc}")
    for path in sorted(undo.get("trees", []), key=len, reverse=True):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)

    for rec in undo.get("db", []):
        home_path, table, pk, ids = rec["home"], rec["table"], rec["pk"], rec.get("ids") or []
        ids = [i for i in ids if i is not None]
        if not ids:
            continue
        con = sqlite3.connect(os.path.join(home_path, DB_NAME), timeout=60.0)
        try:
            con.execute("PRAGMA busy_timeout=60000")
            con.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" for _ in ids)
            cur = con.execute(f'DELETE FROM "{table}" WHERE "{pk}" IN ({marks})', ids)
            print(f"  回滚 {home_path} {table}: 删除 {cur.rowcount} 行")
            con.commit()
        finally:
            con.close()

    print(f"\n已回滚 {removed} 个新建文件。")
    print("并集目录（blobs/skills/connectors-skills）中的新增文件未自动删除，请手工检查。")
    print("记忆文件已被合并改写，已保留 .before-bridge-* 备份，可手工还原。")
    return 0


def do_verify(args: argparse.Namespace, a: Home, b: Home) -> int:
    with open(args.plan, "r", encoding="utf-8") as fh:
        plan = json.load(fh)

    ok = True
    for side in ("a2b", "b2a"):
        target = b if side == "a2b" else a
        want = plan["rows"].get(side, {}).get("sessions", [])
        con = target.connect()
        try:
            have = set()
            for r in con.execute("SELECT id, user_id FROM sessions"):
                have.add(r[0])
            missing = [r["id"] for r in want if r["id"] not in have]
            wrong = [
                r["id"] for r in want
                if r["id"] in have
                and con.execute("SELECT user_id FROM sessions WHERE id=?", (r["id"],)).fetchone()[0]
                != r["user_id"]
            ]
        finally:
            con.close()

        absent = 0
        for row in want:
            if not project_slug_for(target, row["id"]):
                absent += 1

        print(f"[{side}] → {target.label}: 应有 {len(want)} 条 | 缺行 {len(missing)} | "
              f"归属错 {len(wrong)} | 缺正文 {absent}")
        if missing or wrong or absent:
            ok = False
            for cid in (missing or wrong)[:5]:
                print(f"    ! {cid}")

    print("\n核验结论：" + ("通过（仅就本地会话行与正文文件而言）" if ok else "未通过"))
    print("未覆盖：界面展示、云端同步一致性、正文以外的资产、跨 App 的权限模型。")
    return 0 if ok else 3


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--home-a", default=None,
                   help="WorkBuddy 的数据目录（默认按平台自动探测）")
    p.add_argument("--home-b", default=None,
                   help="WorkBuddy AI 的数据目录（默认按平台自动探测）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")


def add_plan_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-changes", action="store_true",
                   help="不搬 changes-detail / changes-index / file-history")
    p.add_argument("--no-skills", action="store_true", help="不合并用户技能目录")
    p.add_argument("--no-memory", action="store_true", help="不合并长期记忆")
    p.add_argument("--no-claw", action="store_true", help="不合并 settings.json 渠道绑定")
    p.add_argument("--include-plugins", action="store_true", help="合并 plugins/cache")
    p.add_argument("--include-automations", action="store_true",
                   help="复制自动化定义（以 PAUSED 落地，避免两个 App 各跑一遍）")
    p.add_argument("--include-storage", action="store_true",
                   help="复制账号个人存储目录（改名合并，已存在不覆盖）")
    p.add_argument("--include-connectors", action="store_true",
                   help="只并连接器开关状态，不搬凭据（凭据跨 home 无法解密）")
    p.add_argument("--overwrite-assets", action="store_true",
                   help="已存在的资产文件也覆盖（默认跳过）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wb-home-bridge",
        description="WorkBuddy 与 WorkBuddy AI 两个数据目录的双向打通工具"
                    "（只新增，不覆盖已有数据）。",
    )
    p.add_argument("--version", action="version", version=f"wb-home-bridge {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    for name, help_text in (("survey", "只读盘点两个 home，不做任何改动"),
                            ("doctor", "survey 的别名")):
        s = sub.add_parser(name, help=help_text)
        add_common(s)

    s = sub.add_parser("backup", help="手工全量快照（需先退出客户端）")
    add_common(s)
    s.add_argument("--dest", required=True, help="备份根目录")
    s.add_argument("--label", default="", help="快照标签，默认时间戳")
    s.add_argument("--include-heavy", action="store_true",
                   help="连 app/logs/traces 一起备份（体积大，注意磁盘空间）")
    s.add_argument("--allow-client-running", action="store_true")

    s = sub.add_parser("plan", help="生成计划，不写数据")
    add_common(s)
    add_plan_options(s)
    s.add_argument("--output", help="把计划写入该文件（已存在不覆盖）")
    s.add_argument("--allow-client-running", action="store_true",
                   help="计划为只读，此参数仅为与 apply 调用形式保持一致")

    s = sub.add_parser("apply", help="执行计划（需先退出两个客户端）")
    add_common(s)
    s.add_argument("--plan", required=True, help="计划文件")
    s.add_argument("--state-dir", required=True, help="客户端目录之外的独立状态目录")
    s.add_argument("--confirm", required=True, help="计划完整 plan_id")
    s.add_argument("--allow-client-running", action="store_true")

    s = sub.add_parser("verify", help="核验执行结果（只读）")
    add_common(s)
    s.add_argument("--plan", required=True)

    s = sub.add_parser("restore", help="撤销本次运行写入的行与文件")
    s.add_argument("--run-dir", required=True, help="state-dir/runs/<plan_id>")
    s.add_argument("--confirm", required=True, help="该 run 的完整 plan_id")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "restore":
            return do_restore(args)

        if args.command in ("survey", "doctor"):
            a, b = build_homes(args)
            infos = [survey(a), survey(b)]
            if args.json:
                emit({"version": VERSION, "homes": infos})
            else:
                print_survey(infos[0])
                print()
                print_survey(infos[1])
                sa = set(infos[0].get("skills") or [])
                sb = set(infos[1].get("skills") or [])
                shared = sorted(sa & sb)
                print()
                print("── 跨 App 技能对比 ──")
                print(f"  仅 WorkBuddy   有 : {len(sa - sb)} 个")
                print(f"  仅 WorkBuddy AI 有 : {len(sb - sa)} 个")
                print(f"  两边同名         : {len(shared)} 个{('  ' + '、'.join(shared)) if shared else ''}")
                print(f"  合并后每边可用   : {len(sa | sb)} 个")
                print("  同名技能两边各留各的版本，不互相覆盖。")
            return 0

        if args.command == "backup":
            require_clients_stopped(args.allow_client_running)
            a, b = build_homes(args)
            return do_backup(args, [a, b])

        if args.command == "plan":
            a, b = build_homes(args)
            plan = build_plan(args, a, b)
            data = plan.as_dict()
            if args.output:
                out = os.path.abspath(os.path.expanduser(args.output))
                if os.path.exists(out):
                    raise BridgeError(f"输出文件已存在，不覆盖：{out}")
                with open(out, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                os.chmod(out, 0o600)
            if args.json:
                emit(data)
            else:
                print(f"plan_id: {plan.plan_id}")
                for side in ("a2b", "b2a"):
                    s = plan.summary[side]
                    print(f"[{side}] {s['from']} → {s['to']}: "
                          f"{s['sessions_to_copy']} 条会话待复制"
                          f"（目标已存在跳过 {s['sessions_skipped']}），"
                          f"{s['file_entries']} 个文件 + {s['tree_entries']} 个目录，"
                          f"缺资产 {s['missing_assets']}，cwd 失效 {s['cwd_invalid']}")
                t = plan.summary["totals"]
                print(f"合计：{t['sessions_to_copy']} 条会话，约 {t['approx_human']}")
                print("选项：" + "  ".join(
                    f"{k}={'是' if v else '否'}" for k, v in plan.options.items()))
                if args.output:
                    print(f"计划已写入：{os.path.abspath(args.output)}")
                print("\n执行前请退出两个客户端，然后在 Terminal.app 中运行 apply。")
            return 0

        if args.command == "apply":
            require_clients_stopped(args.allow_client_running)
            a, b = build_homes(args)
            return do_apply(args, a, b)

        if args.command == "verify":
            a, b = build_homes(args)
            return do_verify(args, a, b)

        parser.error("未知命令")
        return 2
    except BridgeError as exc:
        eprint(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
