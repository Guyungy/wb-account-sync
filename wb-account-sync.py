#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-account-sync —— WorkBuddy 跨账号数据保留 / 备份 / 过户工具

用途
----
WorkBuddy 同一时间只能登录一个账号，各账号的数据按 user_id 分开存放。
本工具让你在切换账号前把「账号级资产」完整备份下来，或者直接过户给
另一个账号，使切换后依然能看到原来的任务、对话历史、自动化、长期记忆、
连接器授权、项目记录等。

账号级资产清单（本工具处理的范围）
--------------------------------
1. workbuddy.db 内的行级数据
   - sessions                    (user_id)        任务 / 对话历史
   - automations                 (owner_user_id)  自动化
   - automation_delivery_outbox  (owner_user_id)  自动化待投递消息
   - automation_runs / automation_runtime_state / session_usage / buddy_snapshots
     → 通过 automation_id / session_id 间接归属，跟随主表自动保留
   - workspaces                  → 设备级，不含 user_id，不动
2. ~/.workbuddy/memory/<uid>_memory.md          长期记忆（记忆与进化）
3. ~/.workbuddy/connectors/<uid>/               连接器授权
4. ~/.workbuddy/storage/user-<uid>-personal/    账号个人存储（专家、偏好等）
   ~/.workbuddy/storage/user-<uid>/
5. ~/.workbuddy/settings.json  → claw.users.<uid>  渠道绑定

会话内容资产（tasks/ traces/ blobs/ changes-*/ file-history/ artifact-index/）
不按账号分目录，过户 user_id 后依然可见，因此本工具只需备份、无需搬运。

子命令
------
  status                       列出所有账号及其资产统计，标出当前账号
  backup [--label L] [--slim]  全量快照（db 一致性副本 + 账号级目录 + 会话内容资产）
  snapshots                    列出已有快照
  restore <快照名|latest>      从快照恢复（默认行级 diff；可加 --prune / --prune-accounts）
  adopt --from S --to T        把源账号的账号级资产过户给目标账号（默认演练）
  revert                       撤销最近一次 adopt（还原 pre-adopt 快照）

持续同步（推荐，无需手动切换）
------------------------------
  sync [--dry-run]             执行一轮全量同步（吸池 + 回写当前账号）
  live [--interval 5]          前台常驻，账号一变就同步
  daemon-install               安装 launchd 后台服务（开机自启 + 常驻）
  daemon-uninstall             卸载后台服务
  daemon-status                查看服务与数据池状态
  daemon-log [-n 40]           查看同步日志

持续同步的原理：所有账号的账号级资产都汇入一个「统一数据池」
（~/.workbuddy-account-sync/pool/），再把池子内容回写到当前登录账号。
于是无论登录哪个账号，看到的都是全部数据。触发时机：
  - 检测到当前账号变化（登录 / 切换）→ 立即同步
  - 检测到客户端刚退出               → 立即同步（最安全的窗口）
  - 每 N 秒兜底校准                  → 捕捉新产生的跨账号数据

与 restore / adopt 不同，持续同步**不做任何删除**，只做 upsert 与行级 UPDATE，
因此可以在客户端运行中使用（SQLite WAL，不会整库覆盖）。

⚠ 重要：必须在 WorkBuddy 完全退出后，用「系统终端」运行写操作
--------------------------------------------------------------
本脚本的写操作（restore / adopt / revert）会要求 WorkBuddy 主进程不在运行。
注意 WorkBuddy 的 macOS 主进程可执行文件名为 Electron
（/Applications/WorkBuddy.app/Contents/MacOS/Electron），因此不能用进程名匹配。

**不要在 WorkBuddy 内部（比如让它自己调用终端）执行写操作**：WorkBuddy 的应用
进程会随之启动，检查必然失败；即使强行 --force 也可能被客户端回写覆盖。

正确流程：
    1. ⌘Q 退出 WorkBuddy
    2. 打开 Terminal.app / iTerm
    3. 运行  ./wb-account-sync.sh adopt --from <源> --to current --yes
    4. 重新打开 WorkBuddy

安全设计
--------
- 写操作前强制检查主进程，运行中直接拒绝（--force 可绕过，仅用于应急回滚）。
- 每次写操作（restore / adopt / revert）前自动生成快照，可随时回退。
- adopt 默认只演练，必须显式加 --yes 才落盘。
- restore 默认走「行级 diff 回写」（普通事务，安全），不会整库覆盖；
  需要整库覆盖时显式加 --overwrite-db。
- adopt 会写一份 adopt-ledger.json 到快照目录，记录所有移动/新建项。

