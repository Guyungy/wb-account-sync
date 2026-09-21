"""CLI 的 --json 输出契约：Go 版与 Python 版必须逐字段一致。

为什么单独有这个文件：
`test_go_ui_parity` 守的是 HTTP 端点，`test_go_apply_parity` 守的是执行**结果**
（数据库内容、文件树）。中间还空着一层——**CLI 自己的 --json 输出**。
它一直没有测试，于是 Go 版 `survey --json` 少带 `version` 字段这件事
谁都没发现：脚本消费这个字段时才会踩到，而那时人已经在用命令行拼流程了。

所以这里补上：对两边同名的子命令，在同一份夹具上各跑一次，
比对 stdout JSON 的**键结构**与**稳定字段值**。
只读命令（survey / plan）比全量；写命令的语义等价性归 test_go_apply_parity 管，
这里只比它们的输出**结构**，避免两处重复维护同一套断言。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

try:  # unittest discover 时是顶层模块，直接跑文件时是包内模块
    from tests.test_go_apply_parity import (
        GO, GO_MODULE, ROOT, _session, make_home,
    )
except ImportError:  # pragma: no cover - 取决于运行方式
    from test_go_apply_parity import (  # type: ignore
        GO, GO_MODULE, ROOT, _session, make_home,
    )

TOOLS = os.path.join(ROOT, "tools")


def key_shape(obj, path="", out=None, depth=0, max_depth=4):
    """递归收集「每个路径上有哪些键」，用来比结构而不是比值。"""
    if out is None:
        out = {}
    if depth > max_depth:
        return out
    if isinstance(obj, dict):
        out[path] = sorted(obj.keys())
        for key, val in obj.items():
            key_shape(val, f"{path}.{key}", out, depth + 1, max_depth)
    elif isinstance(obj, list) and obj:
        key_shape(obj[0], f"{path}[]", out, depth + 1, max_depth)
    return out


def tree_digest(root):
    """整棵目录树的内容哈希，用来证明"一个字节都没动"。"""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            digest.update(os.path.relpath(path, root).encode("utf-8"))
            with open(path, "rb") as fh:
                digest.update(fh.read())
    return digest.hexdigest()


def add_skill(home, *names):
    """往 home/skills 里塞真实技能目录。

    至少要让这一块**非空**：两边都取空时"结构相同"照样成立，
    那样测试会全绿地掩盖"取不到数据"这一类缺陷。
    """
    root = os.path.join(home, "skills")
    os.makedirs(root, exist_ok=True)
    for name in names:
        skill_dir = os.path.join(root, name)
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\nname: {name}\n---\n\n说明。\n")


class CliContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not GO:
            raise unittest.SkipTest("PATH 上没有 go，跳过 CLI 契约测试")
        cls.tmp = tempfile.mkdtemp(prefix="wb-go-cli-")
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

        # 两侧共用同一份路径，plan_id 才具备可比性：
        # 计划正文里含 home 的绝对路径，路径不同则指纹天然不同。
        self.a = os.path.join(self.root, "a")
        self.b = os.path.join(self.root, "b")
        make_home(
            self.a, self.uid_a, "左账号",
            sessions=[_session("s-1", self.ws, self.uid_a),
                      _session("s-both", self.ws, self.uid_a)],
            workspaces=[self.ws],
            projects=[("bucket-a", "s-1")],
        )
        make_home(
            self.b, self.uid_b, "右账号",
            sessions=[_session("s-3", self.ws, self.uid_b),
                      _session("s-both", self.ws, self.uid_b)],
            workspaces=[self.ws],
            projects=[("bucket-b", "s-3")],
        )
        # 左侧多两个、右侧多一个，中间共享一个 —— 结构非空才有区分度。
        add_skill(self.a, "shared-skill", "only-left-1", "only-left-2")
        add_skill(self.b, "shared-skill", "only-right-1")

    # ---------------- 运行器 ----------------

    def _run_raw(self, impl, *args):
        if impl == "py":
            cmd = [self.py, os.path.join(TOOLS, "wb_home_bridge.py"), *args]
        else:
            cmd = [self.bin, *args]
        return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)

    def _run(self, impl, *args):
        proc = self._run_raw(impl, *args)
        self.assertEqual(proc.returncode, 0,
                         f"{impl} {' '.join(args)} 失败：{proc.stderr[-500:]}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"{impl} {' '.join(args)} 的 stdout 不是 JSON：{exc}\n"
                      f"{proc.stdout[:300]}")

    def _both(self, *args):
        return (self._run("py", *args),
                self._run("go", *args))

    # ---------------- 断言 ----------------

    def _assert_same_shape(self, py, go, label):
        sp, sg = key_shape(py), key_shape(go)
        for path in sorted(set(sp) | set(sg)):
            self.assertEqual(
                sp.get(path), sg.get(path),
                f"{label}: 路径 {path or '<root>'} 的键集合不一致\n"
                f"  PY: {sp.get(path)}\n  GO: {sg.get(path)}",
            )

    # ---------------- 用例 ----------------

    def test_survey_json_contract_matches(self):
        args = ("survey", "--json", "--home-a", self.a, "--home-b", self.b)
        py, go = self._both(*args)

        self._assert_same_shape(py, go, "survey")

        # 顶层 version 是脚本用来判断字段含义有没有变的，不能少。
        self.assertIn("version", go, "Go survey 顶层缺 version 字段")
        self.assertEqual(py["version"], go["version"])

        for idx, side in enumerate(("左", "右")):
            for key in ("uid", "nickname", "counts", "skills", "invalid_cwd",
                        "sessions_by_user", "warnings"):
                self.assertEqual(
                    py["homes"][idx].get(key), go["homes"][idx].get(key),
                    f"survey homes[{idx}]（{side}）的 {key} 不一致",
                )

        # 非空断言：夹具退化成空目录时，"两边相同"一样成立。
        self.assertTrue(go["homes"][0]["skills"], "左侧技能列表为空，夹具没生效")
        self.assertTrue(go["homes"][1]["skills"], "右侧技能列表为空，夹具没生效")
        self.assertEqual(go["homes"][0]["counts"]["sessions"], 2)
        self.assertEqual(go["homes"][1]["counts"]["sessions"], 2)

    def test_plan_json_contract_matches(self):
        args = ("plan", "--json", "--home-a", self.a, "--home-b", self.b)
        py, go = self._both(*args)

        self._assert_same_shape(py, go, "plan")

        # 同一份夹具、同一批路径 → 内容指纹必须逐字节同值。
        self.assertEqual(py["plan_id"], go["plan_id"], "plan_id 不一致")
        self.assertEqual(py["entries"], go["entries"], "entries 不一致")
        self.assertEqual(py["source_fingerprints"], go["source_fingerprints"],
                         "source_fingerprints 不一致")
        self.assertEqual(py["summary"], go["summary"], "summary 不一致")
        self.assertEqual(py["rows"], go["rows"], "rows 不一致")
        self.assertEqual(py["skipped"], go["skipped"], "skipped 不一致")

        # 非空断言：没有条目就说明夹具没造出「需要同步的差异」。
        self.assertTrue(go["entries"], "计划里没有任何条目，夹具没生效")
        self.assertGreater(go["summary"]["totals"]["sessions_to_copy"], 0)

    def test_verify_json_contract_matches(self):
        """verify 的两条契约，外加一条**已知差异**。

        契约一：未执行过 apply 时，两边都要判「未通过」并给出同一个返回码。
        契约二：`--json` 模式下 stdout 必须是纯 JSON（日志走 stderr）。

        已知差异：Python 侧 verify 的 `--json` 是**死参数**——argparse 里注册了，
        实现里从没用过，只打印人类可读文本。这里显式断言这个差异存在，
        而不是假装两边一致：一旦有人给 Python 补上，这条断言会红，
        提醒把差异记录删掉。Python 侧即将整体退役（legacy-python/），
        为了一个正在退场的实现去重构它的输出层并不划算。
        """
        plan_file = os.path.join(self.root, "plan.json")
        self._run("go", "plan", "--json", "--home-a", self.a, "--home-b", self.b,
                  "--output", plan_file)

        proc_py = self._run_raw("py", "verify", "--plan", plan_file,
                                "--home-a", self.a, "--home-b", self.b, "--json")
        proc_go = self._run_raw("go", "verify", "--plan", plan_file,
                                "--home-a", self.a, "--home-b", self.b, "--json")

        # 契约一：结论与退出码一致，且确实是"未通过"
        self.assertEqual(proc_py.returncode, proc_go.returncode,
                         "verify 的返回码不一致："
                         f"PY={proc_py.returncode} GO={proc_go.returncode}")
        self.assertEqual(proc_go.returncode, 3,
                         "计划尚未执行，verify 应当判未通过（退出码 3）")

        # 契约二：Go 的 --json 输出必须可解析
        go = json.loads(proc_go.stdout)
        self.assertIn("ok", go)
        self.assertFalse(go["ok"], "未执行时 ok 应为 false")

        # 已知差异：Python 侧仍是非 JSON 文本
        with self.assertRaises(json.JSONDecodeError):
            json.loads(proc_py.stdout)

    def test_json_mode_stdout_is_pure_json(self):
        """所有只读命令的 `--json`：stdout 必须是可解析的 JSON。

        这条专门守「日志混流」。只要有人在 --json 路径上多打一行进度，
        管道消费方就会在第一个非 JSON 字符上失败，而报错信息会指向
        「JSON 解析错误」，跟真正的成因隔了一层——所以必须由测试来钉住。
        """
        cases = (
            ("status", ()),
            ("survey", ("--home-a", self.a, "--home-b", self.b)),
            ("plan", ("--home-a", self.a, "--home-b", self.b)),
        )
        for cmd, extra in cases:
            proc = self._run_raw("go", cmd, "--json", *extra)
            self.assertEqual(proc.returncode, 0,
                             f"{cmd} 失败：{proc.stderr[-300:]}")
            try:
                json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                self.fail(f"`wb-bridge {cmd} --json` 的 stdout 不是纯 JSON："
                          f"{exc}\n前 200 字节：{proc.stdout[:200]!r}")

    def test_sync_dry_run_writes_nothing(self):
        """`sync --dry-run` 必须真的一个字节都不写。

        这条守的是「预演」这个承诺本身：使用者正是靠它来判断
        「如果真同步会发生什么」。一旦它在背后偷偷落了盘，
        这个判断就失去了依据，而后果是不可逆的。
        """
        before = (tree_digest(self.a), tree_digest(self.b))
        proc = self._run_raw("go", "sync", "--dry-run",
                             "--home-a", self.a, "--home-b", self.b,
                             "--state-dir", os.path.join(self.root, "state"))
        self.assertEqual(proc.returncode, 0, f"sync --dry-run 失败：{proc.stderr[-500:]}")
        self.assertIn("没有写入任何数据", proc.stdout)
        # dry-run 也不该去动别人的客户端
        self.assertNotIn("正在请求退出", proc.stdout)
        after = (tree_digest(self.a), tree_digest(self.b))
        self.assertEqual(before, after, "dry-run 修改了数据目录")

    def test_sync_dry_run_reports_real_plan(self):
        """预演给出的计划必须与单独跑 plan 得到的一致。

        否则预演就是在报一个不会发生的数字 —— 比不给预演更糟。
        """
        proc = self._run_raw("go", "sync", "--dry-run", "--json",
                             "--home-a", self.a, "--home-b", self.b,
                             "--state-dir", os.path.join(self.root, "state"))
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        dry = json.loads(proc.stdout)
        self.assertTrue(dry["dry_run"])

        direct = self._run("go", "plan", "--json",
                           "--home-a", self.a, "--home-b", self.b)
        self.assertEqual(dry["plan_id"], direct["plan_id"],
                         "预演的计划与直接生成的不是同一份")
        self.assertEqual(dry["plan"]["entries"], direct["entries"])
        self.assertGreater(direct["summary"]["totals"]["sessions_to_copy"], 0)

    def test_unknown_flag_fails_on_both(self):
        """错误路径也要一致：不认识的参数不能被一边静默忽略。"""
        for impl in ("py", "go"):
            cmd = ([self.py, os.path.join(TOOLS, "wb_home_bridge.py")]
                   if impl == "py" else [self.bin])
            proc = subprocess.run(cmd + ["survey", "--definitely-not-a-flag"],
                                  capture_output=True, text=True, cwd=ROOT)
            self.assertNotEqual(proc.returncode, 0,
                                f"{impl} 对未知参数没有报错")


if __name__ == "__main__":
    unittest.main()
