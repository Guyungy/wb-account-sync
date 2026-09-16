#!/usr/bin/env python3
"""合成夹具端到端测试：用真实 schema 造两个假 home，跑 survey/plan/apply/verify/restore。"""
import json, os, shutil, sqlite3, subprocess, sys, tempfile

TOOL = os.path.join(os.path.dirname(__file__), "wb_home_bridge.py")
REAL_DB = os.path.expanduser("~/.workbuddy/workbuddy.db")


def find_python() -> str:
    """按优先级找一个可用的 Python 3.10+ 解释器。"""
    import glob

    explicit = os.environ.get("WB_PYTHON")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    roots = ["~/.workbuddy", "~/.workbuddy-ai"]
    for root in roots:
        cands = sorted(
            glob.glob(os.path.expanduser(f"{root}/binaries/python/versions/*/bin/python3")),
            reverse=True,
        )
        for c in cands:
            if os.access(c, os.X_OK):
                return c
    for c in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.access(c, os.X_OK):
            return c
    return "python3"


PY = find_python()

UID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
UID_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"

fails = []


def check(name, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def sh(args, expect_rc=None, quiet=False):
    r = subprocess.run([PY, TOOL] + args, capture_output=True, text=True)
    if not quiet:
        print(f"$ wb-home-bridge {' '.join(args)}")
        print((r.stdout or "").rstrip())
        if r.stderr.strip():
            print("STDERR:", r.stderr.strip()[:800])
    if expect_rc is not None and r.returncode != expect_rc:
        fails.append(f"rc {args[0]} 期望 {expect_rc} 实得 {r.returncode}")
    return r


def make_home(root, uid, nickname, sessions, slugs):
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
        cwd = f"/tmp/brtest/fake/{slugs[i % len(slugs)]}"
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
        # 会话正文
        slug = slugs[i % len(slugs)]
        pdir = os.path.join(root, "projects", slug)
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, cid + ".jsonl"), "w") as fh:
            fh.write('{"role":"user","text":"hello"}\n')
        with open(os.path.join(pdir, cid + ".meta.json"), "w") as fh:
            json.dump({"codebuddy.ai/hostKind": "unopted"}, fh)
        # 资产
        tdir = os.path.join(root, "tasks", cid)
        os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, "1.json"), "w") as fh:
            json.dump({"id": "1", "subject": "t"}, fh)
        cdir = os.path.join(root, "changes-detail", cid)
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "cd_x.json"), "w") as fh:
            fh.write("{}")
        os.makedirs(os.path.join(root, "artifact-index"), exist_ok=True)
        with open(os.path.join(root, "artifact-index", cid + ".json"), "w") as fh:
            json.dump({"version": 1, "artifacts": []}, fh)
    con.execute("INSERT INTO workspaces (path,last_opened_at) VALUES (?,?)",
                (f"/tmp/brtest/fake/{slugs[0]}", 1))
    con.commit()
    con.close()

    # 记忆 + 技能 + blobs
    os.makedirs(os.path.join(root, "memory"), exist_ok=True)
    with open(os.path.join(root, "memory", f"{uid}_memory.md"), "w") as fh:
        fh.write(f"# User Memory Profile\n> Version: 0\n\n## Memory Block\n\n记忆-{nickname}\n\n---\n\n"
                 f"<!-- RAW_JSON_START\n{{\"uid\":\"{uid}\",\"memoryBlock\":\"记忆-{nickname}\"}}\nRAW_JSON_END -->\n")
    os.makedirs(os.path.join(root, "skills", f"skill-{nickname}"), exist_ok=True)
    with open(os.path.join(root, "skills", f"skill-{nickname}", "SKILL.md"), "w") as fh:
        fh.write("---\nname: x\n---\n")
    os.makedirs(os.path.join(root, "blobs", "ab"), exist_ok=True)
    with open(os.path.join(root, "blobs", "ab", f"{nickname}.blob"), "w") as fh:
        fh.write("blob-" + nickname)
    with open(os.path.join(root, "settings.json"), "w") as fh:
        json.dump({"claw": {"users": {uid: {"channels": {"wechatmp": {"enabled": True}}}}},
                   "sandbox": True}, fh)
    os.makedirs(os.path.join(root, "connectors", uid), exist_ok=True)
    with open(os.path.join(root, "connectors", uid, "connector-states.json"), "w") as fh:
        json.dump({"feishu": {"enabled": True}}, fh)
    with open(os.path.join(root, "connectors", uid, ".master.key"), "w") as fh:
        fh.write("SECRET-" + nickname)


def count_sessions(root):
    con = sqlite3.connect(os.path.join(root, "workbuddy.db"))
    try:
        return {r[0]: r[1] for r in con.execute(
            "SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id")}
    finally:
        con.close()


base = tempfile.mkdtemp(prefix="brtest-")
A, B = os.path.join(base, "A"), os.path.join(base, "B")
SESS_A = [f"a{i:04d}-1111-4111-8111-aaaaaaaaaaaa" for i in range(6)]
SESS_B = [f"b{i:04d}-2222-4222-8222-bbbbbbbbbbbb" for i in range(3)]
make_home(A, UID_A, "Alpha", SESS_A, ["Users-x-proj1", "Users-x-proj2"])
make_home(B, UID_B, "Beta", SESS_B, ["Users-x-proj2", "Users-x-proj3"])
print(f"夹具：{base}")
print(f"  A: {count_sessions(A)}  B: {count_sessions(B)}")

STATE = os.path.join(base, "state")
PLAN = os.path.join(base, "plan.json")
common = ["--home-a", A, "--home-b", B, "--allow-client-running"]

