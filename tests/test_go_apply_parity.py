"""执行路径的跨实现等价性测试：Go 与 Python 各跑一遍完整迁移，再逐行对账。

为什么值得单独写一组：plan 的等价性只证明"两边看数据的方式一样"，
真正会损坏用户数据的是 apply —— 插入哪些行、复制哪些文件、记忆怎么合并、
undo 记了什么，任何一处不同都可能是"一边能回滚、另一边回滚不掉"。

做法：同一份合成夹具复制成两组互不相干的目录，两边各自 plan → apply → verify
→ restore，然后在每一步之后比对：
  - 数据库：按表逐行比（值相等，与页布局无关）
  - 文件树：相对路径 + 内容哈希
  - 记忆文件与 settings.json：时间戳归一化后逐字节比

`go` 不在 PATH 上时跳过，不阻塞纯 Python 环境。
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
# Go 侧已降为参照实现（2026-09-21 起主底座改 Rust + TypeScript）。
GO_MODULE = os.path.join(ROOT, "legacy-go")
GO = shutil.which("go")

# apply 会遍历这些表；buddy_snapshots 在夹具里恒为空，留着是为了覆盖列裁剪逻辑。
DB_TABLES = (
    "sessions", "session_usage", "workspaces", "buddy_snapshots",
    "automations", "automation_runs", "automation_runtime_state",
)

SESSION_COLUMNS = (
    "id", "cwd", "user_id", "deleted_at", "title", "created_at", "updated_at",
    "status", "mode", "model", "permission_mode", "is_playground",
    "use_sandbox_cli", "buddy_snapshot_id", "context_window",
)

SCHEMA = f"""
CREATE TABLE sessions (
    {", ".join(f"{c} TEXT" for c in SESSION_COLUMNS)},
    is_playground_int INTEGER
);
CREATE TABLE session_usage (session_id TEXT, tokens INTEGER, cost REAL, note TEXT);
CREATE TABLE workspaces (path TEXT, name TEXT);
CREATE TABLE buddy_snapshots (snapshot_id TEXT, payload TEXT);
CREATE TABLE automations (id TEXT, deleted_at TEXT, title TEXT, status TEXT,
                          owner_user_id TEXT, owner_status TEXT, next_run_at TEXT);
