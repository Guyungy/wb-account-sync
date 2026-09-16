# wb-account-sync

<p align="center">
  <strong>WorkBuddy 账号数据安全迁移与跨 App 打通工具</strong><br>
  离线 · 显式确认 · 可回滚 · 零运行时依赖
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.2.0a1-blue" alt="version">
  <img src="https://img.shields.io/badge/python-3.10+-3776ab?logo=python&logoColor=white" alt="python">
  <img src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue" alt="license">
</p>

<p align="center">
  <img src="docs/images/ui.png" width="860" alt="跨 App 数据目录打通界面">
</p>

> **非官方项目**，未获 WorkBuddy 或其厂商背书。仅用于你本人有权管理的账户。

---

## 目录

- [30 秒上手](#30-秒上手)
- [它能做什么](#它能做什么)
- [安全模型](#安全模型)
- [两种使用方式](#两种使用方式)
  - [浏览器界面（推荐）](#浏览器界面推荐)
  - [命令行](#命令行)
- [安装](#安装)
- [测试](#测试)
- [文档](#文档)
- [免责声明](#免责声明)

---

## 30 秒上手

如果你本机同时装了 **WorkBuddy** 和 **WorkBuddy AI**，它们的数据互相隔离。
这个工具可以把两边的会话历史、记忆、技能、配置做**双向合并**，且不会覆盖任何已有数据。

```bash
git clone https://github.com/Guyungy/wb-account-sync.git
cd wb-account-sync
python3 tools/wb_ui.py
```

启动后会自动打开浏览器，界面会实时告诉你两个客户端是否已完全退出。
**只有退出后，「执行」按钮才会解锁。**

macOS 用户也可以直接双击 [`tools/ui.command`](tools/ui.command)，把它拖到 Dock 上当 App 用。

---

## 它能做什么

| 场景 | 支持 | 说明 |
|---|---|---|
| 同一客户端内改账号归属 | ✅ | 把旧账号下的普通会话过户给当前登录账号 |
| 两个独立客户端互相打通 | ✅ | WorkBuddy ↔ WorkBuddy AI，双向合并，两边都保留 |
| 只新增、不覆盖 | ✅ | 冲突时保留目标侧，源侧不删除 |
| 迁移计划 + 人工确认 | ✅ | 先生成计划，再粘贴完整 `plan_id` 才执行 |
| 撤销 / 回滚 | ✅ | 基于 undo journal 恢复 `user_id` |
| 跨设备迁移 / 云端同步 | ❌ | 不在本工具范围内 |

---

## 安全模型

这是 **Alpha 预览版**，设计优先级是“保守”：

- **客户端退出保护**：写操作前必须完全退出 WorkBuddy 与 WorkBuddy AI。
- **漂移拒绝**：执行前会再次核对数据库 fingerprint，源数据变了直接拒绝。
- **完整确认**：不支持 `--yes`，必须人工粘贴 `plan_id`。
- **不搬敏感项**：连接器凭据用各 home 的 `.master.key` 加密，跨 home 解不开，因此默认不搬；自动化任务默认不搬（避免两边各跑一遍）。
- **本地运行**：服务只监听 `127.0.0.1`，所有 API 都校验一次性 token。

> 本工具的 undo journal 是归属恢复，不是完整备份。迁移前请先自行备份。

---

## 两种使用方式

### 浏览器界面（推荐）

最直观，适合大多数人：

```bash
cd wb-account-sync
python3 tools/wb_ui.py              # 自动挑端口、自动开浏览器
./tools/ui.command                  # macOS：双击启动，可拖到 Dock
```

页面流程：**读取盘点 → 勾选迁移范围 → 生成计划 → 审阅 plan_id → 粘贴确认 → 执行迁移 → 核验 / 回滚**。

链接形如 `http://127.0.0.1:8788/?t=<token>`，**`?t=` 是访问凭据**，缺失或错误都会 403；每次启动 token 会变，旧链接失效。

### 命令行

适合脚本、CI，或不想开浏览器：

```bash
# 跨 App 打通：备份 → 计划 → 确认 → 执行 → 核验
./tools/bridge-run.sh

# 同一客户端内改归属
wb-account-sync doctor --home ~/.workbuddy
wb-account-sync plan   --home ~/.workbuddy --source <旧账号UUID> --target <当前账号UUID> --output plan.json
wb-account-sync apply  --plan plan.json --state-dir ./state --confirm <plan_id>
```

详细用法见 [`docs/HOME_BRIDGE.md`](docs/HOME_BRIDGE.md) 与 [`docs/SAFETY.md`](docs/SAFETY.md)。

---

## 安装

### 方式一：下载 wheel（推荐体验）

从 [Releases](https://github.com/Guyungy/wb-account-sync/releases) 下载标为 **Pre-release** 的 wheel 与 `SHA256SUMS.txt`，校验后安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --no-index --no-deps ./wb_account_sync-0.2.0a1-py3-none-any.whl
wb-account-sync --version
```

### 方式二：从源码安装

```bash
git clone https://github.com/Guyungy/wb-account-sync.git
cd wb-account-sync
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
```

**尚未发布 PyPI**，不要假设 `pip install wb-account-sync` 下载的是本项目。

---

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 tools/synth_check.py
```

- 单元测试：125 项，覆盖只读零副作用、计划漂移、权限拒绝、journal 中断续办、幂等、撤销等。
- 合成夹具测试：25 项端到端断言，用真实 schema 造两个假 home，验证 survey / plan / apply / verify / restore 全链路。

---

## 文档

- [`docs/HOME_BRIDGE.md`](docs/HOME_BRIDGE.md) — 跨 App 数据目录打通详细说明
- [`docs/SAFETY.md`](docs/SAFETY.md) — 安全边界及故障处理
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — 长期路线图与 1.0 验收
- [`docs/MIGRATION.md`](docs/MIGRATION.md) — 从旧版迁移
- [`CHANGELOG.md`](CHANGELOG.md) — 更新记录

---

## 免责声明

- 本项目非官方，与 WorkBuddy 及其厂商无关联。
- 仅用于你本人拥有完整管理权限的账户和数据。
- 客户端私有存储协议、界面展示和云同步行为尚未经过正式兼容认证。
- 0.2.0a1 是安全预览版，不是生产稳定版。不要在不可替代的数据上首次运行。

### 许可证

**GNU General Public License v3.0 or later**（GPL-3.0-or-later）

Copyright (C) 2026 Guyungy

本程序是自由软件：你可以依据自由软件基金会发布的 GNU 通用公共许可证条款
（许可证第 3 版，或你选择的任何更新版本）重新分发和/或修改它。

本程序分发的目的是希望它有用，但不提供任何担保，甚至不包含适销性或
特定用途适用性的默示担保。详见 [GNU 通用公共许可证](LICENSE)。

> 采用 GPL 意味着：**衍生作品必须以相同许可证开源**。如果你需要闭源商用，
> 请联系作者另行授权。
