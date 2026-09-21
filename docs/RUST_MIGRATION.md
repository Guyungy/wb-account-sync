# Rust + TypeScript 迁移方案

- 定稿：2026-09-21
- 决策依据：用户要求「改一下用 Rust TypeScript 为主」，界面形态选定 **Tauri 2 桌面应用**（2026-09-21 由 `wry + tao` 改定，见下方「形态变更」），Go 底座移入 `legacy-go/` 保留参照
- 前置条件：**磁盘可用空间 ≥ 8 GB**（见 `outputs/disk-report-2026-09-21.md`）。Tauri 的依赖树比自建窗口重得多，具体见「形态变更」。

---

## 形态变更：`wry + tao` → Tauri 2（2026-09-21）

初版选的是自建 `wry + tao` 窗口，理由是依赖树最小。用户改定为 **Tauri 2**，方案随之调整。

**改动带来的实际差异**：

| | `wry + tao`（原） | **Tauri 2（现）** |
|---|---|---|
| 窗口 / 菜单 / 托盘 | 自己写 | 框架提供 |
| 打包（`.app` / `.dmg` / `.exe` / AppImage） | 自己拼 | `tauri build` 一条命令 |
| 自动更新、单实例、深链 | 自己做 | 官方插件 |
| 前端资产嵌入 | 自己 `rust-embed` | 框架自带（`frontendDist`） |
| **Rust 依赖树** | 小 | **大**（`tauri` + `tauri-build` + `wry` + `tao` + 其传递依赖） |
| **首次编译产物** | 数百 MB | **约 2–4 GB** |
| 前端工具链 | 无要求 | 需要 `@tauri-apps/cli`（随包下载预编译二进制） |

**因此磁盘门槛从 10 GB 调整为 ≥ 8 GB，但要留意**：这个数字是「Tauri 编译产物 + `web/node_modules` + 既有 target」的合计估计。本机 2026-09-21 实测可用空间在 **1.7–5.4 GiB 之间剧烈跳动**，**大概率不够**，开工前应先执行磁盘报告的 A + B 类清理。

**一处架构上的选择（重要）**：Tauri 支持两种前后端通信方式 ——

1. **IPC（`invoke` 命令）**：最「Tauri 原生」，但前端的数据层要按 Tauri API 重写，
   且**浏览器访问会失效**；
2. **内嵌 HTTP 服务**：Tauri 窗口直接加载 `http://127.0.0.1:<port>/`，
   前端仍是普通 Web 应用。

**选 2。** 理由：界面只有一份实现，浏览器与桌面窗口共用；而且「服务只绑 127.0.0.1 +
token 鉴权」这套安全模型在 Python 版已经验证过，换成 IPC 等于把一套已验证的边界推翻重做。
代价是桌面版多一个本地监听端口 —— 这是已知且可接受的取舍，不是遗漏。

---

## 一、目标形态

```
wb-account-sync/
├── crates/                     # Rust 工作区
│   ├── wb-core/                # 迁移引擎（纯逻辑）
│   ├── wb-platform/            # 进程检测 / 退出 / 原生弹窗
│   ├── wb-autosync/            # launchd · systemd · Task Scheduler
│   ├── wb-accounts/            # 账号与用量探测
│   ├── wb-server/              # axum HTTP + SSE + 内嵌前端
│   └── wb-cli/                 # clap CLI（产物名 wb-bridge）
├── web/                        # TypeScript 前端工程（Vite）
│   ├── src/
│   └── dist/                   # 构建产物 → 由 wb-server / Tauri 消费
├── desktop/                    # Tauri 2 桌面壳
│   ├── src-tauri/              # Rust：主线程开窗口，后台线程起 wb-server
│   └── tauri.conf.json
├── legacy-python/              # 原 Python 实现（语义基准，冻结）
├── legacy-go/                  # 上一轮 Go 底座位（仅供参照）
├── packaging/                  # 打包脚本
└── tests/                      # 跨实现等价性测试
```

**唯一真相**：语义以 `legacy-python` 为准（它是线上实际跑通的实现）；Rust 是重写，不是重新设计。

---

## 二、依赖选型

### Rust

