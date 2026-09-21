#!/usr/bin/env python3
"""生成 plan_id 的跨实现 golden 夹具。

## 为什么夹具用**固定路径**

`plan_id` 的正文里含 `home_a` / `home_b` 的**绝对路径**（见
`Plan.body()`），所以同一个夹具放在不同目录下会得到不同的 plan_id。
要让 Python 与 Rust 算出同一个值，两边必须跑在**同一份路径**上。

因此夹具固定在 `/tmp/wb-plan-parity/`（不是 `tempfile.mkdtemp()`——
那会给出每次不同的路径，两边永远对不上）。会话的 `cwd` 也固定，
因为它会进 `sessions_all` 指纹。

## 两个用例

- `default`：照界面的默认勾选（会话正文 / 技能 / claw / 记忆带，其余不带）
- `all_options`：把 include_automations / storage / connectors / plugins / changes
  全打开，覆盖更多条目类型与指纹分支

## 用法

    python3 tools/gen_plan_golden.py            # 写入夹具
    python3 tools/gen_plan_golden.py --check    # 只校验夹具是否最新
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import wb_home_bridge as bridge  # noqa: E402

GOLDEN = os.path.join(ROOT, "tests", "fixtures", "plan_golden.json")

# 固定路径，不是临时目录 —— 见文件头。
FIXTURE_ROOT = "/tmp/wb-plan-parity"
HOME_A = os.path.join(FIXTURE_ROOT, "home-a")
HOME_B = os.path.join(FIXTURE_ROOT, "home-b")
CWD_A = os.path.join(FIXTURE_ROOT, "cwd-a")
CWD_MISSING = os.path.join(FIXTURE_ROOT, "cwd-gone")

SCHEMA = [
    """CREATE TABLE sessions (
        id TEXT, cwd TEXT, user_id TEXT, deleted_at TEXT, title TEXT,
        created_at TEXT, updated_at TEXT, status TEXT, mode TEXT, model TEXT,
        permission_mode TEXT, is_playground TEXT, use_sandbox_cli TEXT,
        buddy_snapshot_id TEXT, context_window TEXT)""",
    "CREATE TABLE session_usage (session_id TEXT, tokens INTEGER, cost REAL, note TEXT)",
    "CREATE TABLE workspaces (path TEXT, name TEXT)",
    "CREATE TABLE buddy_snapshots (snapshot_id TEXT, payload TEXT)",
    """CREATE TABLE automations (
        id TEXT, deleted_at TEXT, title TEXT, status TEXT,
        owner_user_id TEXT, owner_status TEXT, next_run_at TEXT)""",
    "CREATE TABLE automation_runs (thread_id TEXT, automation_id TEXT, ok INTEGER)",
    "CREATE TABLE automation_runtime_state (automation_id TEXT, running INTEGER, last TEXT)",
]

SESSION_COLS = ("id", "cwd", "user_id", "deleted_at", "title", "created_at",
                "updated_at", "status", "mode", "model", "permission_mode",
                "is_playground", "use_sandbox_cli", "buddy_snapshot_id", "context_window")


def session(sid, cwd, uid, title, **over):
    row = {c: "" for c in SESSION_COLS}
    row.update({
        "id": sid, "cwd": cwd, "user_id": uid, "deleted_at": None,
        "title": title, "created_at": "1760000000000", "updated_at": "1760000001000",
        "status": "idle", "mode": "chat", "model": "m", "permission_mode": "default",
        "is_playground": "0", "use_sandbox_cli": "0", "buddy_snapshot_id": "",
        "context_window": "128",
    })
    row.update(over)
    return row


UID_A = "0f1e2d3c-aaaa-4bbb-8ccc-000000000001"
UID_B = "8f7e6d5c-bbbb-4ccc-8ddd-000000000002"


def session_assets(cid: str) -> list[list]:
    """一个会话在一个 home 里会有的文件。

    刻意把**每一类条目都造出来**：`projects/` 下的三种后缀、按会话归档的四个
    内容目录、以及 `artifact-index` 单文件。夹具太薄的话，条目构造那几十行
    有一半跑不到，`plan_id` 一致也不能说明什么。
    """
    return [
        [f"projects/slug-{cid}/{cid}.jsonl", 40],
        [f"projects/slug-{cid}/{cid}.meta.json", 25],
        [f"projects/slug-{cid}/{cid}.file-rollback.ndjson", 30],
        [f"projects/slug-{cid}/{cid}/tool-result.txt", 70],
        [f"artifact-index/{cid}.json", 21],
        [f"tasks/{cid}/step.json", 12],
        [f"changes-detail/{cid}/diff.patch", 33],
        [f"changes-index/{cid}/index.bin", 18],
        [f"file-history/{cid}/v1.bin", 27],
    ]


def global_assets(uid: str) -> list[list]:
    """与具体会话无关的并集目录与连接器文件。"""
    return [
        ["blobs/ab/cdef", 88],
        ["plugins/cache/pkg/mod.bin", 44],
        ["skills/demo/SKILL.md", 64],
        ["connectors/skills/conn/def.json", 19],
        [f"connectors/{uid}/connector-states.json", 27],
        [f"connectors/{uid}/mcp.json", 31],
        [f"connectors/{uid}/.master.key", 16],  # 绝不能出现在计划里
        [f"storage/user-{uid}/pref.json", 15],
        [f"storage/user-{uid}-personal/notes.txt", 23],
    ]


# --------------------------------------------------------------------------
# 夹具内容
# --------------------------------------------------------------------------

HOMES = {
    "home-a": {
        "uid": UID_A,
        "nickname": "主账号",
        "tables": {
            "sessions": [
                # s-shared 两边都有 → a2b 方向必须跳过它
                session("s-shared", CWD_A, UID_A, "共用会话"),
                # cwd 故意不存在，用来覆盖 cwd_invalid 计数
                session("s-a2", CWD_MISSING, UID_A, "甲侧独有"),
                # 已删除的会话必须被排除
                session("s-gone", CWD_A, UID_A, "已删除", deleted_at="1760000002000"),
            ],
            # cost 是 REAL —— 会走浮点 repr 那条路
            "session_usage": [
                {"session_id": "s-shared", "tokens": 100, "cost": 0.5, "note": "备注一"},
                {"session_id": "s-a2", "tokens": 250, "cost": 1.25, "note": "备注二"},
                # 指向已删除的会话，必须被过滤掉
                {"session_id": "s-gone", "tokens": 999, "cost": 9.5, "note": "不该出现"},
            ],
            "workspaces": [{"path": os.path.join(FIXTURE_ROOT, "ws-a"), "name": "ws-a"}],
            "buddy_snapshots": [],
            "automations": [
                {"id": "auto-1", "deleted_at": None, "title": "每日汇总", "status": "ACTIVE",
                 "owner_user_id": UID_A, "owner_status": "confirmed",
                 "next_run_at": "1760009999000"},
                {"id": "auto-gone", "deleted_at": "1760000005000", "title": "已删自动化",
                 "status": "ACTIVE", "owner_user_id": UID_A, "owner_status": "confirmed",
                 "next_run_at": "1760009999000"},
            ],
            "automation_runs": [
                {"thread_id": "th-1", "automation_id": "auto-1", "ok": 1},
                {"thread_id": "th-orphan", "automation_id": "auto-gone", "ok": 0},
            ],
            "automation_runtime_state": [
                {"automation_id": "auto-1", "running": 1, "last": "1760000003000"},
                {"automation_id": "auto-gone", "running": 1, "last": "1760000004000"},
            ],
        },
        "files": session_assets("s-a2") + global_assets(UID_A),
    },
    "home-b": {
        "uid": UID_B,
        "nickname": "副账号",
        "tables": {
            # s-shared 与 home-a 重名 → b2a 方向要跳过；s-b1 才是要搬的
            "sessions": [
                session("s-shared", CWD_A, UID_B, "共用会话（乙侧副本）"),
                session("s-b1", CWD_A, UID_B, "乙侧独有"),
            ],
            "session_usage": [
                {"session_id": "s-shared", "tokens": 5, "cost": 0.125, "note": "乙侧"},
                {"session_id": "s-b1", "tokens": 7, "cost": 0.0625, "note": "乙侧二"},
            ],
            "workspaces": [{"path": os.path.join(FIXTURE_ROOT, "ws-b"), "name": "ws-b"}],
            "buddy_snapshots": [],
            "automations": [],
            "automation_runs": [],
            "automation_runtime_state": [],
        },
        "files": session_assets("s-b1") + global_assets(UID_B),
    },
}

# 两个用例：默认勾选，与"尽量全开"
CASES = [
    (
        "default",
        {
            "include_changes": True,
            "include_skills": True,
            "include_plugins": False,
            "include_automations": False,
            "include_storage": False,
            "include_connectors": False,
            "include_claw": True,
            "include_memory": True,
            "overwrite_assets": False,
        },
    ),
    (
        "all_options",
        {
            "include_changes": True,
            "include_skills": True,
            "include_plugins": True,
            "include_automations": True,
            "include_storage": True,
            "include_connectors": True,
            "include_claw": True,
            "include_memory": True,
            "overwrite_assets": False,
        },
    ),
]


# --------------------------------------------------------------------------
# 造夹具
# --------------------------------------------------------------------------

def build_fixture() -> None:
    """把夹具写到固定的 FIXTURE_ROOT 下。幂等：先清空再重建。"""
    shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)
    os.makedirs(CWD_A, exist_ok=True)
    # CWD_MISSING 刻意不建

    for name, spec in HOMES.items():
        home = os.path.join(FIXTURE_ROOT, name)
        os.makedirs(home, exist_ok=True)

        con = sqlite3.connect(os.path.join(home, bridge.DB_NAME))
        for ddl in SCHEMA:
            con.execute(ddl)
        for table, rows in spec["tables"].items():
            for row in rows:
                cols = list(row)
                con.execute(
                    f'INSERT INTO "{table}" ({", ".join(cols)}) '
                    f'VALUES ({", ".join("?" * len(cols))})',
                    [row[c] for c in cols],
                )
        con.commit()
        con.close()

        for rel, size in spec["files"]:
            full = os.path.join(home, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(b"x" * size)

        snap = os.path.join(home, "storage", "skeleton", "account-snapshot.json")
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        with open(snap, "w", encoding="utf-8") as fh:
            json.dump({"primary": {"uid": spec["uid"], "nickname": spec["nickname"]}},
                      fh, ensure_ascii=False)


def homes() -> tuple[bridge.Home, bridge.Home]:
    return (
        bridge.Home(label="WorkBuddy", slug="wb", path=HOME_A, app="WorkBuddy"),
        bridge.Home(label="WorkBuddy AI", slug="wb_ai", path=HOME_B, app="WorkBuddy AI"),
    )


def plan_args(options: dict[str, bool]) -> argparse.Namespace:
    """把选项字典翻译成 build_plan 需要的 argparse.Namespace。

    注意 `no_*` 与 `include_*` 是**取反**关系 —— Python 侧只接受前者。
    """
    return argparse.Namespace(
        no_changes=not options["include_changes"],
        no_skills=not options["include_skills"],
        include_plugins=options["include_plugins"],
        include_automations=options["include_automations"],
        include_storage=options["include_storage"],
        include_connectors=options["include_connectors"],
        no_claw=not options["include_claw"],
        no_memory=not options["include_memory"],
        overwrite_assets=options["overwrite_assets"],
    )


def build_golden() -> dict:
    build_fixture()
    a, b = homes()

    cases = []
    for label, options in CASES:
        plan = bridge.build_plan(plan_args(options), a, b)
        cases.append({
            "label": label,
            "options": options,
            "expected_plan_id": plan.plan_id,
            # 存**被哈希的那串规范编码**本身，而不只是它的哈希。
            # 比对哈希只能说明"两边都错了同一个地方"和"两边都对"无法区分；
            # 存下原文，出错时 diff 会直接指出是哪个字段。
            "expected_body_canonical": json.dumps(
                plan.body(), ensure_ascii=False, sort_keys=True
            ),
            "expected_entries_canonical": json.dumps(
                [e.as_dict() for e in plan.entries], ensure_ascii=False, sort_keys=True
            ),
            "expected_entry_count": len(plan.entries),
            "expected_entries": [e.as_dict() for e in plan.entries],
            "expected_skipped": plan.skipped,
        })

    return {
        "generated_by": "tools/gen_plan_golden.py",
        "note": "期望值由 CPython 现场算出。改动夹具或选项后必须重跑生成器。",
        "fixture": {
            "root": FIXTURE_ROOT,
            "home_a": HOME_A,
            "home_b": HOME_B,
            "schema": SCHEMA,
            "homes": HOMES,
        },
        "cases": cases,
    }


def render(golden: dict) -> str:
    return json.dumps(golden, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成或校验 plan_id golden 夹具")
    parser.add_argument("--check", action="store_true", help="只校验是否最新，不写入")
    args = parser.parse_args()

    text = render(build_golden())

    if args.check:
        existing = ""
        if os.path.exists(GOLDEN):
            with open(GOLDEN, encoding="utf-8") as fh:
                existing = fh.read()
        if existing != text:
            print("夹具已过时，请重跑：python3 tools/gen_plan_golden.py", file=sys.stderr)
            return 1
        print(f"夹具是最新的：{os.path.relpath(GOLDEN, ROOT)}")
        return 0

    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, "w", encoding="utf-8") as fh:
        fh.write(text)
    cases = build_golden()["cases"]
    print(f"已写入 {os.path.relpath(GOLDEN, ROOT)}：{len(cases)} 个用例")
    for case in cases:
        print(f"  {case['label']:12} plan_id={case['expected_plan_id'][:16]}… "
              f"entries={case['expected_entry_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
