#!/usr/bin/env python3
"""账号识别探针（只读）。

目标：在不联机、不写任何数据的前提下，回答两个问题——
  1. 当前登录的是哪个账号？（uid / 昵称 / 版本 / 是否 Pro）
  2. 能不能拿到积分？（本地能算「已消费」，拿不到「剩余余额」）

用法：
    python3 tools/acct_probe.py                # 人读摘要
    python3 tools/acct_probe.py --json         # 机器可读
    python3 tools/acct_probe.py --home ~/.workbuddy
    python3 tools/acct_probe.py --no-logs      # 跳过日志扫描（更快）

读得到什么、读不到什么，见 ``docs/ACCOUNTS.md``。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_HOMES: list[tuple[str, Path]] = [
    ("WorkBuddy", Path("~/.workbuddy")),
    ("WorkBuddy AI", Path("~/.workbuddy-ai")),
]

# storage/ 下这些前缀不是账号目录，别当 uid 处理
NON_ACCOUNT_DIRS = {"skeleton", "device", "default", "skills"}

# ---------------------------------------------------------------------------
# 日志扫描（昵称补全）
# ---------------------------------------------------------------------------
# 历史账号的昵称在磁盘上没有权威留存：``storage/skeleton/account-snapshot.json``
# 只写当前账号，切换过的旧账号只剩 uid。但渲染进程日志会把 account 对象
# 整个 URL 编码后打进去（形如 ``%2522uid%2522%253A%2522...``），解码两遍
# 就能把 uid / nickname / editionType 还原出来。
#
# 这份数据是**尽力而为**：只用来给旧账号补个可读名字，绝不用它覆盖
# account-snapshot 给出的当前账号权威值。

_LOG_UID_RE = re.compile(r'"uid"\s*:\s*"([0-9a-fA-F-]{36})"')
_LOG_WINDOW = 700          # 从 "uid" 起往后看多少字符，够覆盖同一对象的所有字段
_LOG_SLICE = 512 * 1024    # 每个日志读头部这么多 + 尾部同样多
_LOG_FILE_LIMIT = 250     # 最多扫多少个日志文件
_LOG_PER_DIR = 20         # 每个日期目录最多取几个文件，防止某一天挤光名额
_LOG_BYTE_BUDGET = 120 * 1024 * 1024   # 全部日志的读取总预算，超了就停
_LOG_MAX_AGE_DAYS = 30    # 更早的日志不看

# 从日志里能补的字段，以及它们**不允许**覆盖已有值的约束由调用方掌握
_LOG_FIELDS = ("nickname", "type", "editionType", "isPro", "isAdmin")


def _iter_log_files(home: Path) -> list[Path]:
    """挑出值得扫的日志文件。

    不能简单地"按 mtime 取最新 N 个"——今天的诊断日志就能占满全部名额，
    而账号名往往只留在几天前的会话日志里。所以按来源分层采样：
    顶层主日志全要，**每个日期目录各自**取最新的若干个。
    """
    logs = home / "logs"
    if not logs.is_dir():
        return []
    cutoff = datetime.now().timestamp() - _LOG_MAX_AGE_DAYS * 86400
    picked: list[tuple[float, Path]] = []

    def add(paths: list[Path], limit: int | None = None) -> None:
        fresh: list[tuple[float, Path]] = []
        for path in paths:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff and path.is_file():
                fresh.append((mtime, path))
        fresh.sort(key=lambda item: -item[0])
        picked.extend(fresh if limit is None else fresh[:limit])

    # 顶层：main/daemon/renderer 这些主日志全收
    add(list(logs.glob("*.log")))
    for sub in sorted(logs.iterdir(), reverse=True):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        # 每个日期目录各自采样，避免某一天的文件把别的日期挤光
        add(list(sub.glob("*.log")), limit=_LOG_PER_DIR)
        add([p for p in sub.glob("*/*.log")], limit=_LOG_PER_DIR)
        add([p for p in sub.glob("*/conversations/*.log")], limit=_LOG_PER_DIR)

    picked.sort(key=lambda item: -item[0])
    return [path for _, path in picked[:_LOG_FILE_LIMIT]]


def _decode_log(raw: bytes) -> str:
    """把日志块还原成能直接用正则找 ``"uid"`` 的文本；不相关就返回空串。

    这里必须**先预筛再解码**：``urllib.parse.unquote`` 对 1 MB 的块跑两遍是
    毫秒级的开销，乘以上百个日志就很可观，而绝大多数日志里根本没有账号信息。
    """
    text = raw.decode("utf-8", "replace")
    if '"uid"' in text:
        return text                       # 少数日志是明文的
    if "uid%22" in text or "uid%2522" in text or "%22uid" in text:
        decoded = urllib.parse.unquote(urllib.parse.unquote(text))
        if '"uid"' in decoded:
            return decoded
        if '"uid"' in urllib.parse.unquote(text):
            return urllib.parse.unquote(text)
    return ""


def scan_logs(home: Path, report: HomeReport) -> None:
    """从日志里给历史账号补昵称与版本标识（尽力而为）。"""
    seen: dict[str, dict[str, object]] = {}
    budget = _LOG_BYTE_BUDGET
    for path in _iter_log_files(home):
        if budget <= 0:
            break
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                head = fh.read(min(_LOG_SLICE, budget))
                tail = b""
                if size > _LOG_SLICE:
                    want = min(_LOG_SLICE, budget - len(head))
                    if want > 0:
                        fh.seek(max(0, size - want))
                        tail = fh.read(want)
        except OSError:
            continue
        budget -= len(head) + len(tail)
        # 日志是追加写的，账号信息既可能在启动段（头部）也可能在最近一段（尾部）
        for raw in (head, tail) if tail else (head,):
            text = _decode_log(raw)
            if not text:
                continue
            for match in _LOG_UID_RE.finditer(text):
                uid = match.group(1).lower()
                blob = text[match.start() : match.start() + _LOG_WINDOW]
                fields: dict[str, object] = {}
                for key in _LOG_FIELDS:
                    # 注意花括号要写成 {{}}：这是 f-string，`{0,80}` 会被当成
                    # 替换字段求值成 "0" 而不是量词，字符串分支就永远匹配不上。
                    found = re.search(
                        rf'"{key}"\s*:\s*("([^"]{{0,80}})"|true|false)', blob
                    )
                    if not found:
                        continue
                    if found.group(2) is not None:
                        fields[key] = found.group(2)
                    else:
                        fields[key] = found.group(1) == "true"
                if fields:
                    seen.setdefault(uid, {}).update(fields)

    for uid, fields in seen.items():
        acct = report.account(uid)
        acct.sources.add("logs/")
        acct.from_logs = True
        # 只填空白，不覆盖 account-snapshot 的权威值
        if not acct.nickname and isinstance(fields.get("nickname"), str):
            acct.nickname = fields["nickname"]
        for key, attr in (("type", "account_type"), ("editionType", "edition")):
            if not getattr(acct, attr) and isinstance(fields.get(key), str):
                setattr(acct, attr, fields[key])
        for key, attr in (("isPro", "is_pro"), ("isAdmin", "is_admin")):
            if getattr(acct, attr) is None and isinstance(fields.get(key), bool):
                setattr(acct, attr, fields[key])



def _ts(ms: int | float | None) -> str:
    if not ms:
        return "-"
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "-"


def _age_text(ms: int | float | None) -> str:
    if not ms:
        return "-"
    delta = datetime.now().timestamp() - int(ms) / 1000
    if delta < 0:
        return "刚刚"
    for unit, span in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if delta >= span:
            return f"{int(delta // span)}{unit}前"
    return "刚刚"


def _day_key(ms: int | float | None) -> str | None:
    """毫秒时间戳 → 本地日期 ``YYYY-MM-DD``（趋势图按本地日聚合）。"""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


@dataclass
class Account:
    uid: str
    sources: set[str] = field(default_factory=set)
    nickname: str | None = None
    account_type: str | None = None
    edition: str | None = None
    is_pro: bool | None = None
    is_admin: bool | None = None
    enterprise_id: str | None = None
    sessions: int = 0
    automations: int = 0
    credits_used: float = 0.0
    credit_sessions: int = 0
    last_session_ms: int | None = None
    has_memory: bool = False
    has_connectors: bool = False
    has_personal_storage: bool = False
    channels: list[str] = field(default_factory=list)
    from_logs: bool = False


@dataclass
class HomeReport:
    name: str
    path: str
    exists: bool
    current_uid: str | None = None
    snapshot_age_ms: int | None = None
    accounts: dict[str, Account] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # 按天聚合的积分消耗：date -> {credits, sessions}
    trend: dict[str, dict[str, float]] = field(default_factory=lambda: defaultdict(dict))
    # session_id -> (uid, credits)。跨 home 打通会把同一个会话复制到两边，
    # 只是把 user_id 改写成目标账号，所以两个 home 的用量不能直接相加。
    usage_records: dict[str, tuple[str, float]] = field(default_factory=dict)

    def account(self, uid: str) -> Account:
        # uid 大小写在各来源里不统一，统一按小写归并，否则同一个账号会被拆成两条
        uid = str(uid).strip().lower()
        if uid not in self.accounts:
            self.accounts[uid] = Account(uid=uid)
        return self.accounts[uid]

    def add_usage(self, day: str, credits: float) -> None:
        bucket = self.trend.setdefault(day, {"credits": 0.0, "sessions": 0.0})
        bucket["credits"] += credits
        bucket["sessions"] += 1


def read_snapshot(home: Path) -> tuple[dict | None, str | None]:
    """当前账号的唯一权威来源：storage/skeleton/account-snapshot.json。"""
    path = home / "storage" / "skeleton" / "account-snapshot.json"
    if not path.exists():
        return None, f"缺少 {path}"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"解析失败: {exc}"
    primary = doc.get("primary")
    if not isinstance(primary, dict) or not primary.get("uid"):
        return None, "primary.uid 缺失"
    return primary, None


def scan_db(home: Path, report: HomeReport) -> None:
    db = home / "workbuddy.db"
    if not db.exists():
        report.errors.append(f"缺少 {db}")
        return
    uri = f"file:{db}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        report.errors.append(f"打开数据库失败: {exc}")
        return
    try:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT user_id, COUNT(*) AS n, MAX(updated_at) AS last "
            "FROM sessions WHERE user_id IS NOT NULL GROUP BY user_id"
        ):
            acct = report.account(row["user_id"])
            acct.sources.add("db.sessions")
            acct.sessions = row["n"]
            acct.last_session_ms = row["last"]
        for row in conn.execute(
            "SELECT owner_user_id AS uid, COUNT(*) AS n FROM automations "
            "WHERE owner_user_id IS NOT NULL GROUP BY owner_user_id"
        ):
            acct = report.account(row["uid"])
            acct.sources.add("db.automations")
            acct.automations = row["n"]
        # session_usage.credit_json：每次请求的积分消耗，按 session_id 挂靠账号
        rows = conn.execute(
            "SELECT u.session_id AS sid, s.user_id AS uid, u.credit_json AS cj, "
            "u.updated_at AS ts "
            "FROM session_usage u JOIN sessions s ON s.id = u.session_id "
            "WHERE u.credit_json IS NOT NULL AND s.user_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            try:
                usage = json.loads(row["cj"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(usage, dict):
                continue
            total = sum(v for v in usage.values() if isinstance(v, (int, float)))
            if total <= 0:
                continue
            acct = report.account(row["uid"])
            acct.sources.add("db.session_usage")
            acct.credits_used += total
            acct.credit_sessions += 1
            report.usage_records[row["sid"]] = (acct.uid, total)
            day = _day_key(row["ts"])
            if day:
                report.add_usage(day, total)
    except sqlite3.Error as exc:
        report.errors.append(f"查询失败: {exc}")
    finally:
        conn.close()


def scan_files(home: Path, report: HomeReport) -> None:
    memory_dir = home / "memory"
    if memory_dir.is_dir():
        for path in memory_dir.glob("*_memory.md"):
            uid = path.name[: -len("_memory.md")]
            acct = report.account(uid)
            acct.sources.add("memory/")
            acct.has_memory = True

    connectors_dir = home / "connectors"
    if connectors_dir.is_dir():
        for path in connectors_dir.iterdir():
            if not path.is_dir() or path.name in NON_ACCOUNT_DIRS:
                continue
            acct = report.account(path.name)
            acct.sources.add("connectors/")
            acct.has_connectors = True

    storage_dir = home / "storage"
    if storage_dir.is_dir():
        for path in storage_dir.iterdir():
            if not path.is_dir() or not path.name.startswith("user-"):
                continue
            rest = path.name[len("user-") :]
            uid = rest[: -len("-personal")] if rest.endswith("-personal") else rest
            acct = report.account(uid)
            acct.sources.add("storage/")
            acct.has_personal_storage = True

    settings = home / "settings.json"
    if settings.exists():
        try:
            doc = json.loads(settings.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            doc = {}
        users = (doc.get("claw") or {}).get("users") or {}
        if isinstance(users, dict):
            for uid, cfg in users.items():
                acct = report.account(uid)
                acct.sources.add("settings.claw")
                channels = (cfg or {}).get("channels") or {}
                if isinstance(channels, dict):
                    acct.channels = [
                        name for name, ch in channels.items() if (ch or {}).get("enabled")
                    ]


def build(home: Path | str, name: str, with_logs: bool = True) -> HomeReport:
    home = Path(home).expanduser()
    report = HomeReport(name=name, path=str(home), exists=home.is_dir())
    if not report.exists:
        return report
    snapshot, err = read_snapshot(home)
    if err:
        report.errors.append(err)
    if snapshot:
        report.current_uid = str(snapshot.get("uid", "")).strip().lower() or None
        report.snapshot_age_ms = snapshot.get("savedAt")
        acct = report.account(snapshot["uid"])
        acct.sources.add("account-snapshot")
        acct.nickname = snapshot.get("nickname")
        acct.account_type = snapshot.get("type")
        acct.edition = snapshot.get("editionType")
        acct.is_pro = snapshot.get("isPro")
        acct.is_admin = snapshot.get("isAdmin")
        acct.enterprise_id = snapshot.get("enterpriseId") or None
    scan_db(home, report)
    scan_files(home, report)
    if with_logs:
        # 放最后：日志只补空白，跑在 snapshot 之后才不会被覆盖
        scan_logs(home, report)
    return report


def trend_series(report: HomeReport, days: int = 30) -> list[dict[str, object]]:
    """补齐缺口后的按天序列，供前端画柱状图。"""
    today = datetime.now().date()
    series: list[dict[str, object]] = []
    for offset in range(days - 1, -1, -1):
        day = datetime.fromordinal(today.toordinal() - offset).strftime("%Y-%m-%d")
        bucket = report.trend.get(day) or {}
        series.append(
            {
                "date": day,
                "credits": round(float(bucket.get("credits") or 0.0), 2),
                "sessions": int(bucket.get("sessions") or 0),
            }
        )
    return series


def merge_reports(reports: list[HomeReport]) -> dict:
    """跨 home 去重视图。

    打通工具会把同一个会话复制进另一个 home（顺带把 ``user_id`` 改写成目标
    账号），于是同一个 ``session_id`` 会在两边各出现一次，用户归属还不一样。
    把两边的用量直接相加等于把同一笔消耗算了两遍——实测 49 条记录里有 45 条
    是这种重叠。所以这里按 ``session_id`` 去重，并把重叠规模一并报出来，
    让界面能同时给出"每个客户端各自"和"整机去重后"两个口径。
    """
    owners: dict[str, dict[str, Any]] = {}
    for report in reports:
        for sid, (uid, credits) in report.usage_records.items():
            slot = owners.get(sid)
            if slot is None:
                owners[sid] = {"uid": uid, "credits": credits, "homes": [report.name]}
            elif report.name not in slot["homes"]:
                slot["homes"].append(report.name)

    names: dict[str, str] = {}
    for report in reports:
        for uid, acct in report.accounts.items():
            if acct.nickname and uid not in names:
                names[uid] = acct.nickname

    by_uid: dict[str, dict[str, Any]] = {}
    for slot in owners.values():
        entry = by_uid.setdefault(
            slot["uid"],
            {
                "uid": slot["uid"],
                "uid_short": slot["uid"][:8],
                "nickname": names.get(slot["uid"]),
                "credits_used": 0.0,
                "sessions": 0,
                "homes": set(),
            },
        )
        entry["credits_used"] += slot["credits"]
        entry["sessions"] += 1
        entry["homes"].update(slot["homes"])

    accounts = sorted(by_uid.values(), key=lambda item: -item["credits_used"])
    for entry in accounts:
        entry["credits_used"] = round(entry["credits_used"], 2)
        entry["homes"] = sorted(entry["homes"])
    raw = sum(len(report.usage_records) for report in reports)
    return {
        "credits_used": round(sum(item["credits_used"] for item in accounts), 2),
        "sessions": len(owners),
        "raw_sessions": raw,
        "duplicated_sessions": raw - len(owners),
        "accounts": accounts,
    }


def to_dict(report: HomeReport, days: int = 30) -> dict:
    accounts = []
    for acct in sorted(report.accounts.values(), key=lambda a: -a.credits_used):
        accounts.append(
            {
                "uid": acct.uid,
                "uid_short": acct.uid[:8],
                "is_current": acct.uid == report.current_uid,
                "nickname": acct.nickname,
                "type": acct.account_type,
                "edition": acct.edition,
                "is_pro": acct.is_pro,
                "is_admin": acct.is_admin,
                "enterprise_id": acct.enterprise_id,
                "sessions": acct.sessions,
                "automations": acct.automations,
                "credits_used": round(acct.credits_used, 2),
                "credit_sessions": acct.credit_sessions,
                "last_session": _ts(acct.last_session_ms),
                "has_memory": acct.has_memory,
                "has_connectors": acct.has_connectors,
                "has_personal_storage": acct.has_personal_storage,
                "channels": acct.channels,
                "from_logs": acct.from_logs,
                "sources": sorted(acct.sources),
            }
        )
    trend = trend_series(report, days)
    recent = [point for point in trend if point["credits"]]
    return {
        "name": report.name,
        "path": report.path,
        "exists": report.exists,
        "current_uid": report.current_uid,
        "current_uid_short": (report.current_uid or "")[:8] or None,
        "snapshot_at": _ts(report.snapshot_age_ms),
        "snapshot_age": _age_text(report.snapshot_age_ms),
        "accounts": accounts,
        "trend": trend,
        "totals": {
            "accounts": len(accounts),
            "sessions": sum(a["sessions"] for a in accounts),
            "automations": sum(a["automations"] for a in accounts),
            "credits_used": round(sum(a["credits_used"] for a in accounts), 2),
            "active_days": len(recent),
            "today_credits": trend[-1]["credits"] if trend else 0.0,
        },
        "errors": report.errors,
    }


def render(reports: list[HomeReport], days: int = 14) -> None:
    for report in reports:
        print(f"\n{'=' * 68}\n{report.name}  ({report.path})\n{'=' * 68}")
        if not report.exists:
            print("  目录不存在")
            continue
        if report.current_uid:
            cur = report.account(report.current_uid)
            print(f"  当前账号 : {report.current_uid}")
            print(f"  昵称     : {cur.nickname or '-'}")
            print(f"  类型/版本: {cur.account_type or '-'} / {cur.edition or '-'}"
                  f"  Pro={cur.is_pro}  Admin={cur.is_admin}")
            print(f"  快照时间 : {_ts(report.snapshot_age_ms)} ({_age_text(report.snapshot_age_ms)})")
        else:
            print("  当前账号 : 未识别")
        print(f"  本机账号 : {len(report.accounts)} 个")
        print()
        header = f"  {'uid':10} {'当前':4} {'昵称':16} {'会话':>5} {'自动化':>6} {'累计积分':>10} {'最后活动':>14}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for acct in sorted(report.accounts.values(), key=lambda a: -a.credits_used):
            mark = " ★" if acct.uid == report.current_uid else ""
            nickname = (acct.nickname or "-")[:16]
            print(
                f"  {acct.uid[:8]:10} {mark:4} {nickname:16} "
                f"{acct.sessions:>5} {acct.automations:>6} "
                f"{acct.credits_used:>10.2f} {_age_text(acct.last_session_ms):>14}"
            )
        total = sum(a.credits_used for a in report.accounts.values())
        print(f"\n  本机可统计的累计积分消耗: {total:.2f}（不等于账户剩余余额）")
        recent = trend_series(report, days=days)
        active = [point for point in recent if point["credits"]]
        if active:
            print(f"  近 {days} 天有消耗的天数: {len(active)}")
            for point in active[-7:]:
                bar = "█" * max(1, min(28, int(point["credits"] / 200)))
                print(f"    {point['date']}  {point['credits']:>9.2f}  {bar}")
        if report.errors:
            for err in report.errors:
                print(f"  ! {err}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="账号识别探针（只读）")
    parser.add_argument("--home", action="append", default=None,
                        help="指定 home 目录，可重复；默认扫描两个客户端")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--no-logs", action="store_true",
                        help="跳过日志扫描（不补历史账号昵称，速度更快）")
    parser.add_argument("--days", type=int, default=30,
                        help="趋势序列回看天数，默认 30")
    args = parser.parse_args(argv)

    if args.home:
        homes = [(Path(p).name, Path(p)) for p in args.home]
    else:
        homes = [(name, path.expanduser()) for name, path in DEFAULT_HOMES]

    with_logs = not args.no_logs
    reports = [build(path, name, with_logs=with_logs) for name, path in homes]
    payload = [to_dict(r, days=max(1, min(args.days, 365))) for r in reports]

    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        render(reports, days=max(1, min(args.days, 365)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