| 用途 | 选型 | 理由 |
|---|---|---|
| SQLite | `rusqlite` + `bundled` | 自带 SQLite C 源码，不依赖系统库，单文件打包省心。代价是首次编译慢 |
| JSON | `serde_json`（解析）+ **自写序列化层** | `plan_id` 要求复刻 CPython `json.dumps` 逐字节行为，序列化必须自己控制 |
| 保序 map | `indexmap` | `settings.json` 渠道合并不能丢键序 |
| SHA-256 | `sha2` | `plan_id` 指纹 |
| 目录遍历 | `walkdir` | 文件树哈希、体积统计 |
| CLI | `clap` v4（derive） | 子命令与 `--json` 契约 |
| HTTP | `axum` + `tokio` | SSE 一等支持。若依赖树过重，退路是 `tiny_http` 手写 SSE |
| 静态资源 | `rust-embed` | 把 `web/dist` 编进二进制 |
| 进程 | `sysinfo` | 跨平台进程探测 |
| 信号 | `libc`（unix）/ `windows` | 优雅退出客户端 |
| 原生弹窗 | `rfd` | 窗口模式无终端，异常必须走原生弹窗 |
| 桌面壳 | `tauri` 2 + `tauri-build` | 窗口 / 菜单 / 打包 / 单实例 / 更新一套齐 |
| 窗口实现 | `wry` + `tao`（由 Tauri 带入） | 系统 WebView（mac WKWebView / win WebView2）。**不再单独依赖**，由 Tauri 引 |
| 时间 | `time` | journal 时间戳 |
| 错误 | `anyhow`（CLI）+ `thiserror`（库） | — |

### TypeScript

- **只用 `tsc`，不引 Vite，也不引框架。**
  现有界面是手写的约 1200 行 vanilla JS，直接模块化为 TS 是最诚实的移植；
  而 Vite 的 `node_modules` 要 150–300 MB，`typescript` 只要约 25 MB ——
  **磁盘是当前最紧的资源**，这笔差价没有换来必要的能力：
  Tauri 要的只是一个静态目录，`tsc` 的 ESM 输出正好就是。
- 产物落到 `web/dist/`，是一个**自包含**静态站点（`tsc` 输出 + 复制进来的
  `index.html` / `style.css`）。Tauri 的 `frontendDist` 与 Rust 侧的资产嵌入
  都直接吃这个目录。
- **构建顺序**：先 `web`，后 Rust。`rust-embed` 在编译期就要读 `web/dist`，
  目录不存在会直接编译失败。CI 里也要按这个顺序。
- **数据层与渲染层分开**：`src/api.ts` 是纯逻辑、在浏览器与 Node 里都能跑，
  所以可以对着**真实后端**跑冒烟（`web/smoke.mjs`），而不是靠"页面看起来对"。
- 若将来状态管理变复杂，再评估 Preact（约 3 KB）。**现在不引**。

---

## 三、模块对照表

| Python（语义基准） | Go（legacy-go） | Rust 目标 |
|---|---|---|
| `wb_home_bridge.py` 的 plan_id 指纹 | `internal/pyjson` | `wb-core::pyjson` |
| home 探测 / 体积 | `internal/bridge/home.go` | `wb-core::home` |
| `build_plan` | `plan.go` / `buildplan.go` | `wb-core::plan` |
| 执行（事务 + undo journal） | `apply.go` | `wb-core::apply` |
| 备份（VACUUM INTO） | `backup.go` | `wb-core::backup` |
| 核验 | `verify.go` | `wb-core::verify` |
| 回滚 | `restore.go` | `wb-core::restore` |
| 记忆块合并 / settings.json 并集 | `memory.go` | `wb-core::memory` |
| 文件复制 / 树合并 | `fsutil.go` | `wb-core::fsutil` |
| `wb_platform.py` | `internal/platform` | `wb-platform` |
| `wb_autosync.py` | `internal/autosync`（只做了读侧） | `wb-autosync`（读写齐全） |
| `acct_probe.py` | — | `wb-accounts` |
| `wb_ui.py` 的 HTTP 层 | `internal/webui` | `wb-server` |
| `wb_ui.py` 的 `PAGE`（内嵌 HTML） | `static/index.html`（导出副本） | **`web/src/**/*.ts`（唯一真相）** |
| `wb_ui.py` CLI 装配 | `cmd/wb-bridge` | `wb-cli` |

