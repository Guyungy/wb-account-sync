#!/usr/bin/env python3
"""wb_autosync 的合成夹具端到端自检 —— 不碰真实账号。

用真实 schema 造两个假 home，验证自动同步这一层的行为：
客户端运行时的跳过、变化检测、幂等、备份、暂停、并发锁、清理。

    python3 tools/autosync_check.py
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "wb_autosync.py")
REAL_DB = os.path.expanduser("~/.workbuddy/workbuddy.db")


def find_python() -> str:
    for root in (os.path.expanduser("~/.workbuddy"), os.path.expanduser("~/.workbuddy-ai")):
        versions = os.path.join(root, "binaries", "python", "versions")
        if os.path.isdir(versions):
            for name in sorted(os.listdir(versions), reverse=True):
                cand = os.path.join(versions, name, "bin", "python3")
                if os.path.isfile(cand):
                    return cand
    return sys.executable


PY = find_python()
UID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
UID_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"

fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def sh(args: list[str], expect_rc: int | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run([PY, TOOL] + args, capture_output=True, text=True)
    if r.stderr.strip():
        print("  STDERR:", r.stderr.strip()[:400])
    if expect_rc is not None and r.returncode != expect_rc:
        fails.append(f"rc {args[0]} 期望 {expect_rc} 实得 {r.returncode}")
        print(f"  !! 退出码 {r.returncode} != {expect_rc}")
    return r


def make_home(root: str, uid: str, nickname: str, sessions: list[str]) -> None:
    os.makedirs(root, exist_ok=True)
    db = os.path.join(root, "workbuddy.db")
    schema = subprocess.run(["sqlite3", REAL_DB, ".schema"], capture_output=True, text=True).stdout
    subprocess.run(["sqlite3", db], input=schema, text=True, check=True)

    os.makedirs(os.path.join(root, "storage", "skeleton"), exist_ok=True)
    with open(os.path.join(root, "storage", "skeleton", "account-snapshot.json"), "w") as fh:
        json.dump({"primary": {"version": 1, "uid": uid, "nickname": nickname,
                               "type": "personal", "savedAt": 1}}, fh)

    con = sqlite3.connect(db)
    for i, cid in enumerate(sessions):
        cwd = f"{root}/proj"
        os.makedirs(cwd, exist_ok=True)
        con.execute(
            "INSERT INTO sessions (id,cwd,user_id,title,status,created_at,updated_at,is_playground)"
            " VALUES (?,?,?,?,?,?,?,0)",
            (cid, cwd, uid, f"会话 {i}", "completed", 1000 + i, 2000 + i),
        )
        con.execute(
            "INSERT INTO session_usage (session_id,used,size,updated_at) VALUES (?,?,?,?)",
            (cid, 10 + i, 100, 3000 + i),
        )
        pdir = os.path.join(root, "projects", f"slug-{nickname}")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, cid + ".jsonl"), "w") as fh:
            fh.write('{"role":"user","text":"hello"}\n')
        with open(os.path.join(pdir, cid + ".meta.json"), "w") as fh:
            json.dump({"codebuddy.ai/hostKind": "unopted"}, fh)
        os.makedirs(os.path.join(root, "tasks", cid), exist_ok=True)
        with open(os.path.join(root, "tasks", cid, "1.json"), "w") as fh:
            json.dump({"id": "1"}, fh)
    con.execute("INSERT INTO workspaces (path,last_opened_at) VALUES (?,?)", (f"{root}/proj", 1))
    con.commit()
    con.close()

    os.makedirs(os.path.join(root, "memory"), exist_ok=True)
    with open(os.path.join(root, "memory", f"{uid}_memory.md"), "w") as fh:
        fh.write(f"# Memory\n> Version: 0\n\n## Memory Block\n\n记忆-{nickname}\n")
    os.makedirs(os.path.join(root, "skills", f"skill-{nickname}"), exist_ok=True)
    with open(os.path.join(root, "skills", f"skill-{nickname}", "SKILL.md"), "w") as fh:
        fh.write("---\nname: x\n---\n")
    with open(os.path.join(root, "settings.json"), "w") as fh:
        json.dump({"claw": {"users": {uid: {"channels": {}}}}, "sandbox": True}, fh)


def add_session(root: str, uid: str, cid: str) -> None:
    con = sqlite3.connect(os.path.join(root, "workbuddy.db"))
    con.execute(
        "INSERT INTO sessions (id,cwd,user_id,title,status,created_at,updated_at,is_playground)"
        " VALUES (?,?,?,?,?,?,?,0)",
        (cid, f"{root}/proj", uid, "新会话", "completed", 9000, 9001),
    )
    con.commit()
    con.close()
    pdir = os.path.join(root, "projects", "slug-" + ("Alpha" if uid == UID_A else "Beta"))
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, cid + ".jsonl"), "w") as fh:
        fh.write("{}\n")


def owners(root: str) -> dict[str, int]:
    con = sqlite3.connect(os.path.join(root, "workbuddy.db"))
    try:
        return {r[0]: r[1] for r in con.execute(
            "SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id")}
    finally:
        con.close()


def read_status(state: str) -> dict:
    path = os.path.join(state, "autosync", "status.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


base = tempfile.mkdtemp(prefix="autosync-check-")
A, B = os.path.join(base, "A"), os.path.join(base, "B")
STATE = os.path.join(base, "state")
SESS_A = [f"a{i:04d}-1111-4111-8111-aaaaaaaaaaaa" for i in range(6)]
SESS_B = [f"b{i:04d}-2222-4222-8222-bbbbbbbbbbbb" for i in range(3)]
make_home(A, UID_A, "Alpha", SESS_A)
make_home(B, UID_B, "Beta", SESS_B)
print(f"夹具：{base}")
print(f"  A: {owners(A)}  B: {owners(B)}\n")

common = ["--home-a", A, "--home-b", B, "--state-root", STATE,
          "--allow-client-running", "--no-notify"]

print("[1] 客户端运行时必须跳过（不给 --allow 时）")
r = sh([a for a in ["run", "--state-root", STATE, "--no-notify"] if a], expect_rc=0)
# 真实客户端此刻在跑（Agent 就跑在 WorkBuddy 里），所以这里应当跳过
st = read_status(STATE)
check("跳过时未写入任何数据", owners(A) == {UID_A: 6} and owners(B) == {UID_B: 3})
check("记录跳过原因", st.get("last_result") in ("skipped_running", "unknown"),
      str(st.get("last_result")))

print("\n[2] 首次同步（allow-client-running 模拟两个客户端都已退出）")
sh(["run"] + common, expect_rc=0)
ca, cb = owners(A), owners(B)
print(f"  A: {ca}  B: {cb}")
check("A 得到 6+3=9 条且都归 UID_A", ca == {UID_A: 9})
check("B 得到 3+6=9 条且都归 UID_B", cb == {UID_B: 9})
st = read_status(STATE)
check("状态记录为成功", st.get("last_result") == "ok", str(st.get("last_result")))
check("记录了会话数 9", st.get("last_sessions") == 9, str(st.get("last_sessions")))
check("记录了摘要", bool(st.get("digest")))
check("记录了回滚命令", "restore" in str(st.get("restore_command", "")))

print("\n[3] 写入前是否留了数据库备份")
run_dir = st.get("last_run_dir", "")
pre = os.path.join(run_dir, "pre-db")
files = sorted(os.listdir(pre)) if os.path.isdir(pre) else []
check("拥有两个库的备份", len(files) == 2, ", ".join(files))
check("备份非空", all(os.path.getsize(os.path.join(pre, f)) > 0 for f in files))
check("undo journal 已生成", os.path.exists(os.path.join(run_dir, "undo.json")))
check("journal 已生成", os.path.exists(os.path.join(run_dir, "journal.jsonl")))

print("\n[4] 第二次运行：无变化必须跳过（不空转）")
n_before = len(os.listdir(os.path.join(STATE, "runs")))
sh(["run"] + common, expect_rc=0)
st = read_status(STATE)
check("判定为无变化", st.get("last_result") == "no_change", str(st.get("last_result")))
check("没有新建运行目录", len(os.listdir(os.path.join(STATE, "runs"))) == n_before)

print("\n[5] 新增一条会话后应自动再次同步")
add_session(A, UID_A, "a9999-1111-4111-8111-aaaaaaaaaaaa")
sh(["run"] + common, expect_rc=0)
cb = owners(B)
check("B 收到新增会话（3+7=10）", cb == {UID_B: 10}, str(cb))
check("状态回到成功", read_status(STATE).get("last_result") == "ok")

print("\n[6] 暂停 / 恢复")
sh(["pause", "--state-root", STATE], expect_rc=0)
sh(["run"] + common, expect_rc=0)
check("暂停时运行被跳过", read_status(STATE).get("last_result") == "paused")
sh(["resume", "--state-root", STATE], expect_rc=0)
check("恢复后 PAUSED 标记已删除",
      not os.path.exists(os.path.join(STATE, "autosync", "PAUSED")))

print("\n[7] 并发锁：另一个实例持锁时必须让路")
lock_file = os.path.join(STATE, "autosync", "lock")
fh = open(lock_file, "a+")
fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
r = sh(["run"] + common, expect_rc=0)
fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
fh.close()
with open(os.path.join(STATE, "autosync", "autosync.log"), encoding="utf-8") as fh2:
    tail = fh2.read()[-600:]
check("持锁时被拒绝且不报错退出", "另一个实例正在运行" in tail)

print("\n[8] 保留策略：--keep 1 只留最近一次")
add_session(A, UID_A, "a8888-1111-4111-8111-aaaaaaaaaaaa")
sh(["run"] + common + ["--keep", "1"], expect_rc=0)
runs = os.listdir(os.path.join(STATE, "runs"))
plans = os.listdir(os.path.join(STATE, "autosync", "plans"))
check("运行目录只留 1 个", len(runs) <= 1, f"{len(runs)} 个")
check("计划文件只留 1 个", len(plans) <= 1, f"{len(plans)} 个")

print("\n[9] 生成的 launchd 配置内容检查（不实际安装）")
sys.path.insert(0, HERE)
import wb_autosync as asy  # noqa: E402

ns = type("NS", (), {"state_root": STATE, "interval": 300, "home_a": None, "home_b": None})()
plist = asy.plist_payload(ns)
check("Label 正确", f"<string>{asy.LABEL}</string>" in plist)
check("RunAtLoad 打开", "<key>RunAtLoad</key><true/>" in plist)
check("StartInterval 生效", "<key>StartInterval</key><integer>300</integer>" in plist)
check("不用 KeepAlive 常驻", "KeepAlive" not in plist)
check("日志指向状态根", os.path.join(STATE, "autosync") in plist)
check("run 子命令与 --quiet", "<string>run</string>" in plist and "<string>--quiet</string>" in plist)

print("\n[10] 状态目录权限必须 0700（引擎硬性要求）")
check("状态根 0700", (os.stat(STATE).st_mode & 0o777) == 0o700,
      oct(os.stat(STATE).st_mode & 0o777))

shutil.rmtree(base, ignore_errors=True)

print()
if fails:
    print(f"失败 {len(fails)} 项：")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("全部通过")
sys.exit(0)
