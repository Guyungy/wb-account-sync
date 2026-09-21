"""pyjson golden 夹具的时效性与正确性。

## 为什么单独有这个文件

Rust 侧 `crates/wb-core/tests/pyjson_golden.rs` 拿这个夹具当**真基准**，
但它只能证明「Rust 的输出 == 夹具里的 expected」。它证明不了**夹具本身是对的**——
如果夹具里写错了期望值，Rust 测试照样全绿，而 `plan_id` 会悄悄错掉。

所以这里补上夹具的自我校验，两个方向各一条：

1. **夹具是否最新**：重新生成一遍，与磁盘上的逐字节比对。
   改了用例却忘了重跑生成器时，这条会红。
2. **夹具是否真的等于 CPython 的输出**：逐例重算 `json.dumps`。
   Python 版本变化导致浮点格式变化时，这条会红。

（夹具里的 `json` 字段是**人工撰写的输入文本**，`expected` 是**算出来的**，
所以不构成循环论证 —— 被测的正是「输入 → 输出」这一步变换。）
"""

import hashlib
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "pyjson_golden.json")

if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import gen_pyjson_golden  # noqa: E402  同仓库工具，直接拿来重算


def load() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class FixtureFreshnessTest(unittest.TestCase):
    """夹具必须是当前生成器的产物。"""

    def test_fixture_is_up_to_date(self) -> None:
        with open(FIXTURE, encoding="utf-8") as fh:
            on_disk = fh.read()
        regenerated = gen_pyjson_golden.render(gen_pyjson_golden.build_fixture())
        self.assertEqual(
            on_disk,
            regenerated,
            "夹具已过时。重跑：python3 tools/gen_pyjson_golden.py",
        )

    def test_check_flag_agrees(self) -> None:
        # --check 是 CI/脚本用的入口，它若与测试口径不一致就会两头骗人。
        self.assertEqual(gen_pyjson_golden.main.__module__, "gen_pyjson_golden")
        self.assertEqual(0, _run_check())


def _run_check() -> int:
    old_argv = sys.argv
    sys.argv = ["gen_pyjson_golden.py", "--check"]
    try:
        return gen_pyjson_golden.main()
    finally:
        sys.argv = old_argv


class ExpectedValuesMatchCpythonTest(unittest.TestCase):
    """夹具里每一例的 expected 都必须等于 CPython 现场算出的值。"""

    def test_compact_expected_matches_cpython(self) -> None:
        fixture = load()
        cases = fixture["compact"]
        self.assertTrue(cases, "compact 分组为空，等于没测")
        for case in cases:
            with self.subTest(label=case["label"]):
                value = json.loads(case["json"])
                self.assertEqual(
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                    case["expected"],
                )

    def test_indent_expected_matches_cpython(self) -> None:
        fixture = load()
        cases = fixture["indent2"]
        self.assertTrue(cases, "indent2 分组为空，等于没测")
        for case in cases:
            with self.subTest(label=case["label"]):
                value = json.loads(case["json"])
                self.assertEqual(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=case["indent"],
                    ),
                    case["expected"],
                )

    def test_hashes_match_expected(self) -> None:
        fixture = load()
        for group in ("compact", "indent2"):
            for case in fixture[group]:
                with self.subTest(group=group, label=case["label"]):
                    self.assertEqual(
                        hashlib.sha256(case["expected"].encode("utf-8")).hexdigest(),
                        case["sha256"],
                    )


class CorpusCoverageTest(unittest.TestCase):
    """关键边界必须一直在夹具里 —— 与 Rust 侧的断言互为镜像。

    没有这条的话，某次"清理用例"把最危险的边界删掉，两边测试都会继续绿。
    """

    CRITICAL = (
        # CPython 定点/科学计数的两个切换点，两侧都要有
        "float_1e15",
        "float_1e16",
        "float_1e-4",
        "float_1e-5",
        "float_boundary_hi",
        "float_boundary_lo",
        # 负零：符号位丢了哈希就变
        "float_neg_zero",
        # 转义边界：C0 要转、DEL 与 `/` 不能转
        "str_c0_1f",
        "str_del_7f",
        "str_forward_slash",
        # sort_keys 的排序口径
        "obj_keysort_ascii",
        "obj_cjk_keys",
        # int 与 float 不能混
        "int_zero",
        "float_one",
        # 贴近真实计划体
        "obj_plan_body",
        "obj_plan_with_entries",
    )

    def test_critical_cases_present(self) -> None:
        labels = {c["label"] for c in load()["compact"]}
        missing = [label for label in self.CRITICAL if label not in labels]
        self.assertEqual([], missing, f"夹具缺关键用例: {missing}")

    def test_indent_group_covers_empty_containers(self) -> None:
        # 缩进模式下空容器要压成 {} / []，这条最容易在重构时丢。
        labels = {c["label"] for c in load()["indent2"]}
        self.assertIn("indent_empty_containers", labels)
        self.assertIn("indent_empty_top", labels)


class IntFloatDistinctionTest(unittest.TestCase):
    """夹具里 1 与 1.0 必须同时存在。

    真实数据里 SQLite 的 INTEGER 与 REAL 会分别落到两条路径上；
    夹具如果只有一边，Rust 侧把 int 当 float 处理也不会被发现。
    """

    def test_both_int_and_float_present(self) -> None:
        values = [json.loads(c["json"]) for c in load()["compact"]]
        self.assertTrue(any(isinstance(v, int) and not isinstance(v, bool) for v in values))
        self.assertTrue(any(isinstance(v, float) for v in values))

    def test_int_and_float_render_differently(self) -> None:
        self.assertEqual("1", json.dumps(1, ensure_ascii=False, sort_keys=True))
        self.assertEqual("1.0", json.dumps(1.0, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