⚠ --prune-accounts 的风险
------------------------
该选项会让账号级目录严格对齐快照，因此会**删除快照生成之后新建的账号级文件**
（例如客户端运行期间新写入的 scoped/*.json UI 状态）。仅在你确实要回到快照那一刻
的状态、且客户端已退出时使用。

用法示例
--------
  # 看现状（只读，可在任何地方运行）
  python3 wb-account-sync.py status

  # 切账号前先备份
  python3 wb-account-sync.py backup --label before-switch

  # 把老账号 a1b2c3d4... 的全部数据过户给当前账号（先演练）
  python3 wb-account-sync.py adopt --from a1b2c3d4 --to current

  # 确认无误后真正执行（需先退出 WorkBuddy，在系统终端运行）
  python3 wb-account-sync.py adopt --from a1b2c3d4 --to current --yes

  # 反悔
  python3 wb-account-sync.py revert --yes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 常量与基础工具
# --------------------------------------------------------------------------

WB_HOME = Path(os.environ.get("WORKBUDDY_HOME", Path.home() / ".workbuddy"))
DB_PATH = WB_HOME / "workbuddy.db"
MEMORY_DIR = WB_HOME / "memory"
CONNECTORS_DIR = WB_HOME / "connectors"
STORAGE_DIR = WB_HOME / "storage"
SETTINGS_PATH = WB_HOME / "settings.json"
ACCOUNT_SNAPSHOT_PATH = STORAGE_DIR / "skeleton" / "account-snapshot.json"

DEFAULT_BACKUP_ROOT = Path.home() / ".workbuddy-account-snapshots"

# 需要备份的账号级目录（相对 WB_HOME），备份时逐账号展开
ACCOUNT_DIR_GLOBS = [
    "memory/{uid}_memory.md",
    "memory/{uid}_memory.md.bak",
    "connectors/{uid}",
    "storage/user-{uid}-personal",
    "storage/user-{uid}",
]

# 需要备份的设备级 / 会话级内容目录（整体打包，不按账号拆）
DEVICE_DIRS = [
    "tasks",
    "traces",
    "blobs",
    "sessions",
    "projects",
    "artifacts",
    "artifact-index",
    "artifact-index2",
    "changes-index",
    "changes-detail",
    "file-history",
    "file-tree-manifests",
    "project-resources",
]

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

C_OK = "\033[32m"
C_WARN = "\033[33m"
C_ERR = "\033[31m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_OFF = "\033[0m"


def color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"{code}{text}{C_OFF}" if color_enabled() else text


def ok(msg: str) -> None:
    print(f"{c('✔', C_OK)} {msg}")


def warn(msg: str) -> None:
    print(f"{c('!', C_WARN)} {msg}")


def err(msg: str) -> None:
    print(f"{c('✘', C_ERR)} {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"  {msg}")


def head(msg: str) -> None:
    print(f"\n{c(msg, C_BOLD)}")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def du(path: Path) -> int:
    """目录/文件真实字节数（不依赖 du，避免簇浪费干扰）。"""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = Path(root) / f
            try:
                if not fp.is_symlink():
                    total += fp.stat().st_size
            except OSError:
                pass
    return total


def short(uid: str, n: int = 8) -> str:
    return uid[:n] if uid else "(空)"


def resolve_uid(token: str, accounts: list[str]) -> str:
    """把用户输入的 uid 前缀解析成完整 uid。"""
    if not token:
        raise SystemExit("uid 不能为空")
    if token in ("current", "now", "self"):
        cur = current_uid()
        if not cur:
            raise SystemExit("无法识别当前账号，请直接指定完整 uid")
        return cur
    hits = [a for a in accounts if a == token or a.startswith(token)]
    if not hits:
        raise SystemExit(f"找不到账号: {token}（可用 `status` 查看）")
    if len(hits) > 1:
        raise SystemExit(f"账号前缀有歧义: {token} → {', '.join(short(h) for h in hits)}")
    return hits[0]


# --------------------------------------------------------------------------
# 账号发现
# --------------------------------------------------------------------------


def current_uid() -> str | None:
    try:
        data = json.loads(ACCOUNT_SNAPSHOT_PATH.read_text("utf-8"))
        return (data.get("primary") or {}).get("uid") or None
    except Exception:
        return None


def account_labels() -> dict[str, str]:
    """尽可能取到账号昵称。"""
    labels: dict[str, str] = {}
    try:
        data = json.loads(ACCOUNT_SNAPSHOT_PATH.read_text("utf-8"))
        p = data.get("primary") or {}
        if p.get("uid"):
            labels[p["uid"]] = p.get("nickname") or ""
    except Exception:
        pass
    return labels


def discover_accounts() -> list[str]:
    found: set[str] = set()

    if DB_PATH.exists():
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            for table, col in (
                ("sessions", "user_id"),
                ("automations", "owner_user_id"),
            ):
                try:
                    rows = con.execute(
                        f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''"
                    ).fetchall()
                    found.update(r[0] for r in rows)
                except sqlite3.Error:
                    pass
            con.close()
        except sqlite3.Error:
            pass

    if MEMORY_DIR.is_dir():
        for f in MEMORY_DIR.glob("*_memory.md"):
            uid = f.name[: -len("_memory.md")]
            if UUID_RE.match(uid):
                found.add(uid)

    if CONNECTORS_DIR.is_dir():
        for d in CONNECTORS_DIR.iterdir():
            if d.is_dir() and UUID_RE.match(d.name):
                found.add(d.name)

    if STORAGE_DIR.is_dir():
        for d in STORAGE_DIR.iterdir():
            if not d.is_dir() or not d.name.startswith("user-"):
                continue
            uid = d.name[len("user-") :]
            if uid.endswith("-personal"):
                uid = uid[: -len("-personal")]
            if UUID_RE.match(uid):
                found.add(uid)

    try:
        cfg = json.loads(SETTINGS_PATH.read_text("utf-8"))
        found.update((cfg.get("claw") or {}).get("users", {}).keys())
    except Exception:
        pass

    cur = current_uid()
    if cur:
        found.add(cur)

    return sorted(found)


def account_stats(uid: str) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "sessions": 0,
        "sessions_last": None,
        "workspaces": 0,
        "automations": 0,
        "automations_active": 0,
        "runs": 0,
        "memory": 0,
        "connectors": 0,
        "storage": 0,
        "claw_channels": [],
    }

    if DB_PATH.exists():
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            row = con.execute(
                "SELECT COUNT(*), MAX(last_activity_at) FROM sessions WHERE user_id=?",
                (uid,),
            ).fetchone()
            stats["sessions"] = row[0] or 0
            stats["sessions_last"] = row[1]
            stats["workspaces"] = con.execute(
                "SELECT COUNT(DISTINCT cwd) FROM sessions WHERE user_id=?", (uid,)
            ).fetchone()[0] or 0
            stats["automations"] = con.execute(
                "SELECT COUNT(*) FROM automations WHERE owner_user_id=?", (uid,)
            ).fetchone()[0] or 0
            stats["automations_active"] = con.execute(
                "SELECT COUNT(*) FROM automations WHERE owner_user_id=? AND status='ACTIVE' AND deleted_at IS NULL",
                (uid,),
            ).fetchone()[0] or 0
            stats["runs"] = con.execute(
                "SELECT COUNT(*) FROM automation_runs r "
                "JOIN automations a ON a.id = r.automation_id WHERE a.owner_user_id=?",
                (uid,),
            ).fetchone()[0] or 0
            con.close()
        except sqlite3.Error:
            pass

    mf = MEMORY_DIR / f"{uid}_memory.md"
    stats["memory"] = len(extract_memory_block(mf)) if mf.exists() else 0

    for p in (CONNECTORS_DIR / uid, STORAGE_DIR / f"user-{uid}-personal", STORAGE_DIR / f"user-{uid}"):
        if p.exists():
            stats["connectors" if "connectors" in str(p) else "storage"] += du(p)

    try:
        cfg = json.loads(SETTINGS_PATH.read_text("utf-8"))
        users = (cfg.get("claw") or {}).get("users", {})
        chans = (users.get(uid) or {}).get("channels") or {}
        stats["claw_channels"] = sorted(chans.keys())
    except Exception:
        pass

    return stats


def fmt_ts(ms: int | None) -> str:
    if not ms:
        return "-"
    try:
        v = ms / 1000 if ms > 10_000_000_000 else ms
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(v))
    except Exception:
        return "-"


# --------------------------------------------------------------------------
# 运行时保护
# --------------------------------------------------------------------------


def app_main_pids() -> list[str]:
    """返回 WorkBuddy 主进程 PID 列表。

    注意：WorkBuddy 的 macOS 主进程可执行文件叫 Electron
    （/Applications/WorkBuddy.app/Contents/MacOS/Electron），
    不是 WorkBuddy，因此不能用名字匹配。这里按「应用包内、非 Frameworks
    目录下的可执行文件」来识别，可覆盖 Helper / 各 sidecar 子进程。
    """
    pids: list[str] = []
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, timeout=20
            ).stdout
        except Exception:
            return pids
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            pid, _, cmd = line.partition(" ")
            if "WorkBuddy.app/Contents/MacOS/" in cmd and "/Frameworks/" not in cmd:
                pids.append(pid)
        return pids

    if sys.platform.startswith("win"):
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout
            for line in out.splitlines():
                if "WorkBuddy.exe" in line:
                    pids.append(line.split(",")[1].strip('"'))
        except Exception:
            pass
        return pids

    try:
        out = subprocess.run(
            ["pgrep", "-f", "-i", "workbuddy"], capture_output=True, text=True, timeout=20
        ).stdout
        return [x for x in out.split() if x.strip()]
    except Exception:
        return pids


def require_app_closed(force: bool = False) -> None:
    pids = app_main_pids()
    if not pids:
        return
    msg = (
        "检测到 WorkBuddy 正在运行（PID %s）。\n"
        "  写操作必须在完全退出客户端后进行，否则数据库写入可能被应用覆盖或损坏。\n"
        "  步骤：\n"
        "    1. 退出 WorkBuddy（⌘Q，不是关窗口）\n"
        "    2. 打开「终端 / Terminal.app」（不要在 WorkBuddy 内部运行，\n"
        "       因为 WorkBuddy 的应用进程会随之启动）\n"
        "    3. 在终端里重新执行本命令" % ", ".join(pids)
    )
    if force:
        warn("已用 --force 跳过运行中检查。")
        for line in msg.splitlines():
            warn(line if line.strip() else "")
        return
    err(msg)
    raise SystemExit(2)


# --------------------------------------------------------------------------
# 长期记忆读写（保持 RAW_JSON 结构）
# --------------------------------------------------------------------------

RAW_START = "<!-- RAW_JSON_START"
RAW_END = "RAW_JSON_END -->"
_RAW_RE = re.compile(re.escape(RAW_START) + r"(.*?)" + re.escape(RAW_END), re.S)


def read_memory_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"memoryBlock": "", "raw": None, "text": ""}
    text = path.read_text("utf-8")
    m = _RAW_RE.search(text)
    raw = None
    if m:
        try:
            raw = json.loads(m.group(1).strip())
        except Exception:
            raw = None
    block = (raw or {}).get("memoryBlock", "")
    if not block:
        # 回退：截取 Memory Block 段落的纯文本
        seg = re.search(r"## Memory Block\s*(.*?)\n---", text, re.S)
        if seg:
            block = seg.group(1).strip()
    return {"memoryBlock": block, "raw": raw, "text": text}


def extract_memory_block(path: Path) -> str:
    return read_memory_file(path)["memoryBlock"]


def write_memory_file(path: Path, memory_block: str, uid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    payload = {
        "uid": uid,
        "memoryBlock": memory_block,
        "updatedAt": now,
    }
    content = (
        "# User Memory Profile\n"
        f"> Last updated: {now}\n"
        "> Version: 0\n\n"
        "## Memory Block\n\n"
        f"{memory_block}\n\n"
        "---\n\n"
        f"{RAW_START}\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        f"{RAW_END}\n"
    )
    # 原文件留一份 .bak，避免误删
    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass
    path.write_text(content, "utf-8")


def merge_memory_blocks(a: str, b: str) -> str:
    """按行合并两段记忆，去重，保留原顺序（a 在前）。"""
    if not a.strip():
        return b.strip()
    if not b.strip():
        return a.strip()
    seen: set[str] = set()
    out: list[str] = []
    for line in (a.strip() + "\n" + b.strip()).splitlines():
        key = line.strip()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(line)
    return "\n".join(out).strip()


# --------------------------------------------------------------------------
# SQLite 一致性副本
# --------------------------------------------------------------------------


def sqlite_snapshot(dst: Path) -> None:
    """用官方 backup API 生成一致性副本，对运行中的 WAL 库也安全。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()


def sqlite_restore_inplace(src: Path) -> None:
    """把快照库写回主库（保留主库路径，删除残留 WAL/SHM）。"""
    if not DB_PATH.exists():
        # 首次创建
        shutil.copy2(src, DB_PATH)
        return
    live = sqlite3.connect(str(DB_PATH), timeout=30)
    snap = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        snap.backup(live)
    finally:
        snap.close()
        live.close()
    for suffix in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def db_write(sql: str, params: tuple = ()) -> int:
    con = sqlite3.connect(str(DB_PATH), timeout=60)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        cur = con.execute(sql, params)
        n = cur.rowcount
        con.commit()
        return n
    finally:
        con.close()


# 需要按行级 diff 回写的表（主键 → 归属字段）
DIFF_TABLES: dict[str, tuple[str, str]] = {
    "sessions": ("id", "user_id"),
    "automations": ("id", "owner_user_id"),
    "automation_delivery_outbox": ("id", "owner_user_id"),
}


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def restore_db_diff(snap_db: Path, prune: bool = False) -> dict[str, tuple[int, int, int]]:
    """按行级 diff 把快照内容回写到主库。

    相比整库覆盖，这种方式使用普通事务写入，可以安全地在客户端运行中执行，
    且只会改动有差异的行，不会丢掉快照之后产生的新数据（除非 --prune）。

    返回 {表名: (插入数, 更新数, 删除数)}
    """
    if not DB_PATH.exists():
        shutil.copy2(snap_db, DB_PATH)
        return {}

    result: dict[str, tuple[int, int, int]] = {}
    live = sqlite3.connect(str(DB_PATH), timeout=60)
    snap = sqlite3.connect(f"file:{snap_db}?mode=ro", uri=True)
    try:
        live.execute("PRAGMA foreign_keys=OFF")
        for table, (pk, _owner) in DIFF_TABLES.items():
            live_cols = _table_columns(live, table)
            snap_cols = _table_columns(snap, table)
            cols = [c for c in snap_cols if c in live_cols]
            if not cols:
                continue
            collist = ", ".join(f'"{c}"' for c in cols)
            placeholders = ", ".join("?" for _ in cols)

            snap_rows = {
                r[cols.index(pk)]: r
                for r in snap.execute(f'SELECT {collist} FROM "{table}"')
            }
            live_rows = {
                r[cols.index(pk)]: r
                for r in live.execute(f'SELECT {collist} FROM "{table}"')
            }

            ins = upd = dele = 0
            for key, srow in snap_rows.items():
                if key not in live_rows:
                    live.execute(
                        f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})', srow
                    )
                    ins += 1
                elif tuple(live_rows[key]) != tuple(srow):
                    setclause = ", ".join(f'"{c}"=?' for c in cols if c != pk)
                    vals = [v for c, v in zip(cols, srow) if c != pk]
                    live.execute(
                        f'UPDATE "{table}" SET {setclause} WHERE "{pk}"=?', (*vals, key)
                    )
                    upd += 1
            if prune:
                for key in live_rows:
                    if key not in snap_rows:
                        live.execute(f'DELETE FROM "{table}" WHERE "{pk}"=?', (key,))
                        dele += 1
            result[table] = (ins, upd, dele)
        live.commit()
    finally:
        snap.close()
        live.close()
    return result


# --------------------------------------------------------------------------
# 快照：备份 / 恢复
# --------------------------------------------------------------------------


def snapshot_root(args_out: str | None) -> Path:
    return Path(args_out).expanduser().resolve() if args_out else DEFAULT_BACKUP_ROOT


def make_snapshot(
    label: str | None, out_root: Path, quiet: bool = False, slim: bool = False
) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{label}" if label else stamp
    root = out_root / name
    root.mkdir(parents=True, exist_ok=True)

    if not quiet:
        head(f"生成快照 → {root}")
        if slim:
            info(c("（精简模式：只备份数据库 + 账号级资产，跳过会话内容大目录）", C_DIM))

    # 1) 数据库一致性副本
    db_dst = root / "workbuddy.db"
    if DB_PATH.exists():
        sqlite_snapshot(db_dst)
        if not quiet:
            ok(f"workbuddy.db  {human(db_dst.stat().st_size)}")
    else:
        warn("未找到 workbuddy.db，跳过数据库")

    # 2) 账号级资产
    accounts = discover_accounts()
    acc_dst = root / "accounts"
    copied: list[str] = []
    for uid in accounts:
        for pat in ACCOUNT_DIR_GLOBS:
            src = WB_HOME / pat.format(uid=uid)
            if not src.exists():
                continue
            rel = Path(pat.format(uid=uid))
            target = acc_dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(src, target)
            copied.append(str(rel))
    if not quiet:
        ok(f"账号级资产 {len(copied)} 项（{len(accounts)} 个账号）")

    # 3) settings.json
    if SETTINGS_PATH.exists():
        shutil.copy2(SETTINGS_PATH, root / "settings.json")
        if not quiet:
            ok("settings.json")

    # 4) 会话内容资产（设备级目录，精简模式跳过）
    payload = root / "payload"
    total = 0
    if not slim:
        for d in DEVICE_DIRS:
            src = WB_HOME / d
            if not src.is_dir():
                continue
            size = du(src)
            shutil.copytree(src, payload / d, dirs_exist_ok=True, symlinks=True)
            total += size
            if not quiet:
                info(f"payload/{d}  {human(size)}")

    # 5) 清单
    manifest = {
        "schema": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "created_at_unix": int(time.time()),
        "label": label or "",
        "slim": slim,
        "workbuddy_home": str(WB_HOME),
        "wb_version": read_wb_version(),
        "current_uid": current_uid(),
        "accounts": accounts,
        "account_files": sorted(set(copied)),
        "device_dirs": [] if slim else [d for d in DEVICE_DIRS if (WB_HOME / d).is_dir()],
        "payload_bytes": total,
        "has_db": db_dst.exists(),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8"
    )

    if not quiet:
        ok(f"快照完成：{root}")
    return root


def read_wb_version() -> str:
    try:
        data = json.loads((WB_HOME / "last-launch.json").read_text("utf-8"))
        return f"{data.get('version', '?')} ({data.get('build', '?')[:8]})"
    except Exception:
        return "未知"


def list_snapshots(root: Path) -> list[dict[str, Any]]:
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        mf = d / "manifest.json"
        if not mf.exists():
            continue
        try:
            m = json.loads(mf.read_text("utf-8"))
        except Exception:
            m = {}
        out.append({"path": d, "manifest": m})
    return out


def resolve_snapshot(token: str, root: Path) -> Path:
    if token in ("latest", "last", ""):
        snaps = list_snapshots(root)
        if not snaps:
            raise SystemExit(f"没有可用快照（目录：{root}）")
        return snaps[0]["path"]
    p = Path(token).expanduser()
    if p.is_dir() and (p / "manifest.json").exists():
        return p.resolve()
    cand = root / token
    if cand.is_dir() and (cand / "manifest.json").exists():
        return cand
    hits = [s["path"] for s in list_snapshots(root) if token in s["path"].name]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SystemExit(f"快照名有歧义: {token} → {', '.join(h.name for h in hits)}")
    raise SystemExit(f"找不到快照: {token}")


def do_restore(
    snap: Path,
    prune: bool = False,
    overwrite_db: bool = False,
    clean_migrated: bool = False,
    prune_accounts: bool = False,
    force: bool = False,
) -> None:
    mf = json.loads((snap / "manifest.json").read_text("utf-8"))
    head(f"从快照恢复 ← {snap.name}")
    info(f"快照时间：{mf.get('created_at')}    标签：{mf.get('label') or '-'}")
    info(f"包含账号：{', '.join(short(a) for a in mf.get('accounts', []))}")

    require_app_closed(force)

    # 恢复前先保住现状
    guard_slim = not (snap / "payload").is_dir()
    guard = make_snapshot("pre-restore", DEFAULT_BACKUP_ROOT, quiet=False, slim=guard_slim)
    warn(f"当前状态已备份到 {guard}（如需反悔可恢复它）")

    # 1) 数据库
    db_src = snap / "workbuddy.db"
    if not db_src.exists():
        warn("快照内没有数据库，跳过")
    elif overwrite_db:
        sqlite_restore_inplace(db_src)
        ok("workbuddy.db 已整库覆盖还原")
    else:
        stats = restore_db_diff(db_src, prune=prune)
        if not stats:
            info("数据库无需改动")
        for table, (i, u, d) in stats.items():
            ok(f"{table}: 新增 {i} / 更新 {u} / 删除 {d}")
        if not prune:
            info(c("（未删除快照中不存在的行；需要彻底对齐请加 --prune）", C_DIM))

    # 2) 账号级文件
    acc_src = snap / "accounts"
    n = 0
    if acc_src.is_dir():
        for root_, _dirs, files in os.walk(acc_src):
            for f in files:
                s = Path(root_) / f
                rel = s.relative_to(acc_src)
                d = WB_HOME / rel
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
                n += 1
    ok(f"账号级文件已还原（{n} 个文件）")

    # 3) settings.json
    if (snap / "settings.json").exists():
        shutil.copy2(snap / "settings.json", SETTINGS_PATH)
        ok("settings.json 已还原")

    # 4) 会话内容资产
    payload = snap / "payload"
    if payload.is_dir():
        k = 0
        for d in sorted(payload.iterdir()):
            if d.is_dir():
                shutil.copytree(d, WB_HOME / d.name, dirs_exist_ok=True, symlinks=True)
                k += 1
        ok(f"会话内容资产已还原（{k} 个目录）")
    else:
        info("快照为精简模式，不含会话内容资产（tasks/traces/blobs 等），已跳过")

    # 5) 清理 adopt 留下的中转目录 / 归档多余账号级文件
    leftovers = find_migrated_artifacts()
    if leftovers:
        print()
        if clean_migrated or prune_accounts:
            for p in leftovers:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            ok(f"已清理 {len(leftovers)} 个 .migrated-* 中转项")
        else:
            warn(f"发现 {len(leftovers)} 个 adopt 留下的 .migrated-* 中转项，未清理：")
            for p in leftovers[:10]:
                info(str(p.relative_to(WB_HOME)))
            if len(leftovers) > 10:
                info(f"...另有 {len(leftovers) - 10} 项")
            info(c("  确认不需要后加 --clean-migrated 清理", C_DIM))

    # 6) 让账号级目录与快照严格对齐（删除快照中不存在的账号级文件）
    if prune_accounts:
        extra = prune_account_dirs(snap, dry=True)
        if extra:
            print()
            warn(
                f"--prune-accounts 将删除 {len(extra)} 项「快照生成之后才出现」的账号级文件。"
            )
            for p in extra[:10]:
                info(f"删除 {p.relative_to(WB_HOME)}")
            if len(extra) > 10:
                info(f"...另有 {len(extra) - 10} 项")
            prune_account_dirs(snap, dry=False)
            ok(f"账号级目录已对齐快照（清理 {len(extra)} 项）")

    print()
    ok(c("恢复完成。重新启动 WorkBuddy 即可看到数据。", C_OK))


def find_migrated_artifacts() -> list[Path]:
    """找出 adopt 遗留的 .migrated-* 中转项。"""
    out: list[Path] = []
    for base in (MEMORY_DIR, CONNECTORS_DIR, STORAGE_DIR):
        if not base.is_dir():
            continue
        try:
            for p in base.iterdir():
                if ".migrated-" in p.name:
                    out.append(p)
        except OSError:
            pass
    return out


def _account_scoped_roots() -> list[Path]:
    """列出所有账号级资产根路径（文件或目录）。"""
    roots: list[Path] = []
    if MEMORY_DIR.is_dir():
        for p in MEMORY_DIR.iterdir():
            stem = p.name.split("_memory.md")[0]
            if "_memory.md" in p.name and UUID_RE.match(stem):
                roots.append(p)
    if CONNECTORS_DIR.is_dir():
        for p in CONNECTORS_DIR.iterdir():
            if UUID_RE.match(p.name):
                roots.append(p)
    if STORAGE_DIR.is_dir():
        for p in STORAGE_DIR.iterdir():
            if p.name.startswith("user-"):
                roots.append(p)
    return roots


def prune_account_dirs(snap: Path, dry: bool = False) -> list[Path]:
    """让账号级目录与快照严格对齐：删除快照中不存在的账号级文件/目录。

    只作用于 memory/<uid>_memory.md*、connectors/<uid>/、storage/user-*，
    不碰其它任何位置。
    """
    acc_src = snap / "accounts"
    if not acc_src.is_dir():
        return []
    removed: list[Path] = []

    for root in _account_scoped_roots():
        rel_root = root.relative_to(WB_HOME)
        snap_root = acc_src / rel_root

        if not snap_root.exists():
            removed.append(root)
            if not dry:
                shutil.rmtree(root, ignore_errors=True) if root.is_dir() else root.unlink(
                    missing_ok=True
                )
            continue

        if not root.is_dir():
            continue

        for cur_root, dirs, files in os.walk(root, topdown=False):
            for name in files + dirs:
                lp = Path(cur_root) / name
                if lp in removed:
                    continue
                if not (acc_src / lp.relative_to(WB_HOME)).exists():
                    removed.append(lp)
                    if not dry:
                        if lp.is_dir():
                            shutil.rmtree(lp, ignore_errors=True)
                        else:
                            lp.unlink(missing_ok=True)
    return removed


# --------------------------------------------------------------------------
# 过户（adopt）
# --------------------------------------------------------------------------


def plan_adopt(src: str, dst: str) -> list[str]:
    """列出过户会做的所有改动。"""
    plan: list[str] = []
    if DB_PATH.exists():
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            s = con.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (src,)).fetchone()[0]
            a = con.execute(
                "SELECT COUNT(*) FROM automations WHERE owner_user_id=?", (src,)
            ).fetchone()[0]
            o = con.execute(
                "SELECT COUNT(*) FROM automation_delivery_outbox WHERE owner_user_id=?", (src,)
            ).fetchone()[0]
            con.close()
            plan.append(f"重挂 {s} 个任务/对话  sessions.user_id  {short(src)} → {short(dst)}")
            plan.append(f"重挂 {a} 个自动化    automations.owner_user_id")
            if o:
                plan.append(f"重挂 {o} 条待投递消息 automation_delivery_outbox.owner_user_id")
        except sqlite3.Error as e:
            plan.append(f"(数据库读取失败：{e})")

    src_mem = MEMORY_DIR / f"{src}_memory.md"
    if src_mem.exists():
        plan.append(f"合并长期记忆    memory/{short(src)}_memory.md → {short(dst)}_memory.md")

    if (CONNECTORS_DIR / src).exists():
        exists = (CONNECTORS_DIR / dst).exists()
        plan.append(
            f"连接器授权      connectors/{short(src)} → connectors/{short(dst)}"
            + ("（目标已存在，做合并）" if exists else "（整目录复制）")
        )

    for pat in ("storage/user-{uid}-personal", "storage/user-{uid}"):
        if (WB_HOME / pat.format(uid=src)).exists():
            plan.append(f"账号个人存储    {pat.format(uid=short(src))} → {pat.format(uid=short(dst))}")

    try:
        cfg = json.loads(SETTINGS_PATH.read_text("utf-8"))
        users = (cfg.get("claw") or {}).get("users", {})
        if src in users:
            plan.append(f"渠道绑定        settings.json claw.users[{short(src)}] → [{short(dst)}]")
    except Exception:
        pass

    plan.append("设备级数据（workspaces / skills / MCP / 模型配置）本身跨账号共享，无需改动")
    return plan


def do_adopt(src: str, dst: str, yes: bool, force: bool = False) -> None:
    if src == dst:
        raise SystemExit("源账号与目标账号相同，无需过户")

    head(f"过户 {short(src)} → {short(dst)}")
    info(f"源账号  {src}")
    info(f"目标账号 {dst}")

    plan = plan_adopt(src, dst)
    print()
    print(c("  将要执行：", C_BOLD))
    for i, line in enumerate(plan, 1):
        print(f"    {i:>2}. {line}")

    if not yes:
        print()
        warn("当前为演练模式，未做任何改动。")
        info("确认无误后加 --yes 真正执行：")
        info(c(f"  python3 {Path(__file__).name} adopt --from {src} --to {dst} --yes", C_DIM))
        return

    require_app_closed(force)

    guard = make_snapshot(
        f"pre-adopt-{short(src)}-to-{short(dst)}",
        DEFAULT_BACKUP_ROOT,
        quiet=False,
        slim=True,
    )
    ledger: dict[str, Any] = {
        "op": "adopt",
        "src": src,
        "dst": dst,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "guard": str(guard),
        "db_tables": list(DIFF_TABLES),
        "moved": [],
        "created": [],
        "note": "回滚：restore <guard 快照名> --yes --prune-accounts --clean-migrated",
    }
    warn(f"过户前状态已备份到 {guard}")
    info(c(f"  反悔命令：python3 {Path(__file__).name} restore {guard.name} --yes --prune-accounts --clean-migrated", C_DIM))

    print()
    # 1) 数据库
    if DB_PATH.exists():
        n1 = db_write("UPDATE sessions SET user_id=? WHERE user_id=?", (dst, src))
        ok(f"sessions: {n1} 行已过户")
        n2 = db_write(
            "UPDATE automations SET owner_user_id=?, owner_status='confirmed' WHERE owner_user_id=?",
            (dst, src),
        )
        ok(f"automations: {n2} 行已过户")
        n3 = db_write(
            "UPDATE automation_delivery_outbox SET owner_user_id=? WHERE owner_user_id=?",
            (dst, src),
        )
        if n3:
            ok(f"automation_delivery_outbox: {n3} 行已过户")

    # 2) 长期记忆：合并而非覆盖
    src_mem = MEMORY_DIR / f"{src}_memory.md"
    dst_mem = MEMORY_DIR / f"{dst}_memory.md"
    if src_mem.exists():
        a = extract_memory_block(dst_mem)
        b = extract_memory_block(src_mem)
        merged = merge_memory_blocks(a, b)
        write_memory_file(dst_mem, merged, dst)
        ok(f"长期记忆已合并（{len(merged)} 字符）")
        keep_name = f"{src_mem.name}.migrated-{int(time.time())}"
        src_mem.rename(src_mem.with_name(keep_name))
        ledger["moved"].append(
            [str(Path("memory") / keep_name), str(Path("memory") / src_mem.name)]
        )
        warn(f"源账号记忆文件已改名保留（{keep_name}），确认无误后可自行删除")

    # 3) 连接器授权
    s_con = CONNECTORS_DIR / src
    d_con = CONNECTORS_DIR / dst
    if s_con.exists():
        if not d_con.exists():
            shutil.copytree(s_con, d_con, symlinks=True)
            ok("连接器授权目录已复制")
        else:
            merged = 0
            for f in s_con.iterdir():
                if f.name in (".master.key",):
                    continue  # 密钥不合并，保留目标的
                t = d_con / f.name
                if f.name == "connector-states.json" and t.exists():
                    try:
                        a = json.loads(t.read_text("utf-8") or "{}")
                        b = json.loads(f.read_text("utf-8") or "{}")
                        if isinstance(a, dict) and isinstance(b, dict):
                            for k, v in b.items():
                                a.setdefault(k, v)
                            t.write_text(json.dumps(a, ensure_ascii=False, indent=2), "utf-8")
                            merged += 1
                    except Exception:
                        shutil.copy2(f, t)
                        merged += 1
                elif not t.exists():
                    shutil.copy2(f, t)
                    merged += 1
            ok(f"连接器状态已合并（{merged} 个文件）")
        keep = CONNECTORS_DIR / f".migrated-{short(src)}-{int(time.time())}"
        shutil.move(str(s_con), str(keep))
        ledger["moved"].append(
            [str(keep.relative_to(WB_HOME)), str(s_con.relative_to(WB_HOME))]
        )
        warn(f"源连接器目录已移存至 {keep.name}")
        info("  注意：加密凭据与新账号的密钥不通用，个别连接器可能需要重新授权。")

    # 4) 账号个人存储
    for pat in ("storage/user-{uid}-personal", "storage/user-{uid}"):
        s_dir = WB_HOME / pat.format(uid=src)
        d_dir = WB_HOME / pat.format(uid=dst)
        if not s_dir.exists():
            continue
        existed = d_dir.exists()
        if not existed:
            shutil.copytree(s_dir, d_dir, symlinks=True)
        else:
            shutil.copytree(s_dir, d_dir, dirs_exist_ok=True, symlinks=True)
        keep = WB_HOME / f"{s_dir.name}.migrated-{int(time.time())}"
        shutil.move(str(s_dir), str(keep))
        ledger["moved"].append([str(keep.relative_to(WB_HOME)), str(s_dir.relative_to(WB_HOME))])
        if not existed:
            ledger["created"].append(str(d_dir.relative_to(WB_HOME)))
        ok(f"{d_dir.name} 已{'合并' if existed else '过户'}（源目录中转保留为 {keep.name}）")

    # 5) 渠道绑定
    try:
        cfg = json.loads(SETTINGS_PATH.read_text("utf-8"))
        claw = cfg.setdefault("claw", {})
        users = claw.setdefault("users", {})
        if src in users:
            tgt = users.setdefault(dst, {})
            src_ch = (users[src] or {}).get("channels") or {}
            tgt_ch = tgt.setdefault("channels", {})
            for k, v in src_ch.items():
                tgt_ch.setdefault(k, v)
            del users[src]
            shutil.copy2(SETTINGS_PATH, SETTINGS_PATH.with_suffix(".json.bak"))
            SETTINGS_PATH.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8"
            )
            ok("渠道绑定已并入目标账号")
    except Exception as e:
        warn(f"settings.json 渠道合并跳过：{e}")

    (guard / "adopt-ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), "utf-8"
    )

    print()
    ok(c("过户完成。重新启动 WorkBuddy，用目标账号登录即可看到原账号的全部数据。", C_OK))
    info(
        "如需反悔："
        f"python3 {Path(__file__).name} restore {guard.name} --yes --prune-accounts --clean-migrated"
    )
    info(c("（注意：本工具在 WorkBuddy 内部运行时会拒绝写操作，请退出客户端后用系统终端执行）", C_DIM))


# ==========================================================================
# 持续同步（live sync）—— 「统一数据池 + 自动跟随当前账号」
# ==========================================================================
#
# 原理
# ----
# WorkBuddy 各账号的数据按 user_id 分开存放，界面只展示当前账号的行。
# 持续同步做两件事，循环执行：
#   ① 吸（pull）：把所有账号的账号级资产并入一个「统一数据池」
#                 ~/.workbuddy-account-sync/pool/   —— 只增不删，永不丢数据
#   ② 回（push）：把池子内容回写到「当前登录账号」
#                 （数据库行 + 记忆 + 连接器 + 账号存储 + 渠道绑定）
# 这样无论你登录哪个账号，看到的都是全部数据。
#
# 触发时机
# --------
#   - 检测到当前账号变化（登录/切换）→ 立即同步
#   - 检测到客户端刚退出          → 立即同步（确定性窗口，最推荐）
#   - 每 N 秒定期校准             → 兜底，捕捉新产生的跨账号数据
#
# 与 restore/adopt 的区别：本模式**不做任何删除**，只做 upsert 式回写，
# 且允许在客户端运行中使用（SQLite WAL + 行级 UPDATE，不会整库覆盖）。

SYNC_ROOT = Path(os.environ.get("WB_SYNC_HOME", Path.home() / ".workbuddy-account-sync"))
POOL_DIR = SYNC_ROOT / "pool"
POOL_MEMORY = POOL_DIR / "memory"
POOL_CONNECTORS = POOL_DIR / "connectors"
POOL_STORAGE = POOL_DIR / "storage"
POOL_CLAW = POOL_DIR / "claw-users.json"
POOL_ACCOUNTS = POOL_DIR / "accounts.json"
STATE_PATH = SYNC_ROOT / "state.json"
LOG_PATH = SYNC_ROOT / "sync.log"
LOCK_PATH = SYNC_ROOT / ".lock"

AGENT_LABEL = "com.workbuddy.account-sync"
AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def log(msg: str, level: str = "INFO", echo: bool = False) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {level:<5} {msg}"
    try:
        SYNC_ROOT.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if echo:
        print(line)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def load_state() -> dict[str, Any]:
    return read_json(STATE_PATH, {}) or {}


def save_state(patch: dict[str, Any]) -> None:
    st = load_state()
    st.update(patch)
    write_json(STATE_PATH, st)


class AlreadyRunning(SystemExit):
    pass


def acquire_lock(blocking: bool = False):
    """单实例锁。返回持有的文件对象（需保持引用）。"""
    import fcntl

    SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


# ------------------------------ 池：吸 ------------------------------------


def pool_gather(verbose: bool = False) -> dict[str, Any]:
    """把所有账号的账号级资产并入统一池（只增不删）。"""
    accounts = discover_accounts()
    POOL_MEMORY.mkdir(parents=True, exist_ok=True)
    POOL_CONNECTORS.mkdir(parents=True, exist_ok=True)
    POOL_STORAGE.mkdir(parents=True, exist_ok=True)
    (POOL_CONNECTORS / "_accounts").mkdir(parents=True, exist_ok=True)
    (POOL_CONNECTORS / "_keys").mkdir(parents=True, exist_ok=True)

    # 1) 长期记忆：池 = 所有账号最新块的并集
    blocks: list[str] = []
    for uid in accounts:
        f = MEMORY_DIR / f"{uid}_memory.md"
        if not f.exists():
            continue
        blk = extract_memory_block(f)
        if not blk.strip():
            continue
        (POOL_MEMORY / f"{uid}.md").write_text(blk.strip() + "\n", "utf-8")
        blocks.append(blk)
    merged_prev = ""
    merged_file = POOL_MEMORY / "_merged.md"
    if merged_file.exists():
        merged_prev = merged_file.read_text("utf-8")
    merged = merge_memory_blocks(merged_prev, "\n".join(blocks))
    merged_file.write_text(merged + "\n" if merged else "", "utf-8")

    # 2) 连接器：状态按 key 并集；凭据与主密钥按账号留档
    states: dict[str, Any] = {}
    for uid in accounts:
        d = CONNECTORS_DIR / uid
        if not d.is_dir():
            continue
        st_file = d / "connector-states.json"
        if st_file.exists():
            st = read_json(st_file, {}) or {}
            if isinstance(st, dict):
                deep_fill(states, st)
        key = d / ".master.key"
        if key.exists():
            shutil.copy2(key, POOL_CONNECTORS / "_keys" / f"{uid}.key")
        cred = d / ".credentials.v3.json"
        acct_dir = POOL_CONNECTORS / "_accounts" / uid
        if cred.exists():
            acct_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cred, acct_dir / ".credentials.v3.json")
        mcp = d / "mcp.json"
        if mcp.exists():
            # mcp.json 取最新的一份作为池基线
            base = POOL_CONNECTORS / "mcp.json"
            if not base.exists() or mcp.stat().st_mtime > base.stat().st_mtime:
                shutil.copy2(mcp, base)

    if states:
        sp = POOL_CONNECTORS / "connector-states.json"
        old = read_json(sp, {}) or {}
        if isinstance(old, dict):
            deep_fill(old, states)
            states = old
        write_json(sp, states)

    # 3) 账号个人存储：目录树并集
    n_files = 0
    for uid in accounts:
        for pat, sub in (("storage/user-{uid}-personal", "personal"), ("storage/user-{uid}", "user")):
            src = WB_HOME / pat.format(uid=uid)
            if not src.is_dir():
                continue
            dst = POOL_STORAGE / sub
            dst.mkdir(parents=True, exist_ok=True)
            for root_, _dirs, files in os.walk(src):
                rel = Path(root_).relative_to(src)
                (dst / rel).mkdir(parents=True, exist_ok=True)
                for f in files:
                    s = Path(root_) / f
                    t = dst / rel / f
                    try:
                        if not s.is_symlink() and not t.exists():
                            shutil.copy2(s, t)
                            n_files += 1
                    except OSError:
                        pass

    # 4) 渠道绑定并集
    chans: dict[str, Any] = {}
    cfg = read_json(SETTINGS_PATH, {}) or {}
    users = ((cfg.get("claw") or {}).get("users") or {})
    for uid in list(users.keys()) + accounts:
        ch = (users.get(uid) or {}).get("channels") or {}
        for k, v in ch.items():
            cur = chans.get(k)
            # 已有的取「信息更全」的那份（字段数多者优先）
            if cur is None or len(json.dumps(v)) > len(json.dumps(cur)):
                chans[k] = v
    if chans:
        prev = read_json(POOL_CLAW, {}) or {}
        for k, v in (prev.get("channels") or {}).items():
            chans.setdefault(k, v)
    write_json(POOL_CLAW, {"channels": chans, "updated_at": int(time.time())})

    write_json(
        POOL_ACCOUNTS,
        {"accounts": accounts, "seen_at": int(time.time()), "current": current_uid()},
    )

    if verbose:
        log(f"吸池：{len(accounts)} 个账号，记忆 {len(merged)} 字，"
            f"连接器状态 {len(states)} 项，存储新增 {n_files} 文件", "POOL", True)
    return {"accounts": accounts, "memory_chars": len(merged), "states": len(states),
            "storage_files": n_files, "channels": len(chans)}


# ------------------------------ 池：回 ------------------------------------


def db_repoint(target: str, accounts: list[str], dry: bool = False) -> dict[str, int]:
    """把「本机已知账号」名下的行统一挂到 target 名下。不删除任何行。"""
    out = {"sessions": 0, "automations": 0, "outbox": 0}
    if not DB_PATH.exists() or not accounts:
        return out
    ph = ",".join("?" for _ in accounts)
    pairs = [
        ("sessions", "user_id", None),
        ("automations", "owner_user_id", "confirmed"),
        ("outbox", "owner_user_id", None),
    ]
    con = sqlite3.connect(str(DB_PATH), timeout=60)
    try:
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("PRAGMA foreign_keys=OFF")
        for table, col, extra_status in pairs:
            real = "automation_delivery_outbox" if table == "outbox" else table
            try:
                sql = (
                    f"UPDATE {real} SET {col}=?"
                    + (", owner_status=?" if extra_status else "")
                    + f" WHERE {col} IS NOT NULL AND {col}<>? AND {col} IN ({ph})"
                )
                params: tuple = (target,) + ((extra_status,) if extra_status else ()) + (target, *accounts)
                if dry:
                    cnt_sql = f"SELECT COUNT(*) FROM {real} WHERE {col} IS NOT NULL AND {col}<>? AND {col} IN ({ph})"
                    n = con.execute(cnt_sql, (target, *accounts)).fetchone()[0]
                else:
                    n = con.execute(sql, params).rowcount
                out[table] = n or 0
            except sqlite3.Error as e:
                log(f"数据库写入失败 {real}: {e}", "WARN")
        if not dry:
            con.commit()
    finally:
        con.close()
    return out


def deep_fill(dst: dict, src: dict) -> int:
    """把 src 里有、dst 里没有的字段补进 dst（递归），返回补充的叶子字段数。"""
    added = 0
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
            added += 1
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            added += deep_fill(dst[k], v)
    return added


def file_push(target: str, dry: bool = False) -> dict[str, int]:
    """把池子内容回写到当前账号的账号级文件。只做 upsert，不删除。"""
    res = {"memory": 0, "connector_files": 0, "storage_files": 0, "channels": 0}

    # 1) 长期记忆
    merged_file = POOL_MEMORY / "_merged.md"
    if merged_file.exists():
        block = merged_file.read_text("utf-8").strip()
        if block:
            if not dry:
                write_memory_file(MEMORY_DIR / f"{target}_memory.md", block, target)
            res["memory"] = len(block)

    # 2) 连接器
    dst_c = CONNECTORS_DIR / target
    if not dry:
        dst_c.mkdir(parents=True, exist_ok=True)
    for name in ("mcp.json", "connector-states.json"):
        s = POOL_CONNECTORS / name
        if not s.exists():
            continue
        t = dst_c / name
        if name == "connector-states.json":
            if t.exists():
                a = read_json(t, {}) or {}
                b = read_json(s, {}) or {}
                if isinstance(a, dict) and isinstance(b, dict):
                    deep_fill(a, b)
                    if not dry:
                        write_json(t, a)
                    continue
            if not dry:
                write_json(t, read_json(s, {}) or {})
            res["connector_files"] += 1
            continue
        # mcp.json：只在目标缺失时补一份，避免覆盖本账号的连接器开关
        if not t.exists():
            if not dry:
                dst_c.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, t)
            res["connector_files"] += 1

    # 凭据：当前账号没有凭据时，从池里挑一份带凭据的账号整包接过（含其主密钥）
    dst_cred = dst_c / ".credentials.v3.json"
    if not dst_cred.exists():
        cands = []
        for d in (POOL_CONNECTORS / "_accounts").iterdir() if (POOL_CONNECTORS / "_accounts").is_dir() else []:
            c = d / ".credentials.v3.json"
            if c.is_file():
                cands.append((c.stat().st_mtime, d.name, c))
        if cands:
            cands.sort(reverse=True)
            _, src_uid, cred = cands[0]
            src_key = POOL_CONNECTORS / "_keys" / f"{src_uid}.key"
            if not dry:
                dst_c.mkdir(parents=True, exist_ok=True)
                cur_key = dst_c / ".master.key"
                if cur_key.exists():
                    shutil.copy2(cur_key, dst_c / f".master.key.before-sync-{int(time.time())}")
                shutil.copy2(cred, dst_cred)
                if src_key.exists():
                    shutil.copy2(src_key, cur_key)
            res["connector_files"] += 1
            log(
                f"连接器凭据整包接过（来自 {short(src_uid)}，含主密钥）"
                + ("[dry]" if dry else ""),
                "PUSH",
                not dry,
            )

    # 3) 账号个人存储
    for sub, pat in (("personal", "storage/user-{uid}-personal"), ("user", "storage/user-{uid}")):
        src = POOL_STORAGE / sub
        if not src.is_dir():
            continue
        dst = WB_HOME / pat.format(uid=target)
        for root_, _dirs, files in os.walk(src):
            rel = Path(root_).relative_to(src)
            if not dry:
                (dst / rel).mkdir(parents=True, exist_ok=True)
            for f in files:
                s = Path(root_) / f
                t = dst / rel / f
                if t.exists() and t.stat().st_mtime >= s.stat().st_mtime:
                    continue
                if not dry:
                    (dst / rel).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s, t)
                res["storage_files"] += 1

    # 4) 渠道绑定
    pool_claw = read_json(POOL_CLAW, {}) or {}
    chans = pool_claw.get("channels") or {}
    if chans:
        cfg = read_json(SETTINGS_PATH, {}) or {}
        claw = cfg.setdefault("claw", {})
        users = claw.setdefault("users", {})
        ent = users.setdefault(target, {})
        tgt = ent.setdefault("channels", {})
        added = 0
        for k, v in chans.items():
            if k not in tgt:
                tgt[k] = v
                added += 1
            elif isinstance(v, dict) and isinstance(tgt[k], dict):
                added += deep_fill(tgt[k], v)
        if added and not dry:
            shutil.copy2(SETTINGS_PATH, SETTINGS_PATH.with_suffix(".json.bak"))
            write_json(SETTINGS_PATH, cfg)
        res["channels"] = added

    return res


def sync_once(
    target: str | None = None,
    dry: bool = False,
    verbose: bool = True,
    trigger: str = "手动",
) -> dict[str, Any]:
    """一轮完整同步：吸池 → 回写当前账号。"""
    uid = target or current_uid()
    if not uid:
        log("无法识别当前账号，跳过本轮", "WARN")
        return {"ok": False, "reason": "no_current_uid"}

    pulled = pool_gather(verbose=verbose and not dry)
    res = db_repoint(uid, pulled["accounts"] or [uid], dry=dry)
    files = file_push(uid, dry=dry)

    if not dry:
        save_state(
            {
                "last_sync_at": int(time.time()),
                "last_target": uid,
                "last_trigger": trigger,
                "pool_accounts": pulled["accounts"],
            }
        )
        log(
            f"同步({trigger}) → {short(uid)}：任务 {res['sessions']} 行、自动化 {res['automations']} 行、"
            f"待投递 {res['outbox']} 行；记忆 {files['memory']} 字、连接器 {files['connector_files']} 文件、"
            f"存储 {files['storage_files']} 文件、渠道 {files['channels']} 项",
            "SYNC",
        )
    return {"ok": True, "target": uid, "db": res, "files": files, "dry": dry}


def do_sync(args: argparse.Namespace) -> None:
    accounts = discover_accounts()
    target = resolve_uid(args.to, accounts) if args.to else current_uid()
    head("持续同步 —— 一轮全量同步" + ("（演练，不写盘）" if args.dry_run else ""))
    info(f"统一数据池   {POOL_DIR}")
    info(f"当前账号     {target or '未识别'}")
    info(f"本机账号     {', '.join(short(a) for a in accounts) or '无'}")

    if not args.dry_run:
        lk = acquire_lock()
        if lk is None:
            raise SystemExit("后台同步服务正在运行，请先 daemon-uninstall 或等待其完成")
    else:
        lk = None

    r = sync_once(target, dry=args.dry_run, trigger="手动")
    if not r.get("ok"):
        raise SystemExit(f"同步失败：{r.get('reason')}")

    print()
    ok(f"sessions            归到 {short(r['target'])}：{r['db']['sessions']} 行")
    ok(f"automations         归到 {short(r['target'])}：{r['db']['automations']} 行")
    if r["db"]["outbox"]:
        ok(f"待投递消息          {r['db']['outbox']} 行")
    ok(f"长期记忆            合并后 {r['files']['memory']} 字")
    ok(f"连接器 / 账号存储    {r['files']['connector_files']} / {r['files']['storage_files']} 个文件")
    ok(f"渠道绑定            新增 {r['files']['channels']} 项")
    print()
    if args.dry_run:
        warn("以上为演练结果，未写入任何数据。去掉 --dry-run 才会落盘。")
    else:
        ok(c("同步完成。重启 WorkBuddy 即可在任务列表看到全部历史。", C_OK))


# ------------------------------ 持续守护 ----------------------------------


def do_live(args: argparse.Namespace) -> None:
    lk = acquire_lock()
    if lk is None:
        log("已有同步实例在运行，本实例退出", "WARN")
        raise SystemExit(3)

    interval = max(1, args.interval)
    settle = max(1, args.settle)
    every = max(interval, args.reconcile_every)

    log(
        f"持续同步启动 interval={interval}s settle={settle} reconcile={every}s "
        f"dry={args.dry_run} pid={os.getpid()}",
        "BOOT",
    )
    if args.foreground:
        head("持续同步运行中（Ctrl-C 停止）")
        info(f"轮询间隔 {interval}s，账号变化确认 {settle} 次后同步，每 {every}s 兜底校准")
        info(f"日志 {LOG_PATH}")

    last_uid = None
    pending_uid = None
    pending_hits = 0
    app_was_running = bool(app_main_pids())
    last_reconcile = 0.0

    while True:
        try:
            uid = current_uid()
            running = bool(app_main_pids())
            now = time.time()
            trigger = None

            # A. 账号切换（连续 settle 次观测到同一新 uid 才认账，避免读到中间态）
            if uid and uid != last_uid:
                if uid == pending_uid:
                    pending_hits += 1
                else:
                    pending_uid, pending_hits = uid, 1
                if pending_hits >= settle or last_uid is None:
                    trigger = "账号切换"
            else:
                pending_uid, pending_hits = None, 0

            # B. 客户端刚退出 —— 最安全的确定性窗口
            if trigger is None and app_was_running and not running:
                trigger = "客户端退出"

            # C. 定期兜底校准
            if trigger is None and now - last_reconcile >= every:
                trigger = "定期校准"

            if trigger:
                r = sync_once(uid, dry=args.dry_run, verbose=False, trigger=trigger)
                if r.get("ok"):
                    last_uid = uid
                    pending_uid, pending_hits = None, 0
                last_reconcile = now

            app_was_running = running
            time.sleep(interval)
        except KeyboardInterrupt:
            log("收到中断，持续同步退出", "BOOT")
            if args.foreground:
                print()
                warn("已停止")
            return
        except Exception as e:  # 守护进程绝不因单轮异常退出
            log(f"本轮异常（已忽略，继续运行）：{type(e).__name__}: {e}", "ERROR")
            time.sleep(interval)


# ------------------------------ launchd 托管 ------------------------------


def agent_installed() -> bool:
    return AGENT_PLIST.exists()


def agent_loaded() -> bool:
    try:
        p = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{AGENT_LABEL}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return p.returncode == 0
    except Exception:
        return False


def agent_python() -> str:
    exe = sys.executable
    if exe and Path(exe).exists() and "python" in Path(exe).name.lower():
        return exe
    return shutil.which("python3") or "/usr/bin/python3"


def do_daemon_install(args: argparse.Namespace) -> None:
    head("安装持续同步后台服务（launchd LaunchAgent）")
    py = agent_python()
    script = Path(__file__).resolve()
    SYNC_ROOT.mkdir(parents=True, exist_ok=True)

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>{script}</string>
    <string>live</string>
    <string>--interval</string><string>{args.interval}</string>
    <string>--settle</string><string>{args.settle}</string>
    <string>--reconcile-every</string><string>{args.reconcile_every}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{SYNC_ROOT}/agent.out.log</string>
  <key>StandardErrorPath</key><string>{SYNC_ROOT}/agent.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WB_SYNC_HOME</key><string>{SYNC_ROOT}</string>
  </dict>
</dict>
</plist>
"""
    info(f"解释器   {py}")
    info(f"脚本     {script}")
    info(f"配置     {AGENT_PLIST}")
    if args.dry_run:
        print()
        print(plist)
        warn("演练模式，未写入。去掉 --dry-run 才会真正安装。")
        return

    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.write_text(plist, "utf-8")
    ok("plist 已写入")

    # 先跑首轮同步（此时服务还没启动，不会与之争抢）
    if not args.no_first_sync:
        r = sync_once(None, dry=False, verbose=True, trigger="安装首跑")
        ok(f"首轮同步完成（任务 {r['db']['sessions']} 行归到 {short(r.get('target') or '')}）")

    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                   capture_output=True, text=True)
    p = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(AGENT_PLIST)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        p2 = subprocess.run(["launchctl", "load", "-w", str(AGENT_PLIST)],
                            capture_output=True, text=True)
        if p2.returncode != 0:
            err(f"加载失败：{p.stderr.strip() or p2.stderr.strip()}")
            raise SystemExit(1)
    ok("后台服务已加载")

    print()
    ok(c("持续同步已开启：登录/切换账号后会自动把全部账号数据带过来。", C_OK))
    info("查看状态： ./wb-account-sync.sh daemon-status")
    info("查看日志： ./wb-account-sync.sh daemon-log")


def do_daemon_uninstall(args: argparse.Namespace) -> None:
    head("卸载持续同步后台服务")
    p = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                       capture_output=True, text=True)
    if p.returncode == 0:
        ok("服务已停止")
    else:
        subprocess.run(["launchctl", "unload", "-w", str(AGENT_PLIST)],
                       capture_output=True, text=True)
    if AGENT_PLIST.exists():
        AGENT_PLIST.unlink()
        ok("plist 已移除")
    else:
        info("未发现 plist")
    print()
    ok("持续同步已关闭（数据池与历史日志保留在 " + str(SYNC_ROOT) + "）")


def do_daemon_status(args: argparse.Namespace) -> None:
    head("持续同步状态")
    st = load_state()
    info(f"plist      {'已安装' if agent_installed() else '未安装'}  {AGENT_PLIST}")
    info(f"服务       {'运行中' if agent_loaded() else '未运行'}")
    if st.get("last_sync_at"):
        info(f"上次同步   {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st['last_sync_at']))}"
             f"  触发 {st.get('last_trigger', '-')}  目标 {short(st.get('last_target', ''))}")
    info(f"当前账号   {short(current_uid() or '') or '未识别'}")
    info(f"数据池     {POOL_DIR}")
    accounts = discover_accounts()
    info(f"池内账号   {', '.join(short(a) for a in accounts) or '无'}")
    merged = POOL_MEMORY / "_merged.md"
    if merged.exists():
        info(f"池内记忆   {len(merged.read_text('utf-8'))} 字")
    if agent_installed():
        p = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{AGENT_LABEL}"],
            capture_output=True, text=True,
        )
        for line in p.stdout.splitlines():
            if any(k in line for k in ("state =", "pid =", "last exit code")):
                print(f"    {line.strip()}")
    print()
    if not agent_installed():
        warn("尚未安装后台服务：./wb-account-sync.sh daemon-install")


def do_daemon_log(args: argparse.Namespace) -> None:
    n = max(1, args.lines)
    head(f"持续同步日志（最后 {n} 行）")
    for p in (LOG_PATH, SYNC_ROOT / "agent.err.log"):
        if not p.exists():
            continue
        info(f"{p}")
        content = p.read_text("utf-8", errors="replace").splitlines()[-n:]
        for line in content:
            print(f"    {line}")
        print()


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def do_status() -> None:
    accounts = discover_accounts()
    cur = current_uid()

    head("WorkBuddy 账号数据现状")
    info(f"数据目录  {WB_HOME}")
    info(f"客户端版本 {read_wb_version()}")
    if not DB_PATH.exists():
        warn("未找到 workbuddy.db")
    info(f"当前登录账号 {cur or '未识别'}")

    if not accounts:
        warn("未发现任何账号数据")
        return

    head("账号资产一览")
    hdr = (
        f"    {'账号':<40}{'任务':>6}{'工作区':>7}{'自动化':>9}{'运行':>6}"
        f"{'记忆(字)':>9}{'连接器':>10}{'存储':>9}  最近活动"
    )
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    for uid in accounts:
        s = account_stats(uid)
        mark = c(" ←当前", C_OK) if uid == cur else ""
        chans = f"  [{','.join(s['claw_channels'])}]" if s["claw_channels"] else ""
        auto = f"{s['automations_active']}/{s['automations']}"
        print(
            f"    {uid:<40}{s['sessions']:>6}{s['workspaces']:>7}"
            f"{auto:>9}{s['runs']:>6}{s['memory']:>9}"
            f"{human(s['connectors']):>10}{human(s['storage']):>9}"
            f"  {fmt_ts(s['sessions_last'])}{mark}{chans}"
        )
    print(c("    自动化列 = 生效数/总行数（含已删除的残留行）", C_DIM))

    # 共享层说明
    head("跨账号天然共享（无需处理）")
    for label, path in (
        ("工作区列表   workspaces", DB_PATH),
        ("技能包       skills", WB_HOME / "skills"),
        ("MCP 配置     mcp.json", WB_HOME / "mcp.json"),
        ("模型配置     models.json", WB_HOME / "models.json"),
        ("外观/插件    settings.json", SETTINGS_PATH),
    ):
        size = human(du(path)) if path.exists() else "缺失"
        print(f"    {label:<28}{size:>10}")

    head("会话内容资产（设备级，不随账号）")
    for d in DEVICE_DIRS:
        p = WB_HOME / d
        if p.is_dir():
            print(f"    {d:<24}{human(du(p)):>10}")

    head("快照")
    snaps = list_snapshots(DEFAULT_BACKUP_ROOT)
    if not snaps:
        info(f"暂无（目录 {DEFAULT_BACKUP_ROOT}）")
    else:
        for s in snaps[:8]:
            m = s["manifest"]
            print(
                f"    {s['path'].name:<44}{m.get('created_at', '')[:19]}"
                f"  {len(m.get('accounts', []))} 账号  {m.get('label') or '-'}"
            )

    print()
    if len(accounts) > 1:
        warn(
            f"共 {len(accounts)} 个账号的数据并存于本机。"
            "它们互不可见；想让当前账号看到别的账号数据，用："
        )
        info("  adopt --from <源账号> --to current --yes")
    print(c("  提示：任何写操作前请先完全退出 WorkBuddy（⌘Q）。", C_DIM))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wb-account-sync",
        description="WorkBuddy 跨账号数据保留 / 备份 / 过户工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  %(prog)s status\n"
            "  %(prog)s backup --label before-switch\n"
            "  %(prog)s adopt --from a1b2c3d4 --to current\n"
            "  %(prog)s adopt --from a1b2c3d4 --to current --yes\n"
            "  %(prog)s snapshots\n"
            "  %(prog)s restore latest\n"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="列出账号与资产现状")

    sp = sub.add_parser("backup", help="生成全量快照")
    sp.add_argument("--label", "-l", default="", help="快照标签，如 before-switch")
    sp.add_argument("--out", "-o", default="", help=f"快照根目录（默认 {DEFAULT_BACKUP_ROOT}）")
    sp.add_argument(
        "--slim", action="store_true", help="精简模式：只备份数据库与账号级资产，跳过 tasks/traces 等大目录"
    )

    sp = sub.add_parser("snapshots", help="列出已有快照")
    sp.add_argument("--out", "-o", default="", help="快照根目录")

    sp = sub.add_parser("restore", help="从快照恢复")
    sp.add_argument("target", nargs="?", default="latest", help="快照名 / 目录 / latest")
    sp.add_argument("--out", "-o", default="", help="快照根目录")
    sp.add_argument("--yes", action="store_true", help="跳过二次确认")
    sp.add_argument("--force", action="store_true", help="跳过运行中检查（危险）")
    sp.add_argument(
        "--prune", action="store_true", help="同时删除快照中不存在的会话/自动化行"
    )
    sp.add_argument(
        "--prune-accounts", action="store_true", help="让账号级目录严格对齐快照（清理多余文件）"
    )
    sp.add_argument(
        "--clean-migrated", action="store_true", help="删除 adopt 遗留的 .migrated-* 中转项"
    )
    sp.add_argument(
        "--overwrite-db",
        action="store_true",
        help="整库覆盖还原（默认走行级 diff；仅在已退出客户端时使用）",
    )

    sp = sub.add_parser("adopt", help="把源账号的账号级资产过户给目标账号")
    sp.add_argument("--from", dest="src", required=True, help="源账号 uid 或前缀，或 all")
    sp.add_argument("--to", dest="dst", default="current", help="目标账号 uid / 前缀 / current")
    sp.add_argument("--yes", action="store_true", help="真正执行（默认只演练）")
    sp.add_argument("--force", action="store_true", help="跳过运行中检查（危险）")

    sp = sub.add_parser("revert", help="撤销最近一次 adopt（恢复 pre-adopt 快照）")
    sp.add_argument("--snapshot", "-s", default="", help="指定 pre-adopt 快照名，默认取最近一次")
    sp.add_argument("--yes", action="store_true", help="真正执行")
    sp.add_argument("--force", action="store_true", help="跳过运行中检查（危险）")

    # ---- 持续同步 ----
    sp = sub.add_parser("sync", help="[持续同步] 执行一轮全量同步（吸池 + 回写当前账号）")
    sp.add_argument("--to", default="", help="指定目标账号（默认当前登录账号）")
    sp.add_argument("--dry-run", action="store_true", help="只统计不写盘")

    sp = sub.add_parser("live", help="[持续同步] 前台常驻进程，账号一变就同步")
    sp.add_argument("--interval", type=int, default=5, help="轮询间隔秒（默认 5）")
    sp.add_argument("--settle", type=int, default=2, help="账号变化连续确认次数（默认 2）")
    sp.add_argument("--reconcile-every", type=int, default=300, help="兜底校准间隔秒（默认 300）")
    sp.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    sp.add_argument("--foreground", action="store_true", help="打印运行提示")

    sp = sub.add_parser("daemon-install", help="[持续同步] 安装 launchd 后台服务（开机自启 + 常驻）")
    sp.add_argument("--interval", type=int, default=5, help="轮询间隔秒（默认 5）")
    sp.add_argument("--settle", type=int, default=2, help="账号变化连续确认次数（默认 2）")
    sp.add_argument("--reconcile-every", type=int, default=300, help="兜底校准间隔秒（默认 300）")
    sp.add_argument("--dry-run", action="store_true", help="只打印 plist，不安装")
    sp.add_argument("--no-first-sync", action="store_true", help="安装后不立即跑首轮同步")

    sub.add_parser("daemon-uninstall", help="[持续同步] 卸载 launchd 后台服务")
    sub.add_parser("daemon-status", help="[持续同步] 查看后台服务与数据池状态")

    sp = sub.add_parser("daemon-log", help="[持续同步] 查看同步日志")
    sp.add_argument("--lines", "-n", type=int, default=40, help="显示行数（默认 40）")

    return p


def main() -> None:
    args = build_parser().parse_args()

    if args.cmd == "status":
        do_status()
        return

    if args.cmd == "backup":
        accounts = discover_accounts()
        if not accounts:
            err("未发现任何账号数据，无需备份")
            raise SystemExit(1)
        root = make_snapshot(args.label or None, snapshot_root(args.out), slim=args.slim)
        print()
        ok(f"共 {len(accounts)} 个账号、{len(list_snapshots(root.parent))} 份快照")
        info(str(root))
        return

    if args.cmd == "snapshots":
        root = snapshot_root(args.out)
        snaps = list_snapshots(root)
        head(f"快照列表（{root}）")
        if not snaps:
            info("暂无快照")
            return
        for s in snaps:
            m = s["manifest"]
            size = du(s["path"])
            print(
                f"    {s['path'].name}\n"
                f"      {m.get('created_at', '')[:19]}  "
                f"标签 {m.get('label') or '-'}  "
                f"{len(m.get('accounts', []))} 账号  "
                f"{m.get('has_db') and '含DB' or '无DB'}  {human(size)}"
            )
        return

    if args.cmd == "restore":
        snap = resolve_snapshot(args.target, snapshot_root(args.out))
        if not args.yes:
            mf = json.loads((snap / "manifest.json").read_text("utf-8"))
            head(f"将恢复快照 {snap.name}")
            info(f"创建时间 {mf.get('created_at')}  标签 {mf.get('label') or '-'}")
            info(f"账号 {', '.join(short(a) for a in mf.get('accounts', []))}")
            print()
            warn("恢复会用快照内容回写数据库与账号级文件（默认行级 diff，不删新数据）。")
            info("执行前会自动为当前状态生成一份 pre-restore 快照。")
            info("想彻底对齐快照可加 --prune --prune-accounts --clean-migrated。")
            info(c(f"确认后加 --yes：python3 {Path(__file__).name} restore {snap.name} --yes", C_DIM))
            return
        do_restore(
            snap,
            prune=args.prune,
            overwrite_db=args.overwrite_db,
            clean_migrated=args.clean_migrated,
            prune_accounts=args.prune_accounts,
            force=args.force,
        )
        return

    if args.cmd == "adopt":
        accounts = discover_accounts()
        dst = resolve_uid(args.dst, accounts)
        if args.src in ("all", "*"):
            srcs = [a for a in accounts if a != dst]
            if not srcs:
                raise SystemExit("没有其他账号可供过户")
            for s in srcs:
                do_adopt(s, dst, args.yes, args.force)
            return
        src = resolve_uid(args.src, accounts)
        do_adopt(src, dst, args.yes, args.force)
        return

    if args.cmd == "revert":
        if args.snapshot:
            snap = resolve_snapshot(args.snapshot, DEFAULT_BACKUP_ROOT)
        else:
            cand = [
                s["path"]
                for s in list_snapshots(DEFAULT_BACKUP_ROOT)
                if "-pre-adopt-" in s["path"].name
            ]
            if not cand:
                raise SystemExit("找不到 pre-adopt 快照，请用 --snapshot 指定")
            snap = cand[0]
        if not args.yes:
            head(f"将撤销过户（恢复 {snap.name}）")
            info("会回写数据库行级差异、还原账号级文件、并清理 .migrated-* 中转项。")
            info(c(f"确认后加 --yes 执行", C_DIM))
            return
        do_restore(
            snap,
            prune=False,
            overwrite_db=False,
            clean_migrated=True,
            prune_accounts=True,
            force=args.force,
        )
        return

    if args.cmd == "sync":
        do_sync(args)
        return

    if args.cmd == "live":
        do_live(args)
        return

    if args.cmd == "daemon-install":
        do_daemon_install(args)
        return

    if args.cmd == "daemon-uninstall":
        do_daemon_uninstall(args)
        return

    if args.cmd == "daemon-status":
        do_daemon_status(args)
        return

    if args.cmd == "daemon-log":
        do_daemon_log(args)
        return

    raise SystemExit("未知命令")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        warn("已中断")
        sys.exit(130)
