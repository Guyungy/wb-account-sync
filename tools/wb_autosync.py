#!/usr/bin/env python3
"""WorkBuddy 双 App 机会式自动同步（无人值守）。

设计前提（实测得出，不可绕过）：

* 两个客户端各持一份独立 SQLite（WAL），**外部写入在 SQLite 层面是安全的**；
  但客户端把会话列表缓存在内存里，外部插入的行它看不见，**必须重启客户端才可见**。
* 因此本工具只在**两个客户端都已退出**时才动手——这不是保守，而是唯一有意义的时机：
  客户端还开着的时候同步，等于同步完你看不到，还要重启。

launchd 每 ``--interval`` 秒唤起一次本脚本，流程：

1. 拿互斥锁。已有实例在跑 → 立刻退出（绝不并发写）。
2. 已暂停 → 立刻退出。
3. 检测两个客户端是否都已退出。有一个在跑 → 记录一行日志后退出，**不碰任何数据**。
4. 算一次「状态摘要」；与上次成功同步时相同 → 跳过（无变化），避免空转。
5. 生成计划。无待同步内容 → 跳过。
6. 先备份两个 ``workbuddy.db``，再执行写入。
   **人工确认被免去，但引擎的漂移拒绝仍然生效** —— apply 会重算计划并与 plan_id 比对，
   不一致直接拒绝，所以丢失的只是「我现在确实想跑」这一下点头，不是数据安全。
7. 核验；写运行日志与 ``status.json``；保留最近 ``--keep`` 次记录。

回滚：``wb_home_bridge.py restore --run-dir <state>/runs/<plan_id> --confirm <plan_id>``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any

VERSION = "0.1.0"

HERE = os.path.dirname(os.path.abspath(__file__))
# 复用同一套跨平台抽象，不在本项目里重复实现进程检测
sys.path.insert(0, HERE)
import wb_platform as wp  # noqa: E402

ENGINE = os.path.join(HERE, "wb_home_bridge.py")

LABEL = "com.workbuddy.home-bridge-autosync"
DEFAULT_STATE_ROOT = "~/.wb-home-bridge"
DEFAULT_INTERVAL = 120
DEFAULT_KEEP = 10
FULL_SWEEP_AFTER = 24 * 3600  # 距上次完整扫描超过此时长就强制跑一次（兜住只改资产的情况）
LOG_MAX_BYTES = 1_000_000

IS_MAC = sys.platform == "darwin"


class AutosyncError(Exception):
    pass


# --------------------------------------------------------------------------
# 解释器探测（与 tools/ui.command 同一套顺序）
# --------------------------------------------------------------------------


def find_python() -> str:
    """挑一个 ≥3.10 的解释器。绝对路径，launchd 环境下没有 PATH 可用。"""
    override = os.environ.get("WB_BRIDGE_PYTHON") or os.environ.get("WB_PYTHON")
    roots = [
        os.path.join(os.path.dirname(HERE), ".venv"),
        os.path.expanduser("~/.workbuddy"),
        os.path.expanduser("~/.workbuddy-ai"),
    ]
    cands: list[str] = []
    if override:
        cands.append(override)
    for root in roots:
        versions = os.path.join(root, "binaries", "python", "versions")
        if os.path.isdir(versions):
            for name in sorted(os.listdir(versions), reverse=True):
                cands.append(os.path.join(versions, name, "bin", "python3"))
    for p in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
        cands.append(p)
    for path in cands:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            try:
                out = subprocess.run(
                    [path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                    capture_output=True, text=True, timeout=10, check=False,
                ).stdout.strip()
                major, minor = (int(x) for x in out.split("."))
            except Exception:
                continue
            if (major, minor) >= (3, 10):
                return path
    raise AutosyncError(
        "找不到 Python ≥ 3.10。请设置 WB_BRIDGE_PYTHON 指向一个可用解释器。"
    )


def self_python() -> str:
    """优先复用「正在跑的」解释器；它已被验证可用。"""
    if sys.version_info >= (3, 10) and os.path.isabs(sys.executable):
        return sys.executable
    return find_python()


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------


def ensure_dir(path: str, mode: int = 0o700) -> str:
    """创建目录并确保权限。父目录也要显式 chmod —— makedirs 只对叶子生效。"""
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def state_root(args: argparse.Namespace) -> str:
    """状态根目录。引擎要求非空的状态目录必须是 0700，这里统一保证。"""
    return ensure_dir(os.path.expanduser(args.state_root))


def autosync_dir(args: argparse.Namespace) -> str:
    state_root(args)
    return ensure_dir(os.path.join(state_root(args), "autosync"))


def log_path(args: argparse.Namespace) -> str:
    return os.path.join(autosync_dir(args), "autosync.log")


def status_path(args: argparse.Namespace) -> str:
    return os.path.join(autosync_dir(args), "status.json")


def paused_path(args: argparse.Namespace) -> str:
    return os.path.join(autosync_dir(args), "PAUSED")


def lock_path(args: argparse.Namespace) -> str:
    return os.path.join(autosync_dir(args), "lock")


def plans_dir(args: argparse.Namespace) -> str:
    return ensure_dir(os.path.join(autosync_dir(args), "plans"))


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def describe_lock_conflict(args) -> str:
    try:
        with open(lock_path(args), "r", encoding="utf-8") as fh:
            info = json.load(fh)
        return f"另一个实例正在运行（pid {info.get('pid')}，自 {info.get('since')}）"
    except Exception:
        return "另一个实例正在运行"


def log(args: argparse.Namespace, message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}\n"
    path = log_path(args)
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    if not args.quiet:
        sys.stdout.write(line)
        sys.stdout.flush()


def read_status(args: argparse.Namespace) -> dict[str, Any]:
    try:
        with open(status_path(args), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_status(args: argparse.Namespace, **fields: Any) -> dict[str, Any]:
    data = read_status(args)
    data.update(fields)
    data["tool_version"] = VERSION
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    data["state_root"] = state_root(args)
    data["paused"] = os.path.exists(paused_path(args))
    data["installed"] = os.path.exists(plist_target())
    tmp = status_path(args) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, status_path(args))
    except OSError:
        pass
    return data


# --------------------------------------------------------------------------
# 互斥锁
# --------------------------------------------------------------------------


class RunLock:
    """跨平台互斥。并发写是这类工具最危险的失败模式，宁可拒绝也不排队。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+", encoding="utf-8")
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(json.dumps(
            {"pid": os.getpid(), "since": time.strftime("%Y-%m-%d %H:%M:%S")}
        ))
        self.fh.flush()
        return True

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None

    def __enter__(self) -> "RunLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# --------------------------------------------------------------------------
