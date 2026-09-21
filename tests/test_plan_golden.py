"""plan golden 夹具的时效性、覆盖度与一条安全断言。

## 三层校验里，这个文件负责两层

Rust 侧 `crates/wb-core/tests/plan_golden.rs` 证明的是
「Rust 的 plan_id == 夹具里的 expected_plan_id」。它证明不了**夹具本身是对的** ——
夹具写错了，Rust 测试照样绿，而 `plan_id` 会悄悄错掉。

所以这里补上：

1. **夹具是否最新**：重新生成一遍，逐字节比对（改了夹具或选项却忘了重跑生成器时红）。
2. **夹具是否等于 CPython 的输出**：`build_plan` 会重新跑一遍，
   `expected_*` 字段与重算结果比对。

另外加一条**安全断言**：连接器凭据（`.master.key`）绝不能出现在计划里。
这不是"测试覆盖"，是产品承诺 —— 放到这里是因为夹具里真的埋了一个
`connectors/<uid>/.master.key` 文件，正好能验证它没被卷进条目。
"""

import json
import os
import sys
import unittest
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
GOLDEN = os.path.join(ROOT, "tests", "fixtures", "plan_golden.json")

if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import gen_plan_golden  # noqa: E402


def load() -> dict:
    with open(GOLDEN, encoding="utf-8") as fh:
        return json.load(fh)


class FixtureFreshnessTest(unittest.TestCase):
    def test_fixture_is_up_to_date(self) -> None:
        with open(GOLDEN, encoding="utf-8") as fh:
            on_disk = fh.read()
        regenerated = gen_plan_golden.render(gen_plan_golden.build_golden())
        self.assertEqual(
            on_disk,
            regenerated,
            "夹具已过时。重跑：python3 tools/gen_plan_golden.py",
        )

    def test_no_environment_metadata(self) -> None:
        # 夹具里唯一允许出现的绝对路径是固定夹具路径本身。
        # 夹带了 $TMPDIR / 解释器版本 / 时间戳，两边就永远对不上。
        text = json.dumps(load(), ensure_ascii=False)
        self.assertNotIn("python", load().keys(), "夹具不该记录解释器版本")
        for token in ("/var/folders", "site-packages", "T00:00:00"):
            self.assertNotIn(token, text, f"夹具里不该出现 {token}")


class ExpectedMatchesCpythonTest(unittest.TestCase):
    """夹具里的期望值必须等于现场重算的结果。"""

    def test_expected_values_are_reproducible(self) -> None:
        golden = load()
        fresh = gen_plan_golden.build_golden()
        self.assertEqual(
            [c["expected_plan_id"] for c in golden["cases"]],
            [c["expected_plan_id"] for c in fresh["cases"]],
            "重算得到的 plan_id 与夹具不一致",
        )
        self.assertEqual(
            [c["expected_body_canonical"] for c in golden["cases"]],
            [c["expected_body_canonical"] for c in fresh["cases"]],
            "重算得到的计划正文与夹具不一致",
        )

    def test_plan_id_is_the_hash_of_the_canonical_body(self) -> None:
        # 这条把"plan_id 是什么"钉死：它就是规范编码的 SHA-256。
        # 若哪天有人往正文里加了时间戳之类的易变字段，这条会红。
        import hashlib
        for case in load()["cases"]:
            with self.subTest(label=case["label"]):
                digest = hashlib.sha256(
                    case["expected_body_canonical"].encode("utf-8")
                ).hexdigest()
                self.assertEqual(case["expected_plan_id"], digest)


class CoverageTest(unittest.TestCase):
    def test_every_entry_kind_appears(self) -> None:
        # 夹具太薄的话，条目构造那几十行有一半跑不到，
        # 这时"plan_id 一致"说明不了什么。所以把覆盖度也钉住。
        notes = Counter(
            e.get("note", "")
            for case in load()["cases"]
            for e in case["expected_entries"]
        )
        for expected in (
            "conversation",
            "tool-results",
            "session asset",
            "content-addressed union",
            "union",
            "connector state (no credentials)",
            "account storage (merge-if-missing)",
            "connector skills",
        ):
            self.assertIn(expected, notes, f"夹具没有覆盖条目类型：{expected}")

    def test_both_directions_have_skips(self) -> None:
        # 两边共有同一个会话 id，所以两个方向都应有跳过 ——
        # 只测单向的话，"跳过去重"这条逻辑可能根本没被执行。
        case = load()["cases"][0]
        self.assertEqual(set(case["expected_skipped"]), {"a2b", "b2a"})
        for side, counts in case["expected_skipped"].items():
            self.assertGreater(counts.get("sessions", 0), 0, f"{side} 没有跳过任何会话")

    def test_entry_count_is_not_trivially_small(self) -> None:
        for case in load()["cases"]:
            self.assertGreater(
                case["expected_entry_count"], 10,
                f"[{case['label']}] 条目太少（{case['expected_entry_count']}），"
                "夹具偏薄，等价性证明力不足",
            )


class CredentialsNeverEnterThePlanTest(unittest.TestCase):
    """连接器凭据绝不能进计划 —— 这是产品承诺，不是测试便利。"""

    def test_master_key_absent_from_every_entry(self) -> None:
        for case in load()["cases"]:
            for entry in case["expected_entries"]:
                for field in ("src", "dst"):
                    self.assertNotIn(
                        ".master.key", entry[field],
                        f"[{case['label']}] 凭据出现在 {field}：{entry[field]}",
                    )

    def test_fixture_actually_contains_a_credential_file(self) -> None:
        # 反向校验：夹具里必须真的埋着一个 .master.key，
        # 否则上面那条断言只是在证明"一个不存在的东西没出现"。
        files = [
            rel
            for home in load()["fixture"]["homes"].values()
            for rel, _ in home["files"]
        ]
        self.assertTrue(
            any(rel.endswith(".master.key") for rel in files),
            "夹具里没有凭据文件，上面的断言等于没测",
        )


if __name__ == "__main__":
    unittest.main()