print("\n[1] survey")
sh(["survey", "--home-a", A, "--home-b", B], expect_rc=0, quiet=True)
print("  ok")

print("\n[2] plan")
sh(["plan"] + common + ["--output", PLAN], expect_rc=0)
plan = json.load(open(PLAN))
PID = plan["plan_id"]
check("plan 记录 a2b 6 条", plan["summary"]["a2b"]["sessions_to_copy"] == 6)
check("plan 记录 b2a 3 条", plan["summary"]["b2a"]["sessions_to_copy"] == 3)

print("\n[3] apply（确认串错误应拒绝）")
r = sh(["apply"] + common + ["--plan", PLAN, "--state-dir", STATE, "--confirm", "wrong"],
       expect_rc=2)
check("错误确认串被拒绝", "确认串不匹配" in r.stderr)

print("\n[4] apply（正确确认串）")
sh(["apply"] + common + ["--plan", PLAN, "--state-dir", STATE, "--confirm", PID], expect_rc=0)
ca, cb = count_sessions(A), count_sessions(B)
print(f"  A: {ca}  B: {cb}")
check("A 得到 6+3=9 条且都归 UID_A", ca == {UID_A: 9})
check("B 得到 3+6=9 条且都归 UID_B", cb == {UID_B: 9})

print("\n[5] 正文与资产是否到位")
for cid in SESS_B:
    hit = [s for s in os.listdir(os.path.join(A, "projects"))
           if os.path.isfile(os.path.join(A, "projects", s, cid + ".jsonl"))]
    if not hit:
        check(f"A 缺 {cid} 正文", False)
        break
else:
    check("A 已含 B 的全部正文", True)
check("A 已含 B 的 tasks", all(os.path.isdir(os.path.join(A, "tasks", c)) for c in SESS_B))
check("A 已含 B 的 artifact-index",
      all(os.path.isfile(os.path.join(A, "artifact-index", c + ".json")) for c in SESS_B))
check("B 已含 A 的 changes-detail",
      all(os.path.isdir(os.path.join(B, "changes-detail", c)) for c in SESS_A))

print("\n[6] 记忆 / 技能 / blobs / 凭据隔离")
mem_a = open(os.path.join(A, "memory", f"{UID_A}_memory.md")).read()
check("A 记忆含来自 B 的 '记忆-Beta'", "记忆-Beta" in mem_a)
check("A 记忆含自己原有 '记忆-Alpha'", "记忆-Alpha" in mem_a)
check("A 记忆 uid 已改写", f'"uid": "{UID_A}"' in mem_a or f'"uid":"{UID_A}"' in mem_a)
check("A 拿到 B 的技能", os.path.isdir(os.path.join(A, "skills", "skill-Beta")))
check("B 拿到 A 的技能", os.path.isdir(os.path.join(B, "skills", "skill-Alpha")))
check("A 拿到 B 的 blob", os.path.isfile(os.path.join(A, "blobs", "ab", "Beta.blob")))
key_a = open(os.path.join(A, "connectors", UID_A, ".master.key")).read()
check("默认未覆盖 A 的连接器主密钥", key_a == "SECRET-Alpha", f"实得 {key_a!r}")
check("默认未新增 B 的连接器目录",
      not os.path.isdir(os.path.join(A, "connectors", UID_B)))
claw = json.load(open(os.path.join(A, "settings.json")))["claw"]["users"]
check("A 的渠道绑定含两个账号", UID_A in claw and UID_B in claw)

print("\n[7] verify")
r = sh(["verify", "--home-a", A, "--home-b", B, "--plan", PLAN], expect_rc=0)
check("verify 通过", "通过" in r.stdout)

print("\n[8] 幂等：再跑一次 apply")
before = (count_sessions(A), count_sessions(B))
sh(["apply"] + common + ["--plan", PLAN, "--state-dir", STATE, "--confirm", PID],
   expect_rc=None, quiet=True)
after = (count_sessions(A), count_sessions(B))
check("重复 apply 不改变行数", before == after, f"{before} vs {after}")

print("\n[9] 漂移检测：源新增会话后旧计划应拒绝")
con = sqlite3.connect(os.path.join(B, "workbuddy.db"))
con.execute("INSERT INTO sessions (id,cwd,user_id,title,status,created_at,updated_at,is_playground)"
            " VALUES (?,?,?,?,?,?,?,0)",
            ("bNEW-2222-4222-8222-bbbbbbbbbbbb", "/tmp/brtest/fake/Users-x-proj3",
             UID_B, "新会话", "completed", 9, 9))
con.commit(); con.close()
r = sh(["apply"] + common + ["--plan", PLAN, "--state-dir", STATE, "--confirm", PID],
       expect_rc=2)
check("漂移被拒绝", "漂移" in r.stderr)

print("\n[10] restore")
run_dir = os.path.join(STATE, "runs", PID)
sh(["restore", "--run-dir", run_dir, "--confirm", PID], expect_rc=0)
ca2, cb2 = count_sessions(A), count_sessions(B)
print(f"  A: {ca2}  B: {cb2}")
check("A 回滚到 6 条", ca2 == {UID_A: 6})
check("B 回滚到 4 条（含漂移测试新插的 1 条）", cb2 == {UID_B: 4})
check("A 的 B 会话正文文件已删",
      not any(os.path.isfile(os.path.join(A, "projects", s, c + ".jsonl"))
              for s in os.listdir(os.path.join(A, "projects")) for c in SESS_B))

print("\n" + ("=" * 60))
if fails:
    print(f"失败 {len(fails)} 项：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("全部通过。")
shutil.rmtree(base, ignore_errors=True)