---

## 四、实施顺序

每个阶段独立可验证、可提交。**不跨阶段并行**。

### P0 前置（已基本完成）

1. ~~腾磁盘~~ —— **仍未完成，是当前唯一的硬阻塞**。Tauri 定案后门槛见「形态变更」
2. ~~装 rustup~~ —— **本机本来就有**（`~/.rustup` 442M，rustc 1.98.1，target `aarch64-apple-darwin`），
   只是不在 PATH：用前 `export PATH="$HOME/.cargo/bin:$PATH"`。刻意 `--no-modify-path`，
   沿用仓库「自动探测解释器」的惯例
3. ~~冻结 Python 基准~~ —— 已完成，见 P1 的 golden 夹具
4. 建 `crates/` 工作区 —— 已完成；`web/` 工程骨架待建

**验收**：`cargo --version` 可用 ✓；`web/` 能 `npm run build` 出 `dist/`（待办）。

### P1 `wb-core::pyjson` —— ✅ 已完成（2026-09-21）

`plan_id` 是内容指纹，Python 与 Rust 必须**逐字节一致**。已复刻 CPython `json.dumps` 的：

- 浮点 `repr`（最短往返表示 + `-4 <= exp < 16` 的定点/科学计数切换）
- 非 ASCII 转义策略（`ensure_ascii=False`）
- 递归 `sort_keys`
- `separators` 行为
- 保序 map 的键序

**验收**：58 紧凑 + 9 缩进 golden 用例逐字节一致。**已通过**，10 项 Rust 测试 + 9 项 Python 测试全绿。
夹具由 CPython 现场算出（`tools/gen_pyjson_golden.py`），且在 3.9 与 3.13 下渲染结果完全相同。

### P2 `wb-core` 其余模块

- **`home` ✅ 已完成**（`src/home.rs`）：数据目录表示与目录扫描。
  这一层在**契约上** —— `dir_size` 的 `approx_bytes` 会进计划正文、决定 `plan_id`。
- **`plan` ✅ 已完成**（`src/plan.rs`）：`collect_rows` / `build_direction_entries` /
  `build_plan` 与 `plan_id`。引入 `rusqlite`（`bundled`）。
  **验收已达成**：两个用例（默认勾选 / 全开）的计划正文规范编码、条目、跳过计数、
  `plan_id` 与 CPython 全部一致。
- ⬜ 待做：`apply` / `backup` / `verify` / `restore` / `memory` / `fsutil`。

#### plan 的对账是怎么做的

1. `tools/gen_plan_golden.py` 在**固定路径**`/tmp/wb-plan-parity/` 上造出两个合成 home
   （固定路径是必需的：正文含 home 的绝对路径，两边必须跑在同一份路径上），
   跑一遍 CPython 的 `build_plan`，把结果落盘。
2. 夹具存**被哈希的那串规范编码原文**，而不只是哈希 —— 只比哈希的话失败信息是一串
   十六进制，你只知道不一样、不知道哪里不一样。
3. Rust 侧 `tests/plan_golden.rs` 把同一份夹具回放出来，逐项比对。
4. Python 侧 `tests/test_plan_golden.py` 守夹具的时效性、覆盖度，以及
   **「凭据绝不进计划」**这条产品承诺。

夹具里刻意埋了一个 `connectors/<uid>/.master.key`，并**反向**也断言它确实存在 ——
否则「它没出现在计划里」只是在证明一个不存在的东西没出现。

**剩余部分的验收**：用同一套夹具跑 plan → apply → verify → restore，
数据库逐行、文件树哈希、记忆文件与 `settings.json`（时间戳归一化后）逐项对账。

### P3 `wb-platform` + `wb-cli`

子命令对齐：`status` / `quit-clients` / `survey` / `plan` / `apply` / `backup` / `verify` / `restore` / `serve` / `sync`。

**硬约束**：`--json` 模式下日志必须走 stderr，stdout 只能是纯 JSON（否则 `--json | jq` 直接失败——Go 版踩过这个坑）。

**验收**：CLI 契约测试（`survey` / `plan` 逐字段、`--json` 纯 JSON、`sync --dry-run` 不落盘）。

### P4 `web/` TypeScript + `wb-server`

