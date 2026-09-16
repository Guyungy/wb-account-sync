#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge-run — 跨平台的「备份 → 计划 → 人工确认 → 执行 → 核验」一条命令流程。

这是 ``bridge-run.sh`` 的跨平台实现。原脚本依赖 ``ps -Ao`` 与 ``.app`` 路径匹配，
在 Windows 上无法运行；本文件把同样的流程改用 ``wb_platform`` 做平台分流，
所以三个入口共用同一套逻辑：

* macOS / Linux：``./tools/bridge-run.sh``
* 任意平台：``python3 tools/bridge_run.py``
* 图形界面：``python3 tools/wb_ui.py``

确认门与 CLI 保持一致：必须原样粘贴完整 ``plan_id``，不支持短前缀或 ``--yes``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import wb_home_bridge as bridge  # noqa: E402
import wb_platform  # noqa: E402

VERSION = "0.1.0"
DEFAULT_STATE_DIR = "~/.wb-home-bridge"


def banner(args: argparse.Namespace, home_a: bridge.Home, home_b: bridge.Home) -> None:
    print("=" * 58)
    print(" WorkBuddy <-> WorkBuddy AI  跨 App 打通")
    print(f" 平台     : {wb_platform.platform_label()}")
    print(f" 左侧目录 : {home_a.path}")
    print(f" 右侧目录 : {home_b.path}")
    print(f" 状态目录 : {args.state_dir}")
    print("=" * 58)
    print()


def step_check_clients() -> bool:
    print("-- 0/4 客户端状态检查 --")
    try:
        statuses = wb_platform.client_statuses()
    except wb_platform.PlatformError as exc:
        print(f"[X] 无法检查客户端进程：{exc}")
        return False
    running = [item for item in statuses if item["running"]]
    if running:
        print("[X] 检测到客户端仍在运行：")
        for item in running:
            for proc in item["processes"][:4]:
                print(f"      pid {proc['pid']}  {proc['cmd'][:100]}")
        print()
        print(wb_platform.stop_instructions())
        return False
    for item in statuses:
        mark = "目录正常" if item["db_exists"] else "缺少 workbuddy.db"
        print(f"    {item['display']}: 已退出 | {item['home']} | {mark}")
    print("[OK] 两个客户端都已退出")
    print()
    return True


def step_backup(args: argparse.Namespace, home_a: bridge.Home, home_b: bridge.Home) -> bool:
    if args.no_backup:
        print("-- 1/4 备份：已按 --no-backup 跳过 --")
        print()
        return True
    print("-- 1/4 备份（默认排除 app/logs/traces）--")
    ns = argparse.Namespace(
        dest=args.backup_dest, label="before-bridge", include_heavy=False,
        allow_client_running=False,
    )
    try:
        bridge.do_backup(ns, [home_a, home_b])
    except bridge.BridgeError as exc:
        print(f"[X] 备份失败：{exc}")
        print("    可用 --no-backup 跳过，但不推荐在无备份的情况下执行。")
        return False
    print()
    return True


def step_plan(
    args: argparse.Namespace, home_a: bridge.Home, home_b: bridge.Home
) -> tuple[str, str] | None:
    print("-- 2/4 生成计划（只读，不写数据）--")
    ns = argparse.Namespace(
        home_a=home_a.path, home_b=home_b.path, json=False,
        output=None,
        no_changes=args.no_changes, no_skills=args.no_skills,
        no_memory=args.no_memory, no_claw=args.no_claw,
        include_plugins=args.include_plugins,
        include_automations=args.include_automations,
        include_storage=args.include_storage,
        include_connectors=args.include_connectors,
        overwrite_assets=args.overwrite_assets,
        allow_client_running=False,
    )
    try:
        plan = bridge.build_plan(ns, home_a, home_b)
    except bridge.BridgeError as exc:
        print(f"[X] 生成计划失败：{exc}")
        return None

    summary = plan.summary
    for side in ("a2b", "b2a"):
        item = summary[side]
        print(f"    [{side}] {item['from']} -> {item['to']}: "
              f"复制 {item['sessions_to_copy']} 条（已存在跳过 {item['sessions_skipped']}），"
              f"缺资产 {item['missing_assets']}，cwd 失效 {item['cwd_invalid']}")
    totals = summary["totals"]
    print(f"    合计：{totals['sessions_to_copy']} 条会话，约 {totals['approx_human']}")
    print("    选项：" + "  ".join(
        f"{name}={'是' if value else '否'}" for name, value in plan.options.items()))

    plan_dir = os.path.join(args.state_dir, "plans")
    os.makedirs(plan_dir, mode=0o700, exist_ok=True)
    path = os.path.join(plan_dir, f"{plan.plan_id[:16]}.json")
    if os.path.exists(path):
        print(f"[X] 计划文件已存在，不覆盖：{path}")
        return None
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan.as_dict(), fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    print(f"    计划文件：{path}")
    print()
    return plan.plan_id, path