# 客户端状态与「有没有变化」
# --------------------------------------------------------------------------


def client_report() -> list[dict[str, Any]]:
    return wp.client_statuses()


def state_digest(home_a: str, home_b: str) -> str:
    """轻量「有没有变化」指纹。

    只覆盖三类高价值信号：会话 id 集合、技能名单、记忆文件内容。
    资产文件（tool-results 等）不在此列 —— 它们会在下一次真实变化触发完整扫描时被一并补齐。
    首次运行、或距上次完整扫描过久时，本函数不参与判断（直接跑完整流程）。
    """
    h = hashlib.sha256()
    for path in (home_a, home_b):
        db = os.path.join(path, wp.DB_NAME)
        ids: list[str] = []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                ids = sorted(
                    r[0] for r in con.execute(
                        "SELECT id FROM sessions WHERE deleted_at IS NULL"
                    )
                )
            finally:
                con.close()
        except sqlite3.Error:
            ids = ["<db-unreadable>"]
        h.update(("\n".join(ids)).encode("utf-8"))
        h.update(b"\x00")

        skills = os.path.join(path, "skills")
        names = sorted(os.listdir(skills)) if os.path.isdir(skills) else []
        h.update(("\n".join(names)).encode("utf-8"))
        h.update(b"\x00")

        mem_dir = os.path.join(path, "memory")
        if os.path.isdir(mem_dir):
            for name in sorted(os.listdir(mem_dir)):
                fp = os.path.join(mem_dir, name)
                if os.path.isfile(fp):
                    h.update(name.encode("utf-8"))
                    try:
                        with open(fp, "rb") as fh:
                            h.update(fh.read())
                    except OSError:
                        pass
        h.update(b"\x01")
    return h.hexdigest()


# --------------------------------------------------------------------------
# 调用引擎
# --------------------------------------------------------------------------


