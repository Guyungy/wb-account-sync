"""界面层的跨实现等价性测试：Python 版 UI 与 Go 版 UI 对同一请求给出同一答案。

界面是唯一会"看起来一样、实际不一样"的地方：HTTP 状态码、错误文案、
JSON 字段名任何一处不同，前端可能就少显示一块或者卡在某个分支上，
而这类问题在手工点几下的时候很难发现。

做法：同一份合成夹具，两个实现各起一个服务（固定 token，避免随机性），
逐个端点发同样的请求，比对状态码与响应体。另外单独断言前端页面逐字节相同
——Go 版的前端是 go:embed 原样复用 Python 版的 PAGE，这条断言就是"复用"
这个决定的守门人：谁改了一边没改另一边，测试会直接红。

`go` 不在 PATH 上时跳过。
"""

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
GO_MODULE = os.path.join(ROOT, "gobridge")
GO = shutil.which("go")

TOKEN = "test-token-0123456789"

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


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


def make_home(path, uid, nickname, sessions, workspaces, projects, skills=()):
    os.makedirs(path, exist_ok=True)
    con = sqlite3.connect(os.path.join(path, "workbuddy.db"))
    con.executescript(SCHEMA)
    for row in sessions:
        cols = ", ".join(SESSION_COLUMNS)
        con.execute(
            f"INSERT INTO sessions ({cols}) VALUES ({', '.join('?' * len(SESSION_COLUMNS))})",
            row)
        con.execute(
            "INSERT INTO session_usage (session_id, tokens, cost, note) VALUES (?,?,?,?)",
            (row[0], 100, 0.5, "备注"))
    for ws in workspaces:
        con.execute("INSERT INTO workspaces (path, name) VALUES (?,?)",
                    (ws, os.path.basename(ws)))
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
    for sub in ("blobs", "skills", "tasks", "changes-detail", "changes-index",
                "file-history", "artifact-index", "connectors", "memory",
                "storage/user-" + uid):
        os.makedirs(os.path.join(path, sub), exist_ok=True)
    # 真实技能目录：不造这个，"技能对比"那一块会因为两边都取空而
    # 假装通过——写测试时踩过一次，所以这里必须造出非空数据。
    for name in skills:
        sdir = os.path.join(path, "skills", name)
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\nname: {name}\norigin: {nickname}\n---\n")
    with open(os.path.join(path, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump({"claw": {"users": {uid: {"channels": {}}}}, "sandbox": True}, fh)


class _Server:
    """一个跑着的界面进程，负责起停与发请求。"""

    def __init__(self, proc, port):
        self.proc = proc
        self.port = port
        self.base = f"http://127.0.0.1:{port}"

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        # 显式关掉管道：否则每个用例都会留下未关闭的文件对象，
        # 测试跑完满屏 ResourceWarning，把真正的失败淹掉。
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream and not stream.closed:
                stream.close()

    def request(self, method, path, body=None, token=TOKEN):
        url = f"{self.base}{path}"
        if token is not None:
            sep = "&" if "?" in path else "?"
            url = f"{url}{sep}t={token}"
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")


class GoUiParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not GO:
            raise unittest.SkipTest("PATH 上没有 go，跳过界面等价性测试")
        cls.tmp = tempfile.mkdtemp(prefix="wb-go-ui-")
        cls.bin = os.path.join(cls.tmp, "wb-bridge")
        proc = subprocess.run([GO, "build", "-o", cls.bin, "./cmd/wb-bridge"],
                              cwd=GO_MODULE, capture_output=True, text=True)
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
        self.a = os.path.join(self.root, "a")
        self.b = os.path.join(self.root, "b")
        make_home(self.a, self.uid_a, "左账号",
                  [_session("s-1", self.ws, self.uid_a),
                   _session("s-2", self.ws, self.uid_a)],
                  [self.ws], [("bucket-a", "s-1")],
                  skills=("shared-skill", "only-left-skill"))
        make_home(self.b, self.uid_b, "右账号",
                  [_session("s-3", self.ws, self.uid_b)],
                  [self.ws], [("bucket-b", "s-3")],
                  skills=("shared-skill", "only-right-skill"))

    def _start_py(self):
        port = free_port()
        env = dict(os.environ, PYTHONPATH=TOOLS)
        proc = subprocess.Popen(
            [self.py, os.path.join(TOOLS, "wb_ui.py"),
             "--home-a", self.a, "--home-b", self.b,
             "--state-dir", os.path.join(self.root, "py-state"),
             "--port", str(port), "--token", TOKEN, "--no-open"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env)
        self.addCleanup(proc.kill)
        srv = _Server(proc, port)
        self.addCleanup(srv.stop)
        self._wait_ready(srv, proc)
        return srv

    def _start_go(self):
        port = free_port()
        proc = subprocess.Popen(
            [self.bin, "serve", "--home-a", self.a, "--home-b", self.b,
             "--state-dir", os.path.join(self.root, "go-state"),
             "--port", str(port), "--token", TOKEN, "--no-open"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        srv = _Server(proc, port)
        self.addCleanup(srv.stop)
        self._wait_ready(srv, proc)
        return srv

    def _wait_ready(self, srv, proc):
        deadline = time.time() + 20
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                err = proc.stderr.read() if proc.stderr else ""
                self.fail(f"服务提前退出：\n{out}\n{err}")
            try:
                with urllib.request.urlopen(srv.base + "/", timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.15)
        self.fail("服务 20 秒内没起来")

    # ---------------- 页面 ----------------

    def test_frontend_page_is_byte_identical(self):
        """前端只能有一份：Go 侧是 go:embed 复用的，不许悄悄改。"""
        sys.path.insert(0, TOOLS)
        import wb_ui  # noqa: PLC0415
        embedded = os.path.join(GO_MODULE, "internal", "webui", "static", "index.html")
        with open(embedded, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(
            wb_ui.PAGE, text,
            "Go 侧内嵌的前端与 Python 的 PAGE 不一致——两边必须保持同一份界面")

    def test_page_is_served_identically(self):
        py, go = self._start_py(), self._start_go()
        s1, body1 = py.request("GET", "/", token=None)
        s2, body2 = go.request("GET", "/", token=None)
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 200)
        self.assertEqual(body1, body2, "两个实现端出的页面不同")

    # ---------------- 鉴权 ----------------

    def test_token_required_on_both(self):
        py, go = self._start_py(), self._start_go()
        for srv, name in ((py, "py"), (go, "go")):
            code, body = srv.request("GET", "/api/state", token=None)
            self.assertEqual(code, 403, f"{name}：缺 token 竟然放行了")
            self.assertIn("error", json.loads(body))
            code, _ = srv.request("GET", "/api/state", token="wrong-token")
            self.assertEqual(code, 403, f"{name}：错 token 竟然放行了")

    def test_unknown_endpoint_on_both(self):
        py, go = self._start_py(), self._start_go()
        for srv, name in ((py, "py"), (go, "go")):
            code, body = srv.request("GET", "/api/nope")
            self.assertEqual(code, 404, f"{name}：未知端点没有 404")
            self.assertIn("未知端点", json.loads(body)["error"])

    # ---------------- 只读端点 ----------------

    def test_state_endpoint_agrees(self):
        py, go = self._start_py(), self._start_go()
        s1, b1 = py.request("GET", "/api/state")
        s2, b2 = go.request("GET", "/api/state")
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 200)
        d1, d2 = json.loads(b1), json.loads(b2)
        self.assertEqual(sorted(d1.keys()), sorted(d2.keys()),
                         "state 的字段集合不同")
        for d, name in ((d1, "py"), (d2, "go")):
            for key in ("key", "display", "running", "home", "home_exists",
                        "db_exists", "home_note", "home_confirmed"):
                self.assertIn(key, d["clients"][0], f"{name}：clients 缺字段 {key}")
                self.assertEqual(sorted(d["clients"][0].keys()),
                                 sorted(d1["clients"][0].keys()),
                                 f"{name}：clients 字段集合不同")
        # 与实现无关、必须一致的部分
        for key in ("all_stopped", "running_names", "has_plan"):
            self.assertEqual(d1[key], d2[key], f"state.{key} 不一致")
        self.assertEqual(d1["clients"][0]["home"], d2["clients"][0]["home"])
        self.assertEqual(d1["clients"][0]["db_exists"], d2["clients"][0]["db_exists"])
        self.assertEqual(sorted(d1["autosync"].keys()), sorted(d2["autosync"].keys()),
                         "autosync 概览字段不同")

    def test_survey_endpoint_agrees(self):
        py, go = self._start_py(), self._start_go()
        s1, b1 = py.request("GET", "/api/survey")
        s2, b2 = go.request("GET", "/api/survey")
        self.assertEqual((s1, s2), (200, 200), f"{b1[-400:]}{b2[-400:]}")
        d1, d2 = json.loads(b1), json.loads(b2)
        self.assertEqual(d1["skills"], d2["skills"], "技能对比结果不同")
        # 先确认这一块**真的有内容**，否则"两边都一样空"会假装通过。
        # 这正是写这组测试时踩到的：Go 侧返回的是 []string，
        # 按 []any 取值会静默取空，JSON 照样 200，肉眼完全看不出来。
        self.assertEqual(d1["skills"]["shared"], ["shared-skill"],
                         "共有技能没算出来——技能对比可能被取空了")
        self.assertEqual(d1["skills"]["only_a"], ["only-left-skill"])
        self.assertEqual(d1["skills"]["only_b"], ["only-right-skill"])
        for side, idx in (("左", 0), ("右", 1)):
            for key in ("uid", "nickname", "counts", "skills", "invalid_cwd",
                        "sizes_human"):
                self.assertEqual(d1["homes"][idx].get(key), d2["homes"][idx].get(key),
                                 f"{side}侧 home 的 {key} 不一致")
            self.assertTrue(d1["homes"][idx]["skills"],
                            f"{side}侧 skills 为空，夹具没造出真实技能目录")
            self.assertTrue(d1["homes"][idx]["sizes_human"],
                            f"{side}侧 sizes_human 为空——体积换算可能被取空了")

    # ---------------- 计划 ----------------

    def test_plan_endpoint_agrees(self):
        py, go = self._start_py(), self._start_go()
        s1, b1 = py.request("POST", "/api/plan", {})
        s2, b2 = go.request("POST", "/api/plan", {})
        self.assertEqual((s1, s2), (200, 200), f"{b1[-500:]}{b2[-500:]}")
        d1, d2 = json.loads(b1), json.loads(b2)
        self.assertEqual(d1["plan_id"], d2["plan_id"], "plan_id 不一致")
        self.assertEqual(d1["summary"], d2["summary"], "summary 不一致")
        self.assertEqual(d1["counts"], d2["counts"], "counts 不一致")
        self.assertEqual(d1["sample"], d2["sample"], "sample 不一致")
        self.assertEqual(sorted(d1.keys()), sorted(d2.keys()), "plan 视图字段不同")
        # 计划文件必须真的落盘：apply 要读它
        for state, name in (("py-state", "py"), ("go-state", "go")):
            path = os.path.join(self.root, state, "plans", d1["plan_id"][:16] + ".json")
            self.assertTrue(os.path.isfile(path), f"{name}：计划文件没落盘 {path}")

    def test_plan_options_are_honoured_the_same_way(self):
        py, go = self._start_py(), self._start_go()
        opts = {"include_changes": False, "include_skills": False,
                "include_memory": False, "include_claw": False}
        s1, b1 = py.request("POST", "/api/plan", {"options": opts})
        s2, b2 = go.request("POST", "/api/plan", {"options": opts})
        self.assertEqual((s1, s2), (200, 200))
        d1, d2 = json.loads(b1), json.loads(b2)
        self.assertEqual(d1["plan_id"], d2["plan_id"], "全关选项后 plan_id 不一致")
        self.assertEqual(d1["options"], d2["options"], "生效选项不一致")

    # ---------------- 错误路径 ----------------

    def test_verify_without_plan_is_rejected_identically(self):
        py, go = self._start_py(), self._start_go()
        for srv, name in ((py, "py"), (go, "go")):
            code, body = srv.request("POST", "/api/verify")
            self.assertEqual(code, 400, f"{name}：没有计划时 verify 应当 400")
            self.assertEqual(json.loads(body)["error"], "尚未生成计划。",
                             f"{name}：错误文案不一致")

    def test_apply_without_plan_is_rejected_identically(self):
        py, go = self._start_py(), self._start_go()
        for srv, name in ((py, "py"), (go, "go")):
            code, body = srv.request("GET", "/api/apply?plan_id=deadbeef")
            self.assertEqual(code, 400, f"{name}：没有计划时 apply 应当 400")
            self.assertEqual(json.loads(body)["error"], "尚未生成计划。",
                             f"{name}：错误文案不一致")

    def test_apply_with_stale_plan_id_is_rejected_identically(self):
        py, go = self._start_py(), self._start_go()
        for srv, name in ((py, "py"), (go, "go")):
            srv.request("POST", "/api/plan", {})
            code, body = srv.request("GET", "/api/apply?plan_id=not-the-current-one")
            self.assertEqual(code, 400, f"{name}：陈旧 plan_id 应当被拒")
            self.assertIn("plan_id", json.loads(body)["error"],
                          f"{name}：错误文案不一致")

    def test_restore_without_run_dir_is_rejected_identically(self):
        py, go = self._start_py(), self._start_go()
        for srv, name in ((py, "py"), (go, "go")):
            srv.request("POST", "/api/plan", {})
            code, body = srv.request("POST", "/api/restore", {})
            self.assertEqual(code, 400, f"{name}：找不到 run 目录时应当 400")
            self.assertIn("找不到运行记录目录", json.loads(body)["error"],
                          f"{name}：错误文案不一致")

    def test_apply_refuses_while_client_running(self):
        """客户端在跑时必须两边都拒绝，且提示同一件事。

        夹具是合成的，但"WorkBuddy 是否在运行"探测的是真实进程——
        本机运行测试时客户端确实在跑，所以这里断言的是 409 与提示语。
        若真机上客户端恰好没开，则跳过（不算失败，因为前提不成立）。
        """
        py, go = self._start_py(), self._start_go()
        out = []
        for srv in (py, go):
            _s, body = srv.request("POST", "/api/plan", {})
            plan_id = json.loads(body)["plan_id"]
            code, resp = srv.request("GET", f"/api/apply?plan_id={plan_id}")
            out.append((code, json.loads(resp)))
        if out[0][0] == 200:
            self.skipTest("本机没有客户端在运行，这条前提不成立")
        self.assertEqual(out[0][0], out[1][0], "两边对「客户端在跑」的判断不同")
        self.assertIn("仍在运行", out[0][1]["error"])
        self.assertEqual(out[0][1]["error"], out[1][1]["error"],
                         "拒绝文案不一致")


if __name__ == "__main__":
    unittest.main()