CREATE TABLE automation_runs (thread_id TEXT, automation_id TEXT, ok INTEGER);
CREATE TABLE automation_runtime_state (automation_id TEXT, running INTEGER, last TEXT);
"""

# 时间戳在两台机器上必然不同，比对前抹平；
# .before-bridge-* 备份的文件名里也带时间戳，整个文件从清单里排除。
TS_PATTERNS = [
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:?\d{2})?"), "<TS>"),
    (re.compile(r"\.before-bridge-\d+"), ".before-bridge-<TS>"),
    (re.compile(r'"at":\s*"<TS>"'), '"at": "<TS>"'),
]

EXCLUDE_FROM_MANIFEST = {
    "workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm",
}


def normalize(text: str) -> str:
    for pat, repl in TS_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _session(sid, cwd, uid, **over):
    row = dict.fromkeys(SESSION_COLUMNS)
    row.update({
        "id": sid, "cwd": cwd, "user_id": uid, "deleted_at": None,
        "title": f"会话 {sid}", "created_at": "1760000000000",
        "updated_at": "1760000001000", "status": "idle", "mode": "chat",
        "model": "m", "permission_mode": "default", "is_playground": "0",
        "use_sandbox_cli": "0", "buddy_snapshot_id": "", "context_window": "128",
    })
    row.update(over)
    return tuple(row[c] for c in SESSION_COLUMNS)


def make_home(path, uid, nickname, sessions, workspaces, projects, options=None):
    """造一个自带完整目录布局的合成 home。"""
    options = options or {}
    os.makedirs(path, exist_ok=True)
    con = sqlite3.connect(os.path.join(path, "workbuddy.db"))
    con.executescript(SCHEMA)
    for row in sessions:
        cols = ", ".join(SESSION_COLUMNS)
        con.execute(f"INSERT INTO sessions ({cols}) VALUES ({', '.join('?' * len(SESSION_COLUMNS))})", row)
        con.execute(
            "INSERT INTO session_usage (session_id, tokens, cost, note) VALUES (?,?,?,?)",
            (row[0], 100, 0.5, "备注 中文"),
        )
    for ws in workspaces:
        con.execute("INSERT INTO workspaces (path, name) VALUES (?,?)", (ws, os.path.basename(ws)))
    for auto in options.get("automations", []):
        con.execute(
            "INSERT INTO automations (id, deleted_at, title, status, owner_user_id,"
            " owner_status, next_run_at) VALUES (?,?,?,?,?,?,?)", auto)
    con.commit()
    con.close()

    snap = os.path.join(path, "storage", "skeleton", "account-snapshot.json")
    os.makedirs(os.path.dirname(snap), exist_ok=True)
    with open(snap, "w", encoding="utf-8") as fh:
        json.dump({"primary": {"uid": uid, "nickname": nickname}}, fh)

    for slug, cid in projects:
        bucket = os.path.join(path, "projects", slug)
        os.makedirs(os.path.join(bucket, cid), exist_ok=True)
        with open(os.path.join(bucket, cid + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"role":"user"}\n')
        with open(os.path.join(bucket, cid, "tool.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")

    for sub in ("blobs", "skills", "tasks", "changes-detail", "changes-index",
                "file-history", "artifact-index", "connectors",
                "storage/user-" + uid):
        os.makedirs(os.path.join(path, sub), exist_ok=True)

    # 记忆：磁盘上写用户自己的表述（带 RAW_JSON 段），合并时要被重写成目标账号
    mem = os.path.join(path, "memory", uid + "_memory.md")
    os.makedirs(os.path.dirname(mem), exist_ok=True)
    with open(mem, "w", encoding="utf-8") as fh:
        fh.write(
            "# User Memory Profile\n> Version: 0\n\n## Memory Block\n\n"
            f"来自{nickname}的记忆段\n\n---\n\n<!-- RAW_JSON_START\n"
            f'{{"uid": "{uid}", "memoryBlock": "来自{nickname}的记忆段"}}\n'
            "RAW_JSON_END -->\n"
        )

    # 技能：同名技能两边各留各的版本，合并时不应互相覆盖
    skill = os.path.join(path, "skills", "shared-skill")
    os.makedirs(skill, exist_ok=True)
    with open(os.path.join(skill, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(f"---\nname: shared-skill\norigin: {nickname}\n---\n")

    blob_dir = os.path.join(path, "blobs", "ab")
    os.makedirs(blob_dir, exist_ok=True)
    with open(os.path.join(blob_dir, f"{nickname}.blob"), "w", encoding="utf-8") as fh:
        fh.write("blob-" + nickname)

    # 渠道绑定 + 一个非 claw 的顶层键，用来验证保序往返不会重排整个文件
    with open(os.path.join(path, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "theme": "light",
            "claw": {"users": {uid: {"channels": {"wechatmp": {"enabled": True}}}}},
            "sandbox": True,
        }, fh, ensure_ascii=False, indent=2)

    conn = os.path.join(path, "connectors", uid)
    os.makedirs(conn, exist_ok=True)
    with open(os.path.join(conn, "connector-states.json"), "w", encoding="utf-8") as fh:
        json.dump({"feishu": {"enabled": True}}, fh)
    # 凭据绝不能跨 home 复制——夹具里放一份，用来验证它没被搬走
    with open(os.path.join(conn, ".master.key"), "w", encoding="utf-8") as fh:
        fh.write("SECRET-" + nickname)


def dump_db(home):
    """按表导出全部行，排序后返回——与 SQLite 的页布局无关。"""
    out = {}
    con = sqlite3.connect(os.path.join(home, "workbuddy.db"))
    con.row_factory = sqlite3.Row
    try:
        for table in DB_TABLES:
            try:
                rows = [dict(r) for r in con.execute(f"SELECT * FROM {table}")]
            except sqlite3.OperationalError:
                continue
            keys = sorted(rows[0].keys()) if rows else []
            out[table] = sorted(
                (tuple(str(r[k]) for k in keys) for r in rows)
            )
    finally:
        con.close()
    return out


def file_manifest(home):
    """相对路径 → 内容哈希（文本走归一化，二进制原样）。"""
    out = {}
    for root, _dirs, files in os.walk(home):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, home)
            base = os.path.basename(rel)
            if base in EXCLUDE_FROM_MANIFEST or ".before-bridge-" in base:
                continue
            if rel.endswith(".before-bridge-0"):
                continue
            try:
                with open(full, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            try:
                text = raw.decode("utf-8")
                out[rel] = hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()
            except UnicodeDecodeError:
                out[rel] = hashlib.sha256(raw).hexdigest()
    return out


class GoApplyParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not GO:
            raise unittest.SkipTest("PATH 上没有 go，跳过执行路径等价性测试")
        cls.tmp = tempfile.mkdtemp(prefix="wb-go-apply-")
        cls.bin = os.path.join(cls.tmp, "wb-bridge")
        proc = subprocess.run(
            [GO, "build", "-o", cls.bin, "./cmd/wb-bridge"],
            cwd=GO_MODULE, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("go build 失败，跳过：" + proc.stderr[-400:])
        cls.py = sys.executable

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="case-", dir=self.tmp)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.uid_a = "aaaaaaaa-0000-0000-0000-000000000001"
        self.uid_b = "bbbbbbbb-0000-0000-0000-000000000002"
        self.ws = os.path.join(self.root, "ws")
        os.makedirs(self.ws, exist_ok=True)

    # ---------------- 夹具 ----------------

    def _seed(self, base):
        """在 base 下造出一对一模一样的 homes。"""
        a = os.path.join(base, "a")
        b = os.path.join(base, "b")
        make_home(
            a, self.uid_a, "左账号",
            sessions=[
                _session("s-1", self.ws, self.uid_a),
                _session("s-2", self.ws, self.uid_a, title='带 "引号" 与\n换行'),
                _session("s-both", self.ws, self.uid_a, title="两边都有"),
            ],
            workspaces=[self.ws, self.root],
            projects=[("bucket-a", "s-1"), ("bucket-a", "s-2")],
            options={"automations": [
                ("auto-1", None, "每日汇总", "PAUSED", self.uid_a, "confirmed", None),
            ]},
        )
        make_home(
            b, self.uid_b, "右账号",
            sessions=[
                _session("s-3", self.ws, self.uid_b, title="右侧独有 🙂"),
                _session("s-both", self.ws, self.uid_b, title="两边都有"),
            ],
            workspaces=[self.ws],
            projects=[("bucket-b", "s-3")],
        )
        return a, b

    def _run_py(self, *args):
        env = dict(os.environ, PYTHONPATH=TOOLS)
        return subprocess.run(
            [self.py, os.path.join(TOOLS, "wb_home_bridge.py"), *args],
            cwd=ROOT, capture_output=True, text=True, env=env,
        )

    def _run_go(self, *args):
        return subprocess.run([self.bin, *args], capture_output=True, text=True)

    # ---------------- 场景 ----------------

    def _do_apply(self, impl, plan_extra=()):
        """跑一遍完整的 plan → apply，返回 (a, b, plan, state_dir)。"""
        base = os.path.join(self.root, impl)
        a, b = self._seed(base)
        state = os.path.join(self.root, impl + "-state")
        plan_file = os.path.join(self.root, impl + "-plan.json")

        plan_args = ["plan", "--json", "--home-a", a, "--home-b", b,
                     "--output", plan_file, *plan_extra]
        run = self._run_py if impl == "py" else self._run_go
        proc = run(*plan_args)
        self.assertEqual(proc.returncode, 0, f"{impl} plan 失败：{proc.stderr[-800:]}")
        plan = json.loads(proc.stdout)

        common = ["--home-a", a, "--home-b", b, "--plan", plan_file,
                  "--state-dir", state, "--confirm", plan["plan_id"],
                  "--allow-client-running"]
        proc = run("apply", *common)
        self.assertEqual(proc.returncode, 0, f"{impl} apply 失败：{proc.stderr[-1200:]}")
        return a, b, plan, state

    def test_apply_produces_identical_databases(self):
        pa = self._do_apply("py")
        ga = self._do_apply("go")
        # 两边实现都是各自独立的夹具副本，跑完之后 DB 内容必须完全一致。
        for side, idx in (("a", 0), ("b", 1)):
            self.assertEqual(
                dump_db(pa[idx]), dump_db(ga[idx]),
                f"{side} 侧数据库在两边实现下不一致",
            )

    def test_apply_produces_identical_file_trees(self):
        pa = self._do_apply("py")
        ga = self._do_apply("go")
        for side, idx in (("a", 0), ("b", 1)):
            mp, mg = file_manifest(pa[idx]), file_manifest(ga[idx])
            self.assertEqual(
                sorted(mp.keys()), sorted(mg.keys()),
                f"{side} 侧文件清单不一致："
                f"仅 Python 有 {set(mp) - set(mg)}；仅 Go 有 {set(mg) - set(mp)}",
            )
            for rel in sorted(mp):
                self.assertEqual(mp[rel], mg[rel], f"{side} 侧 {rel} 内容不一致")

    def test_apply_survives_both_option_shapes(self):
        extra = ["--include-plugins", "--include-automations",
                 "--include-storage", "--include-connectors", "--overwrite-assets"]
        pa = self._do_apply("py", extra)
        ga = self._do_apply("go", extra)
        self.assertEqual(dump_db(pa[0]), dump_db(ga[0]))
        self.assertEqual(dump_db(pa[1]), dump_db(ga[1]))

    def test_credentials_never_cross_homes(self):
        """两侧都不能把 .master.key 搬过去——这是最不能出错的一条。"""
        for impl in ("py", "go"):
            a, b, _plan, _state = self._do_apply(impl)
            for home, other in ((a, "左账号"), (b, "右账号")):
                for root, _dirs, files in os.walk(home):
                    for name in files:
                        if name != ".master.key":
                            continue
                        with open(os.path.join(root, name), encoding="utf-8") as fh:
                            content = fh.read()
                        self.assertIn(
                            other, content,
                            f"{impl}：{home} 下出现了别的账号的凭据",
                        )

    def test_verify_agrees_and_passes_on_both(self):
        pa = self._do_apply("py")
        ga = self._do_apply("go")
        for impl, (a, b, plan, _state) in (("py", pa), ("go", ga)):
            run = self._run_py if impl == "py" else self._run_go
            plan_file = os.path.join(self.root, impl + "-plan.json")
            proc = run("verify", "--plan", plan_file, "--home-a", a, "--home-b", b)
            self.assertEqual(proc.returncode, 0,
                             f"{impl} verify 未通过：{proc.stdout[-800:]}{proc.stderr[-400:]}")
        # 注意：这里**不能**比 plan_id。计划正文里含两个 home 的绝对路径，
        # 而 py/go 两份夹具放在不同目录下，plan_id 天然不同。
        # 跨实现的 plan_id 等价性由 test_go_parity 在**同一份路径**上验证。

    def test_second_apply_is_rejected_as_drift_on_both(self):
        """第二次 apply 必须被两边一致地拒绝。

        这里值得说清楚"幂等"到底指什么：**行级**是幂等的
        （INSERT OR IGNORE，重复插不会多），但**计划级**不是——
        第一遍写完之后目标侧多出了会话，重新生成的计划自然不同，
        于是 plan_id 变化、漂移检测触发。这是设计意图，不是缺陷：
        宁可拒绝，也不要拿一份对不上现状的计划继续写。
        所以"跑两次结果一样"这个断言本身是错的，该断言的是
        "两边用同一种方式拒绝，且拒绝时一个字节都没动"。
        """
        snapshots = {}
        for impl in ("py", "go"):
            a, b, plan, state = self._do_apply(impl)
            before = (dump_db(a), dump_db(b), file_manifest(a), file_manifest(b))
            run = self._run_py if impl == "py" else self._run_go
            plan_file = os.path.join(self.root, impl + "-plan.json")
            proc = run("apply", "--home-a", a, "--home-b", b, "--plan", plan_file,
                       "--state-dir", state, "--confirm", plan["plan_id"],
                       "--allow-client-running")
            self.assertEqual(proc.returncode, 2,
                             f"{impl} 第二次 apply 未被拒绝（rc={proc.returncode}）")
            self.assertIn("漂移", proc.stderr + proc.stdout,
                          f"{impl} 拒绝理由不是漂移：{proc.stderr[-400:]}")
            after = (dump_db(a), dump_db(b), file_manifest(a), file_manifest(b))
            self.assertEqual(before, after, f"{impl} 被拒绝时仍然改了数据")
            snapshots[impl] = after
        self.assertEqual(snapshots["py"], snapshots["go"])

    def test_restore_returns_both_to_pre_apply_state(self):
        """回滚后两个实现都要回到迁移前的样子。"""
        snapshots = {}
        for impl in ("py", "go"):
            base = os.path.join(self.root, impl + "-pre")
            a, b = self._seed(base)
            before = (dump_db(a), dump_db(b), file_manifest(a), file_manifest(b))
            # 把刚造好的夹具挪到标准位置再跑 apply
            shutil.rmtree(base)
            a, b, plan, state = self._do_apply(impl)
            run = self._run_py if impl == "py" else self._run_go
            run_dir = os.path.join(state, "runs", plan["plan_id"])
            proc = run("restore", "--run-dir", run_dir, "--confirm", plan["plan_id"])
            self.assertEqual(proc.returncode, 0,
                             f"{impl} restore 失败：{proc.stderr[-800:]}")
            after = (dump_db(a), dump_db(b), file_manifest(a), file_manifest(b))
            snapshots[impl] = after
            self.assertEqual(before[0], after[0], f"{impl}：a 侧数据库未回到迁移前")
            self.assertEqual(before[1], after[1], f"{impl}：b 侧数据库未回到迁移前")
        self.assertEqual(snapshots["py"], snapshots["go"])

    def test_backup_manifests_agree(self):
        """备份：两边的清单结构一致，库内容一致（页布局不管）。"""
        results = {}
        for impl in ("py", "go"):
            a, b, _plan, _state = self._do_apply(impl)
            dest = os.path.join(self.root, impl + "-backup")
            run = self._run_py if impl == "py" else self._run_go
            proc = run("backup", "--dest", dest, "--label", "snap",
                       "--home-a", a, "--home-b", b, "--allow-client-running")
            self.assertEqual(proc.returncode, 0, f"{impl} backup 失败：{proc.stderr[-800:]}")
            root = os.path.join(dest, "snap-home-bridge")
            with open(os.path.join(root, "backup-manifest.json"), encoding="utf-8") as fh:
                manifest = json.load(fh)
            # 备份里的子目录名用的是客户端 slug（wb / wb_ai），不是 a / b。
            results[impl] = {
                "manifest_keys": sorted(manifest.keys()),
                "include_heavy": manifest["include_heavy"],
                "excluded": manifest["excluded_by_default"],
                "db": {side: dump_db(os.path.join(root, side)) for side in ("wb", "wb_ai")},
                "has_settings": os.path.isfile(os.path.join(root, "wb", "settings.json")),
            }
        self.assertEqual(results["py"], results["go"])


if __name__ == "__main__":
    unittest.main()
