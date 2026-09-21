#!/usr/bin/env python3
"""生成 pyjson 的 golden 夹具。

为什么需要它：Rust 侧的 `pyjson` 要复刻 CPython 的 `json.dumps`，
而"复刻得像不像"必须由**真 Python** 判定，不能由我手抄期望值 ——
手抄的期望值一旦抄错，两边一起错，测试反而是绿的。

所以这里的做法是：
  1. 用例的**输入**由人工撰写（JSON 文本）；
  2. **期望输出**由 `json.dumps(json.loads(输入), ...)` 现场算出。

输入是写死的文本、输出是算出来的，所以不构成循环论证 ——
被测的正是"输入 → 输出"这一步变换。

`tests/test_pyjson_golden.py` 会重新算一遍并与夹具比对，
夹具一旦过时（比如换了 Python 版本改了浮点输出）测试就会红。

    python3 tools/gen_pyjson_golden.py            # 写入夹具
    python3 tools/gen_pyjson_golden.py --check    # 只校验夹具是否最新
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "pyjson_golden.json")

# --------------------------------------------------------------------------
# 紧凑模式用例（对标 json.dumps(v, ensure_ascii=False, sort_keys=True)）
# --------------------------------------------------------------------------

COMPACT: list[tuple[str, str]] = [
    # --- 整数 ---
    ("int_zero", "0"),
    ("int_neg", "-1"),
    ("int_i64_max", "9223372036854775807"),
    ("int_i64_min", "-9223372036854775808"),
    ("int_2pow53", "9007199254740992"),
    ("int_year", "2026"),
    # --- 字面量 ---
    ("bool_true", "true"),
    ("bool_false", "false"),
    ("null_value", "null"),
    # --- 浮点：CPython 的 repr 规则是风险最集中的地方 ---
    ("float_zero", "0.0"),
    ("float_neg_zero", "-0.0"),
    ("float_one", "1.0"),
    ("float_half", "1.5"),
    ("float_neg_half", "-1.5"),
    ("float_tenth", "0.1"),
    ("float_third", "0.3333333333333333"),
    ("float_hundred", "100.0"),
    ("float_123_456", "123.456"),
    # exp < 16 要定点输出，>= 16 切科学计数 —— 边界两侧都要有
    ("float_1e15", "1e15"),
    ("float_1e16", "1e16"),
    ("float_boundary_hi", "9999999999999998.0"),
    ("float_1e17", "1e17"),
    # exp >= -4 要定点输出，< -4 切科学计数
    ("float_1e-4", "1e-4"),
    ("float_1e-5", "1e-5"),
    ("float_boundary_lo", "0.0001"),
    ("float_1e-7", "1e-7"),
    # 极端值
    ("float_1e100", "1e100"),
    ("float_1e_neg100", "1e-100"),
    ("float_min_subnormal", "5e-324"),
    ("float_max", "1.7976931348623157e308"),
    ("float_pi", "3.141592653589793"),
    # --- 字符串：ensure_ascii=False 的转义边界 ---
    ("str_empty", '""'),
    ("str_ascii", '"hello"'),
    ("str_quote", '"a\\"b"'),
    ("str_backslash", '"a\\\\b"'),
    ("str_named_escapes", '"a\\nb\\tc\\rd\\be\\ff"'),
    ("str_c0_1f", '"\\u001f"'),
    ("str_c0_00", '"\\u0000"'),
    ("str_del_7f", '"\\u007f"'),
    ("str_forward_slash", '"a/b"'),
    ("str_cjk", '"中文测试"'),
    ("str_emoji", '"🎉🚀"'),
    ("str_fullwidth", '"（）【】、"'),
    ("str_literal_backslash_u", '"\\\\u4e2d"'),
    ("str_windows_path", '"C:\\\\Users\\\\demo"'),
    # --- 数组 ---
    ("array_empty", "[]"),
    ("array_nums", "[1, 2, 3]"),
    ("array_mixed", '[1, "a", true, null, 1.5]'),
    ("array_nested", "[[1, [2, [3]]]]"),
    ("array_of_arrays_empty", "[[], [[]]]"),
    # --- 对象（sort_keys 的排序口径）---
    ("obj_empty", "{}"),
    # 'Z'(0x5a) < '_'(0x5f) < 'a'(0x61)，字节序与码点序一致
    ("obj_keysort_ascii", '{"b": 1, "a": 2, "A": 3, "_": 4, "Z": 5}'),
    ("obj_cjk_keys", '{"中文": 1, "英文": 2, "abc": 3}'),
    ("obj_nested", '{"outer": {"inner": {"leaf": 1}}}'),
    ("obj_nonascii_values", '{"path": "/Users/示例/数据", "note": "备注：含标点。"}'),
    ("obj_mixed_shapes", '{"arr": [1, {"k": []}], "obj": {"x": {}}, "s": ""}'),
    # --- 贴近真实的计划体 ---
    (
        "obj_plan_body",
        json.dumps(
            {
                "version": "0.3.0a1",
                "home_a": "/Users/demo/.workbuddy",
                "home_b": "/Users/demo/.workbuddy-ai",
                "uid_a": "0f1e2d3c4b5a6978",
                "uid_b": "8f7e6d5c4b3a2910",
                "options": {"sessions": True, "memory": False, "skills": True},
                "source_fingerprints": {
                    "sessions_all": "9f2b" * 16,
                    "memory": "1a2b" * 16,
                },
                "entries_fingerprint": "cafe" * 16,
            },
            ensure_ascii=False,
        ),
    ),
    (
        "obj_plan_with_entries",
        json.dumps(
            {
                "entries": [
                    {
                        "kind": "file",
                        "mode": "copy_if_missing",
                        "src": "/Users/示例/a.db",
                        "dst": "/Users/示例/b.db",
                    },
                    {
                        "kind": "tree",
                        "mode": "merge_tree",
                        "src": "/Users/示例/skills",
                        "dst": "/Users/示例/skills",
                        "note": "并集，不覆盖已有",
                    },
                ],
                "summary": {"files": 2, "bytes": 540_512_345},
            },
            ensure_ascii=False,
        ),
    ),
]

# --------------------------------------------------------------------------
# 缩进模式用例（对标 json.dumps(..., indent=2)）—— 空容器必须压成 {} / []
# --------------------------------------------------------------------------

INDENT2: list[tuple[str, str]] = [
    ("indent_empty_containers", '{"a": {}, "b": []}'),
    ("indent_array", '[1, [2, 3], {"k": "v"}]'),
    ("indent_deep", '{"a": {"b": {"c": [1, 2]}}}'),
    ("indent_scalars", '{"n": null, "b": true, "i": 7, "f": 1.5}'),
    ("indent_cjk", '{"键": "值", "arr": ["一", "二"]}'),
    ("indent_empty_top", "{}"),
    ("indent_empty_array_top", "[]"),
    (
        "indent_plan_body",
        json.dumps(
            {
                "version": "0.3.0a1",
                "options": {"sessions": True, "memory": False},
                "entries": [
                    {"kind": "file", "src": "/a", "dst": "/b", "note": ""},
                    {"kind": "tree", "src": "/c", "dst": "/d"},
                ],
            },
            ensure_ascii=False,
        ),
    ),
    (
        "indent_memory_block",
        json.dumps(
            {"uid": "0f1e", "memoryBlock": "行一\n行二\t制表", "updatedAt": "2026-09-21T11:00:00+08:00"},
            ensure_ascii=False,
        ),
    ),
]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_compact(cases: list[tuple[str, str]]) -> list[dict[str, str]]:
    out = []
    for label, raw in cases:
        value = json.loads(raw)
        expected = json.dumps(value, ensure_ascii=False, sort_keys=True)
        out.append(
            {
                "label": label,
                "json": raw,
                "expected": expected,
                "sha256": sha256_text(expected),
            }
        )
    return out


def build_indent(cases: list[tuple[str, str]], indent: int = 2) -> list[dict[str, str]]:
    out = []
    for label, raw in cases:
        value = json.loads(raw)
        expected = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=indent)
        out.append(
            {
                "label": label,
                "json": raw,
                "indent": indent,
                "expected": expected,
                "sha256": sha256_text(expected),
            }
        )
    return out


def build_fixture() -> dict:
    return {
        "generated_by": "tools/gen_pyjson_golden.py",
        "note": "期望输出由 CPython 现场算出，不是手抄。改动请重跑生成器。",
        "python": sys.version.split()[0],
        "compact": build_compact(COMPACT),
        "indent2": build_indent(INDENT2),
    }


def render(fixture: dict) -> str:
    # ensure_ascii=False 让 CJK / emoji 在夹具里保持可读，
    # 否则满屏 \uXXXX，出了问题根本看不出是哪个用例。
    return json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成或校验 pyjson golden 夹具")
    parser.add_argument("--check", action="store_true", help="只校验夹具是否最新，不写入")
    args = parser.parse_args()

    text = render(build_fixture())

    if args.check:
        existing = ""
        if os.path.exists(FIXTURE):
            with open(FIXTURE, encoding="utf-8") as fh:
                existing = fh.read()
        if existing != text:
            print("夹具已过时，请重跑：python3 tools/gen_pyjson_golden.py", file=sys.stderr)
            return 1
        print(f"夹具是最新的：{os.path.relpath(FIXTURE, ROOT)}")
        return 0

    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, "w", encoding="utf-8") as fh:
        fh.write(text)
    fixture = build_fixture()
    print(
        f"已写入 {os.path.relpath(FIXTURE, ROOT)}："
        f"compact {len(fixture['compact'])} 例，indent2 {len(fixture['indent2'])} 例"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