把 `wb_ui.py` 里那 1200 行内嵌 HTML 拆成 TS 模块：`api.ts`（带 token 的 fetch 封装）、`state.ts`、`views/`、`components/`。

**验收**：所有端点等价；前端**单一真相是 `web/src`**（构建产物进 `rust-embed`）。

### P5 `wb-autosync`

补齐 install / pause / uninstall 的写入侧：macOS launchd plist、Linux systemd timer、Windows 计划任务。

**硬约束**：只有确认两个客户端**都已退出**时才真正写入；否则只扫一次进程就退出，不碰任何数据。

### P6 Tauri 桌面壳

`desktop/`：主线程开窗口，后台线程起 `wb-server`，窗口加载 `http://127.0.0.1:<port>/`。

**两条已验证的教训必须沿用**：
- 窗口**只能在主线程创建**（Cocoa 硬性要求）→ HTTP 服务跑后台线程，主线程阻塞在窗口事件循环
- 启动窗口后必须**回调确认它真的起来了**：没有图形会话时 `start()` 会二话不说直接返回，若当成「正常关闭」，用户看到的就是一闪而过

**打包**：`tauri build` 出 `.app` / `.dmg`（mac）、`.exe` / `.msi`（win）、AppImage / deb（linux）。
这一步的输出物与 Python 版 PyInstaller 的 `.app` **不能同时装在 `/Applications`**（同名冲突），
切换时先卸载旧的。

### P7 收口

Python / Go 归档进 `legacy-*/`，CI 主路径切到 Rust + TS。`--selftest` 保留为排障统一入口。

---

## 五、验收口径

| 项 | 标准 |
|---|---|
| `plan_id` | 与 Python 冻结夹具**逐字节一致** |
| 执行语义 | apply / verify / restore 对合成夹具逐项对账通过 |
| CLI `--json` | stdout 纯 JSON，可管道进 `jq` |
| 界面 | 端点等价；`web/src` 是唯一真相 |
| 幂等性 | 重复执行不产生变更 |
| 漂移拒绝 | 客户端未退出时建计划 → 指纹漂移 → 执行被拒 |
| 回滚 | undo journal 能完整回退 |

---

## 六、风险清单

| 风险 | 等级 | 应对 |
|---|---|---|
| **磁盘不足** | **高** | 实测在 1.7–5.4 GiB 之间跳动，而 Tauri 首次编译要 2–4 GB。P0 不做完无法开工 |
| **Tauri 首次编译体积** | **高** | 依赖树大是选 Tauri 的已知代价。先用 `cargo check` 探路，别一上来 `cargo build` |
| CPython 浮点 repr 复刻不精确 | 高 | P1 单独立项，golden 夹具守着；已有 Go 版实现可对照 |
| `rusqlite bundled` 首次编译慢 | 中 | 接受一次性的长编译；不要在磁盘紧张时跑 |
| 无图形会话时窗口静默失败 | 中 | 沿用回调确认 + 无图形会话退回浏览器 |
| 测试口径要改 | 中 | `test_go_ui_parity.py` 的**逐字节比对**在 TS 化后失效，必须改成「TS 构建产物为唯一真相 + 功能等价断言」 |
| 版本线分叉 | **中** | `/Applications` 里现装的仍是 Python 版 `.app`。Tauri 版同名，切换前必须先卸载旧的，否则「用户跑的是哪一版」说不清 |
| Windows 侧未真机验证 | 中 | 数据目录与 `WorkBuddy.exe` 进程名仍是推断值；自动探测失败时可显式指定路径 |
| 依赖树继续失控 | 中 | 除 Tauri 外一律优先「依赖少」；`axum` 若过重则退 `tiny_http` |

---

## 七、明确不做的事

- 不引 Electron：桌面壳只用一个（Tauri 2），两套壳会让打包与签名翻倍
- 桌面版不用 Tauri IPC 替代 HTTP：界面只保留一份实现，浏览器与桌面窗口共用（详见「形态变更」）
- 不在 Rust 侧重新设计语义：Python 是唯一真相，Rust 只做等价复刻
- 不在这一轮动 `legacy-python/` 的内容：它是基准，冻结不改
- 不手工复制前端产物：TS 构建是唯一来源，禁止两边各维护一份