def run_engine(args: argparse.Namespace, argv: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    cmd = [self_python(), ENGINE, *argv]
    if args.home_a:
        cmd += ["--home-a", args.home_a]
    if args.home_b:
        cmd += ["--home-b", args.home_b]
    # 只有 plan / apply 接受这个开关（verify 没有）
    if argv[0] in ("plan", "apply") and getattr(args, "allow_client_running", False):
        cmd.append("--allow-client-running")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
    )


def notify(title: str, message: str) -> None:
    if not IS_MAC:
        return
    script = (
        f'display notification {json.dumps(message, ensure_ascii=False)} '
        f'with title {json.dumps(title, ensure_ascii=False)}'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15, check=False)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 备份与清理
# --------------------------------------------------------------------------


def backup_db(home_path: str, dest_dir: str) -> str:
    """用 sqlite 在线备份 API 复制 workbuddy.db（比文件拷贝安全，且不受 WAL 影响）。"""
    src = os.path.join(home_path, wp.DB_NAME)
    if not os.path.exists(src):
        return ""
    ensure_dir(dest_dir)
    name = os.path.basename(home_path.rstrip("/")) + ".db"
    out = os.path.join(dest_dir, name)
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        bck = sqlite3.connect(out)
        try:
            with bck:
                con.backup(bck)
        finally:
            bck.close()
    finally:
        con.close()
    os.chmod(out, 0o600)
    return out


def prune(args: argparse.Namespace) -> int:
    """只保留最近 --keep 次运行目录与计划文件，避免无人值守时无限堆积。"""
    removed = 0
    runs = os.path.join(state_root(args), "runs")
    if os.path.isdir(runs):
        items = [
            (os.path.getmtime(os.path.join(runs, n)), os.path.join(runs, n))
            for n in os.listdir(runs)
        ]
        items.sort(reverse=True)
        for _, path in items[args.keep:]:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    pd = plans_dir(args)
    files = [
        (os.path.getmtime(os.path.join(pd, n)), os.path.join(pd, n))
        for n in os.listdir(pd)
    ]
    files.sort(reverse=True)
    for _, path in files[args.keep:]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# 核心：一次机会式同步
# --------------------------------------------------------------------------


def run_once(args: argparse.Namespace) -> int:
    started = time.time()
    with RunLock(lock_path(args)) as lock:
        if not lock.acquire():
            log(args, f"跳过：{describe_lock_conflict(args)}。")
            return 0
        return _run_locked(args, started)