def step_confirm(plan_id: str) -> bool:
    print("-- 3/4 人工确认 --")
    print("请核对上面的方向、会话数与体积。确认无误后把下面这串完整 plan_id 粘贴回来")
    print("（直接回车 = 放弃）：")
    print()
    print(f"  {plan_id}")
    print()
    try:
        answer = input("plan_id> ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    print()
    if answer != plan_id:
        print("[X] 确认串不匹配，已放弃执行，未写入任何数据。")
        return False
    return True


def step_apply(
    args: argparse.Namespace, home_a: bridge.Home, home_b: bridge.Home,
    plan_id: str, plan_path: str,
) -> int:
    print("-- 4/4 执行 --")
    ns = argparse.Namespace(
        home_a=home_a.path, home_b=home_b.path,
        plan=plan_path, state_dir=args.state_dir, confirm=plan_id,
        allow_client_running=False,
    )
    try:
        bridge.require_clients_stopped(False)
        return bridge.do_apply(ns, home_a, home_b)
    except bridge.BridgeError as exc:
        print(f"[X] 执行失败：{exc}")
        return 2


def step_verify(home_a: bridge.Home, home_b: bridge.Home, plan_path: str) -> int:
    print()
    print("-- 核验 --")
    ns = argparse.Namespace(
        home_a=home_a.path, home_b=home_b.path, plan=plan_path, json=False,
    )
    try:
        return bridge.do_verify(ns, home_a, home_b)
    except bridge.BridgeError as exc:
        print(f"[X] 核验失败：{exc}")
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge-run",
        description="跨 App 数据目录打通的一条命令流程（只新增，不覆盖已有数据）。",
    )
    parser.add_argument("--version", action="version", version=f"bridge-run {VERSION}")
    parser.add_argument("--home-a", default=None, help="WorkBuddy 的数据目录（默认自动探测）")
    parser.add_argument("--home-b", default=None, help="WorkBuddy AI 的数据目录（默认自动探测）")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                        help=f"状态目录，默认 {DEFAULT_STATE_DIR}")
    parser.add_argument("--backup-dest", default="~/wb-home-bridge-backups",
                        help="备份根目录，默认 ~/wb-home-bridge-backups")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    parser.add_argument("--no-changes", action="store_true",
                        help="不搬 changes-detail / changes-index / file-history")
    parser.add_argument("--no-skills", action="store_true", help="不合并用户技能目录")
    parser.add_argument("--no-memory", action="store_true", help="不合并长期记忆")
    parser.add_argument("--no-claw", action="store_true", help="不合并 settings.json 渠道绑定")
    parser.add_argument("--include-plugins", action="store_true", help="合并 plugins/cache")
    parser.add_argument("--include-automations", action="store_true",
                        help="复制自动化定义（以暂停状态落地）")
    parser.add_argument("--include-storage", action="store_true", help="复制账号个人存储目录")
    parser.add_argument("--include-connectors", action="store_true",
                        help="只并连接器开关状态，不搬凭据")
    parser.add_argument("--overwrite-assets", action="store_true",
                        help="已存在的资产文件也覆盖（默认跳过）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    args.state_dir = os.path.abspath(os.path.expanduser(args.state_dir))
    args.backup_dest = os.path.abspath(os.path.expanduser(args.backup_dest))

    spec_a = wb_platform.CLIENTS_BY_KEY["wb"]
    spec_b = wb_platform.CLIENTS_BY_KEY["wb_ai"]

    try:
        home_a = bridge.make_home(spec_a, args.home_a)
        home_b = bridge.make_home(spec_b, args.home_b)
    except bridge.BridgeError as exc:
        print(f"[X] {exc}")
        return 2

    banner(args, home_a, home_b)

    try:
        for home in (home_a, home_b):
            home.require_valid()
    except bridge.BridgeError as exc:
        print(f"[X] {exc}")
        return 2

    if not step_check_clients():
        return 2

    os.makedirs(args.state_dir, mode=0o700, exist_ok=True)

    if not step_backup(args, home_a, home_b):
        return 2

    planned: Any = step_plan(args, home_a, home_b)
    if planned is None:
        return 2
    plan_id, plan_path = planned

    if not step_confirm(plan_id):
        return 2

    code = step_apply(args, home_a, home_b, plan_id, plan_path)
    if code != 0:
        print()
        print(f"执行未成功（退出码 {code}）。上方的运行记录保留在状态目录里。")
        return code

    verify_code = step_verify(home_a, home_b, plan_path)

    print()
    print("=" * 58)
    if verify_code == 0:
        print(" 完成。现在启动两个客户端，两边都应该能看到全部历史。")
    else:
        print(f" 核验未完全通过（退出码 {verify_code}）。请把上面的输出发回排查。")
    print(f" 回滚：python3 tools/wb_home_bridge.py restore "
          f"--run-dir {os.path.join(args.state_dir, 'runs', plan_id)} --confirm {plan_id}")
    print("=" * 58)
    return 0 if verify_code == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
