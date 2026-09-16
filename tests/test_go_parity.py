"""Go 实现与 Python 实现的等价性契约测试。

**这是整个 Go 移植的验收标准。** plan_id 是对规范化计划正文取的 SHA-256，
所以"两边 plan_id 相同"等价于"计划内容逐字节相同"——不需要人工比对字段。

测试自带一份合成数据（不碰任何真实客户端目录），在临时目录里跑出两个 home，
两边各生成一次计划再比对。`go` 不在 PATH 上时跳过，不阻塞纯 Python 环境。
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
GO_MODULE = os.path.join(ROOT, "gobridge")
GO = shutil.which("go")

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


def _write_snapshot(home: str, uid: str, nickname: str) -> None:
    path = os.path.join(home, "storage", "skeleton", "account-snapshot.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"primary": {"uid": uid, "nickname": nickname}}, fh)


def _make_home(path: str, uid: str, nickname: str, sessions, workspaces, projects) -> None:
    os.makedirs(path, exist_ok=True)
    con = sqlite3.connect(os.path.join(path, "workbuddy.db"))
    con.executescript(SCHEMA)
    for row in sessions:
        cols = ", ".join(SESSION_COLUMNS)
        placeholders = ", ".join("?" for _ in SESSION_COLUMNS)
        con.execute(
            f"INSERT INTO sessions ({cols}) VALUES ({placeholders})",
            row,
        )
        con.execute(
            "INSERT INTO session_usage (session_id, tokens, cost, note) VALUES (?,?,?,?)",
            (row[0], 100, 0.5, "备注 中文"),
        )
    for ws in workspaces:
        con.execute("INSERT INTO workspaces (path, name) VALUES (?,?)", (ws, os.path.basename(ws)))
    con.commit()
    con.close()

    _write_snapshot(path, uid, nickname)
    # 目录与文件的存在性会影响 entries（只搬存在的），所以两边布局要对称。
    for slug, cid in projects:
        bucket = os.path.join(path, "projects", slug)
        os.makedirs(os.path.join(bucket, cid), exist_ok=True)
        with open(os.path.join(bucket, cid + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"role":"user"}\n')
        with open(os.path.join(bucket, cid, "tool.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
    for sub in ("blobs", "skills", "tasks", "changes-detail", "changes-index",
                "file-history", "artifact-index", "connectors", "storage/user-" + uid):
        os.makedirs(os.path.join(path, sub), exist_ok=True)


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


class GoParityTest(unittest.TestCase):
    """Go 与 Python 必须对同一份数据算出同一个 plan_id。"""

    @classmethod
    def setUpClass(cls):
        if not GO:
            raise unittest.SkipTest("PATH 上没有 go，跳过跨实现等价性测试")
        cls.tmp = tempfile.mkdtemp(prefix="wb-go-parity-")
        cls.bin = os.path.join(cls.tmp, "wb-bridge")
        proc = subprocess.run(
            [GO, "build", "-o", cls.bin, "./cmd/wb-bridge"],
            cwd=GO_MODULE, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("go build 失败，跳过：" + proc.stderr[-400:])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="homes-", dir=self.tmp)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.uid_a = "aaaaaaaa-0000-0000-0000-000000000001"
        self.uid_b = "bbbbbbbb-0000-0000-0000-000000000002"
        self.a = os.path.join(self.root, "a")
        self.b = os.path.join(self.root, "b")
        ws = os.path.join(self.root, "ws")
        os.makedirs(ws, exist_ok=True)

        # A 侧三条会话（两条是 B 没有的），B 侧一条。
        _make_home(
            self.a, self.uid_a, "左账号",
            sessions=[
                _session("s-1", ws, self.uid_a),
                _session("s-2", ws, self.uid_a, title='带 "引号" 与\n换行'),
                _session("s-both", ws, self.uid_a, title="两边都有"),
            ],
            workspaces=[ws, self.root],
            projects=[("bucket-a", "s-1"), ("bucket-a", "s-2")],
        )
        _make_home(
            self.b, self.uid_b, "右账号",
            sessions=[
                _session("s-3", ws, self.uid_b, title="右侧独有 🙂"),
                _session("s-both", ws, self.uid_b, title="两边都有"),
            ],
            workspaces=[ws],
            projects=[("bucket-b", "s-3")],
        )

    def _python_plan(self, *extra):
        env = dict(os.environ, PYTHONPATH=TOOLS)
        proc = subprocess.run(
            [sys.executable, os.path.join(TOOLS, "wb_home_bridge.py"), "plan", "--json",
             "--home-a", self.a, "--home-b", self.b, *extra],
            cwd=ROOT, capture_output=True, text=True, env=env,
        )
        if proc.returncode != 0:
            self.fail("Python 版生成计划失败：\n" + proc.stderr[-800:])
        return json.loads(proc.stdout)

    def _go_plan(self, *extra):
        proc = subprocess.run(
            [self.bin, "plan", "--json", "--home-a", self.a, "--home-b", self.b, *extra],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            self.fail("Go 版生成计划失败：\n" + proc.stderr[-800:])
        return json.loads(proc.stdout)

    def _assert_same_plan(self, *extra):
        py, go = self._python_plan(*extra), self._go_plan(*extra)
        self.assertEqual(py["version"], go["version"], "version 不同")
        self.assertEqual(py["options"], go["options"], "options 不同")
        self.assertEqual(
            py["source_fingerprints"], go["source_fingerprints"], "源指纹不同"
        )
        self.assertEqual(py["entries"], go["entries"], "entries 不同")
        self.assertEqual(py["summary"], go["summary"], "summary 不同")
        self.assertEqual(
            py["plan_id"], go["plan_id"],
            "plan_id 不同——说明计划正文存在差异，逐字段比对上面几项可定位",
        )
        return py, go

    def test_default_options_produce_identical_plan(self):
        py, go = self._assert_same_plan()
        self.assertEqual(py["summary"]["totals"]["sessions_to_copy"], 3)
        self.assertGreater(len(py["entries"]), 0)

    def test_identical_when_all_optional_scope_enabled(self):
        # 分支最多的一条路径：自动化、存储、连接器、插件缓存全开。
        self._assert_same_plan(
            "--include-plugins", "--include-automations",
            "--include-storage", "--include-connectors", "--overwrite-assets",
        )

    def test_identical_when_scope_narrowed(self):
        self._assert_same_plan("--no-changes", "--no-skills", "--no-memory", "--no-claw")

    def test_identical_for_reverse_direction_only(self):
        # 交换两侧顺序，走的是 b2a 主导的路径。
        self.a, self.b = self.b, self.a
        self._assert_same_plan()

    def test_plan_id_changes_when_source_changes(self):
        """漂移检测本身也要两边一致：源数据改了，plan_id 必须换。"""
        before = self._assert_same_plan()[0]["plan_id"]
        con = sqlite3.connect(os.path.join(self.a, "workbuddy.db"))
        con.execute(
            "INSERT INTO sessions ({}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)".format(
                ", ".join(SESSION_COLUMNS)),
            _session("s-new", self.root, self.uid_a, title="新加的会话"),
        )
        con.commit()
        con.close()
        after_py = self._python_plan()
        after_go = self._go_plan()
        self.assertNotEqual(before, after_py["plan_id"], "源数据变了 plan_id 却没变")
        self.assertEqual(after_py["plan_id"], after_go["plan_id"])

    def test_survey_agrees_on_counts(self):
        def run(cmd):
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  env=dict(os.environ, PYTHONPATH=TOOLS))
            if proc.returncode != 0:
                self.fail(proc.stderr[-800:])
            return json.loads(proc.stdout)

        py = run([sys.executable, os.path.join(TOOLS, "wb_home_bridge.py"),
                  "survey", "--json", "--home-a", self.a, "--home-b", self.b])
        go = run([self.bin, "survey", "--json", "--home-a", self.a, "--home-b", self.b])
        for side in (0, 1):
            for key in ("uid", "nickname", "counts", "sizes", "skills", "invalid_cwd"):
                self.assertEqual(
                    py["homes"][side][key], go["homes"][side][key],
                    f"第 {side} 个 home 的 {key} 不一致",
                )


if __name__ == "__main__":
    unittest.main()