def _run_locked(args: argparse.Namespace, started: float) -> int:
    if os.path.exists(paused_path(args)):
        log(args, "跳过：自动同步处于暂停状态（resume 恢复）。")
        write_status(args, last_result="paused")
        return 0

    # 1) 客户端必须都已退出
    try:
        statuses = client_report()
    except wp.PlatformError as exc:
        log(args, f"跳过：进程探测失败（{exc}）。探测失败时不假装安全。")
        write_status(args, last_result="unknown", last_note="进程探测失败")
        return 0

    running = [s["display"] for s in statuses if s["running"]]
    if running and not args.allow_client_running:
        log(args, f"跳过：{'、'.join(running)} 仍在运行（同步需两个客户端都退出）。")
        write_status(
            args, last_result="skipped_running",
            last_note=f"{'、'.join(running)} 仍在运行",
            running_names=running,
        )
        return 0
    if running:
        log(args, f"警告：{'、'.join(running)} 仍在运行，已按 --allow-client-running 继续。")

    # 显式 --home-a/--home-b 优先：摘要与备份必须和引擎实际操作的目录一致
    home_a = args.home_a or statuses[0]["home"]
    home_b = args.home_b or statuses[1]["home"]
    for st in statuses:
        if not args.home_a and not st["home_exists"]:
            log(args, f"跳过：{st['display']} 的数据目录不存在（{st['home']}）。")
            write_status(args, last_result="error", last_note="数据目录不存在")
            return 0
    for label, path in (("WorkBuddy", home_a), ("WorkBuddy AI", home_b)):
        if not os.path.exists(os.path.join(path, wp.DB_NAME)):
            log(args, f"跳过：{label} 的 {wp.DB_NAME} 不存在（{path}）。")
            write_status(args, last_result="error", last_note=f"{label} 数据库不存在")
            return 0

    # 2) 有没有变化？（首次运行 / 距上次完整扫描过久 → 强制走完整流程）
    saved = read_status(args)
    reason = "首次运行"
    if saved.get("last_full_at") and saved.get("digest"):
        age = time.time() - float(saved["last_full_at"])
        if age < args.full_every:
            try:
                digest = state_digest(home_a, home_b)
            except Exception as exc:  # 摘要算不出来就当有变化，宁可多跑
                digest = ""
                log(args, f"状态摘要计算失败（{exc}），按有变化处理。")
            if digest and digest == saved["digest"]:
                log(args, f"跳过：无变化（摘要 {digest[:12]} 与上次相同）。")
                write_status(args, last_result="no_change", last_note="无变化")
                return 0
            reason = "检测到变化"
        else:
            reason = f"距上次完整扫描已 {int(age // 3600)} 小时"

    # 3) 生成计划（plan 是只读的，不需要 --state-dir）
    # 计划文件名必须每次唯一：引擎拒绝覆盖已有计划文件，
    # 而同秒内连续两次运行会撞名（秒级时间戳不够）。
    fd, plan_file = tempfile.mkstemp(
        prefix="autosync-", suffix=".json", dir=plans_dir(args)
    )
    os.close(fd)
    os.remove(plan_file)
    plan_argv = ["plan", "--output", plan_file]
    if args.no_changes:
        plan_argv.append("--no-changes")
    proc = run_engine(args, plan_argv)
    if proc.returncode != 0 or not os.path.exists(plan_file):
        detail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        log(args, f"跳过：生成计划失败（退出码 {proc.returncode}）。\n{detail}")
        write_status(args, last_result="plan_failed", last_note=detail[:400])
        return 0

    with open(plan_file, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    plan_id = plan["plan_id"]
    totals = plan["summary"]["totals"]
    sessions = int(totals["sessions_to_copy"])
    approx = int(totals["approx_bytes"])

    log(args, f"计划 {plan_id[:12]}…：待复制 {sessions} 条会话，约 {totals['approx_human']}"
             f"（触发原因：{reason}）。")

    if args.dry_run:
        log(args, "dry-run：只生成计划，不写入。")
        write_status(args, last_result="dry_run", last_plan_id=plan_id,
                     last_sessions=sessions, last_note="dry-run")
        return 0

    # 4) 先把两个库各备一份（体积很小，sqlite 在线备份）
    run_dir = os.path.join(state_root(args), "runs", plan_id)
    ensure_dir(run_dir)
    pre_dir = os.path.join(run_dir, "pre-db")
    made = []
    for home in (home_a, home_b):
        try:
            got = backup_db(home, pre_dir)
            if got:
                made.append(os.path.basename(got))
        except sqlite3.Error as exc:
            log(args, f"跳过：备份 {home} 的数据库失败（{exc}）。写入前必须先有备份。")
            write_status(args, last_result="backup_failed", last_note=str(exc)[:300])
            return 0
    log(args, f"已备份数据库：{'、'.join(made) or '（无）'} → {pre_dir}")

    # 5) 执行。免去人工确认，但引擎会重算计划并做漂移拒绝。
    apply_argv = ["apply", "--plan", plan_file, "--state-dir", state_root(args),
                  "--confirm", plan_id]
    proc = run_engine(args, apply_argv, timeout=3600)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        log(args, f"执行失败（退出码 {proc.returncode}）：\n{detail}")
        write_status(args, last_result="apply_failed", last_plan_id=plan_id,
                     last_sessions=sessions, last_note=detail[:400])
        if args.notify:
            notify("WorkBuddy 自动同步失败", f"计划 {plan_id[:12]} 执行失败，详见日志")
        return 1

    # 6) 核验
    vproc = run_engine(args, ["verify", "--plan", plan_file])
    ok = vproc.returncode == 0
    vout = (vproc.stdout or vproc.stderr or "").strip()

    log(args, f"执行完成：{sessions} 条会话，约 {totals['approx_human']}。"
             f"核验{'通过' if ok else '未通过'}。")
    write_status(
        args,
        last_result="ok" if ok else "verify_failed",
        last_full_at=time.time(),
        digest=state_digest(home_a, home_b),
        last_plan_id=plan_id,
        last_sessions=sessions,
        last_bytes=approx,
        last_human=totals["approx_human"],
        last_run_dir=run_dir,
        last_duration_s=round(time.time() - started, 1),
        last_note="" if ok else vout[-400:],
        restore_command=(
            f"{self_python()} {ENGINE} restore --run-dir {run_dir} --confirm {plan_id}"
        ),
    )
    if args.notify:
        if ok and sessions:
            notify("WorkBuddy 双 App 已自动同步",
                   f"新增 {sessions} 条会话（{totals['approx_human']}），重启客户端即可看到")
        elif not ok:
            notify("WorkBuddy 自动同步核验未通过", "写入已完成但核验异常，请查看日志")
    if not ok:
        log(args, "核验未通过，未自动回滚。回滚命令：\n"
                  f"  {self_python()} {ENGINE} restore --run-dir {run_dir} --confirm {plan_id}")
    prune(args)
    return 0 if ok else 3


# --------------------------------------------------------------------------
# launchd
# --------------------------------------------------------------------------


def plist_target() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def plist_payload(args: argparse.Namespace) -> str:
    root = state_root(args)
    adir = os.path.join(root, "autosync")
    py = self_python()
    script = os.path.abspath(__file__)
    extra = ""
    if args.home_a:
        extra += f"    <string>--home-a</string><string>{args.home_a}</string>\n"
    if args.home_b:
        extra += f"    <string>--home-b</string><string>{args.home_b}</string>\n"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>{script}</string>
    <string>run</string>
    <string>--state-root</string><string>{root}</string>
    <string>--quiet</string>
{extra}  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>{args.interval}</integer>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>5</integer>
  <key>StandardOutPath</key><string>{adir}/agent.out.log</string>
  <key>StandardErrorPath</key><string>{adir}/agent.err.log</string>
</dict>
</plist>
"""


def _launchctl(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True, check=False)


def _gui_target() -> str:
    return f"gui/{os.getuid()}"


def do_install(args: argparse.Namespace) -> int:
    if not IS_MAC:
        raise AutosyncError(
            "自动安装常驻代理目前只支持 macOS（launchd）。\n"
            "Windows 可用「任务计划程序」、Linux 可用 systemd timer，"
            "定时调用本脚本的 run 子命令即可，参数与 plist 中的一致。"
        )
    ensure_dir(state_root(args))
    ensure_dir(os.path.join(state_root(args), "runs"))
    ensure_dir(autosync_dir(args))
    py = self_python()
    me = os.path.abspath(__file__)
    if not os.path.exists(me):
        raise AutosyncError(f"脚本自身路径不存在：{me}")

    target = plist_target()
    ensure_dir(os.path.dirname(target), 0o755)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(plist_payload(args))
    os.replace(tmp, target)
    print(f"已写入 launchd 配置：{target}")

    booted = _launchctl("bootstrap", _gui_target(), target)
    if booted.returncode != 0:
        legacy = _launchctl("load", "-w", target)
        if legacy.returncode != 0:
            raise AutosyncError(
                "launchctl 加载失败，未启用代理。\n"
                f"  bootstrap: {booted.stderr.strip()}\n"
                f"  load     : {legacy.stderr.strip()}\n"
                "配置文件已写入，可手动排查后执行："
                f"launchctl bootstrap {_gui_target()} {target}"
            )
    _launchctl("enable", f"{_gui_target()}/{LABEL}")
    print(f"已加载服务：{LABEL}")

    kick = _launchctl("kickstart", "-k", f"{_gui_target()}/{LABEL}")
    if kick.returncode == 0:
        print("已立即触发一次（两个客户端都退出时才会真正写入）。")

    write_status(args, last_result=read_status(args).get("last_result", "never"))
    print(f"\n解释器 : {py}")
    print(f"状态根 : {state_root(args)}")
    print(f"间隔   : 每 {args.interval} 秒检查一次（另有登录时启动）")
    print(f"日志   : {log_path(args)}")
    print("\n验证：python3 tools/wb_autosync.py status")
    print("暂停：python3 tools/wb_autosync.py pause")
    print("卸载：python3 tools/wb_autosync.py uninstall")
    return 0


def do_uninstall(args: argparse.Namespace) -> int:
    target = plist_target()
    if not os.path.exists(target):
        print("未安装，无需卸载。")
        return 0
    out = _launchctl("bootout", f"{_gui_target()}/{LABEL}")
    if out.returncode != 0:
        _launchctl("unload", "-w", target)
    try:
        os.remove(target)
    except OSError as exc:
        raise AutosyncError(f"删除 plist 失败：{exc}") from exc
    print(f"已卸载并删除：{target}")
    print("数据、日志与历史运行记录都保留在状态根里，可随时重新 install 或手动回滚。")
    return 0


def do_pause(args: argparse.Namespace) -> int:
    with open(paused_path(args), "w", encoding="utf-8") as fh:
        fh.write(f"paused at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    write_status(args, paused=True, last_result=read_status(args).get("last_result", "never"))
    print("已暂停自动同步。恢复：python3 tools/wb_autosync.py resume")
    return 0


def do_resume(args: argparse.Namespace) -> int:
    path = paused_path(args)
    if os.path.exists(path):
        os.remove(path)
        print("已恢复自动同步。")
    else:
        print("本来就没有暂停。")
    write_status(args, paused=False, last_result=read_status(args).get("last_result", "never"))
    return 0


def do_run_now(args: argparse.Namespace) -> int:
    if IS_MAC and os.path.exists(plist_target()):
        out = _launchctl("kickstart", "-k", f"{_gui_target()}/{LABEL}")
        if out.returncode == 0:
            print("已触发一次运行（走 launchd，不受当前终端影响）。")
            print(f"查看结果：python3 tools/wb_autosync.py status")
            return 0
    print("未安装代理或触发失败，改为在当前进程里直接跑一次。")
    return run_once(args)


def human_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)} 秒前"
    if seconds < 5400:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} 小时前"
    return f"{seconds / 86400:.1f} 天前"


def do_status(args: argparse.Namespace) -> int:
    saved = read_status(args)
    installed = os.path.exists(plist_target())
    interval = args.interval
    try:
        with open(plist_target(), "r", encoding="utf-8") as fh:
            raw = fh.read()
        marker = "<key>StartInterval</key><integer>"
        if marker in raw:
            interval = int(raw.split(marker, 1)[1].split("<", 1)[0])
    except Exception:
        pass

    if args.json:
        print(json.dumps({
            "installed": installed, "paused": os.path.exists(paused_path(args)),
            "interval": interval, "state_root": state_root(args),
            "status": saved, "log": log_path(args), "plist": plist_target(),
        }, ensure_ascii=False, indent=2))
        return 0

    print("── WorkBuddy 双 App 自动同步 ──")
    print(f"  代理安装   : {'已安装' if installed else '未安装'}")
    print(f"  运行状态   : {'已暂停' if os.path.exists(paused_path(args)) else '运行中'}"
          f"（每 {interval} 秒检查一次，登录时也跑一次）")
    print(f"  状态根目录 : {state_root(args)}")
    print(f"  配置文件   : {plist_target()}")
    print(f"  日志       : {log_path(args)}")

    result_labels = {
        "ok": "上次同步成功",
        "no_change": "上次检查：无变化",
        "skipped_running": "上次检查：客户端仍在运行，已跳过",
        "paused": "上次检查：已暂停",
        "dry_run": "上次检查：dry-run",
        "plan_failed": "上次：生成计划失败",
        "backup_failed": "上次：备份失败",
        "apply_failed": "上次：执行失败",
        "verify_failed": "上次：核验未通过",
        "error": "上次：出错",
        "unknown": "上次：状态未知",
    }
    if saved.get("updated_at"):
        when = saved.get("updated_at", "")
        label = result_labels.get(saved.get("last_result", ""), saved.get("last_result") or "—")
        print(f"\n  最近一次   : {label}（{when}）")
        if saved.get("last_sessions") is not None:
            print(f"  会话数     : {saved['last_sessions']} 条 / {saved.get('last_human', '—')}")
        if saved.get("last_plan_id"):
            print(f"  计划       : {saved['last_plan_id'][:16]}…")
        if saved.get("last_note"):
            print(f"  备注       : {saved['last_note']}")
        if saved.get("restore_command"):
            print(f"  回滚命令   : {saved['restore_command']}")
    else:
        print("\n  最近一次   : 还没有运行记录")
    return 0


def do_doctor(args: argparse.Namespace) -> int:
    print("── 自检 ──")
    problems = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal problems
        if not ok:
            problems += 1
        print(f"  [{'ok' if ok else '!!'}] {name}" + (f"  {detail}" if detail else ""))

    check("平台", True, wp.platform_label())
    try:
        py = self_python()
        check("Python ≥ 3.10", True, py)
    except AutosyncError as exc:
        check("Python ≥ 3.10", False, str(exc))
    check("引擎存在", os.path.exists(ENGINE), ENGINE)

    try:
        statuses = client_report()
        for st in statuses:
            check(f"{st['display']} 数据目录", bool(st["home_exists"]), st["home"])
    except wp.PlatformError as exc:
        check("进程探测", False, str(exc))

    root = state_root(args)
    check("状态根目录", os.path.isdir(root) or not os.path.exists(root), root)
    if os.path.isdir(root):
        mode = os.stat(root).st_mode & 0o777
        check("状态根权限 0700", mode == 0o700, oct(mode))

    free = shutil.disk_usage(os.path.expanduser("~")).free
    check("磁盘余量 ≥ 1.5GB", free > 1_500_000_000, human_bytes(free))

    if IS_MAC:
        plist = plist_target()
        check("代理已安装", os.path.exists(plist), plist)
        if os.path.exists(plist):
            listed = _launchctl("list", LABEL)
            check("代理已被 launchd 加载", listed.returncode == 0,
                  (listed.stdout or listed.stderr).strip().splitlines()[0] if (listed.stdout or listed.stderr) else "")
    else:
        print("  [--] 非 macOS：自动安装不支持，请用任务计划程序 / systemd timer 调用 run")

    print(f"\n结论：{'全部通过' if problems == 0 else f'{problems} 项需要注意'}")
    return 0 if problems == 0 else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--state-root", default=DEFAULT_STATE_ROOT,
                   help=f"运行状态根目录（默认 {DEFAULT_STATE_ROOT}）")
    p.add_argument("--home-a", default=None, help="WorkBuddy 数据目录（默认自动探测）")
    p.add_argument("--home-b", default=None, help="WorkBuddy AI 数据目录（默认自动探测）")
    p.add_argument("--quiet", action="store_true", help="不把日志同时打到 stdout")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wb-autosync",
        description="WorkBuddy 与 WorkBuddy AI 的机会式自动同步：两个客户端都退出时自动写入。",
    )
    p.add_argument("--version", action="version", version=f"wb-autosync {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("run", help="执行一次机会式同步（launchd 调用，也可手动）")
    add_common(s)
    s.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    s.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="保留最近几次运行记录")
    s.add_argument("--full-every", type=int, default=FULL_SWEEP_AFTER,
                   help="距上次完整扫描超过该秒数就强制完整扫描一次")
    s.add_argument("--no-changes", action="store_true",
                   help="不搬 changes-detail/file-history，体积更小")
    s.add_argument("--dry-run", action="store_true", help="只生成计划，不写入")
    s.add_argument("--allow-client-running", action="store_true",
                   help="客户端运行时也继续（仅用于测试，正常不要用）")
    s.add_argument("--notify", dest="notify", action="store_true", default=True)
    s.add_argument("--no-notify", dest="notify", action="store_false")

    for name, help_text in (("status", "查看安装与上次运行状态"),
                            ("doctor", "自检")):
        s = sub.add_parser(name, help=help_text)
        add_common(s)
        s.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
        s.add_argument("--json", action="store_true")

    s = sub.add_parser("install", help="安装 launchd 代理（登录自启 + 定时检查）")
    add_common(s)
    s.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)

    s = sub.add_parser("uninstall", help="卸载代理（保留数据与日志）")
    add_common(s)

    for name, help_text in (("pause", "暂停自动同步"), ("resume", "恢复自动同步")):
        s = sub.add_parser(name, help=help_text)
        add_common(s)

    s = sub.add_parser("run-now", help="立刻触发一次（走 launchd 或前台执行）")
    add_common(s)
    s.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    s.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    s.add_argument("--full-every", type=int, default=FULL_SWEEP_AFTER)
    s.add_argument("--no-changes", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--allow-client-running", action="store_true")
    s.add_argument("--notify", dest="notify", action="store_true", default=True)
    s.add_argument("--no-notify", dest="notify", action="store_false")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return {
            "run": run_once,
            "status": do_status,
            "doctor": do_doctor,
            "install": do_install,
            "uninstall": do_uninstall,
            "pause": do_pause,
            "resume": do_resume,
            "run-now": do_run_now,
        }[args.command](args)
    except AutosyncError as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("\n已中断。\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
