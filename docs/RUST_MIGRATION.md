# Rust + TypeScript 迁移方案

- 定稿：2026-09-21
- 决策依据：用户要求「改一下用 Rust TypeScript 为主」，界面形态选定 **wry + tao 自建窗口**，Go 底座移入 `legacy-go/` 保留参照
- 前置条件：**磁盘可用空间 ≥ 10 GB**（见 `outputs/disk-report-2026-09-21.md`）。当前 257 MiB，是开工的硬阻塞。

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
│   └── dist/                   # 构建产物 → rust-embed 打进 wb-server
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
| 窗口 | `wry` + `tao` + `raw-window-handle` | 系统 WebView（mac WKWebView / win WebView2），依赖树最小 |
| 时间 | `time` | journal 时间戳 |
| 错误 | `anyhow`（CLI）+ `thiserror`（库） | — |

### TypeScript

- **Vite + TypeScript，不引框架**。现有界面是手写的约 1200 行 vanilla JS，直接模块化为 TS 是最诚实的移植，产物最小。
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

### P0 前置（当前卡在这里）

1. 腾出 ≥ 10 GB（执行磁盘报告里的 A + B 类清单）
2. 装 rustup（stable，`aarch64-apple-darwin`）
3. 冻结 Python 基准：确认 `legacy-python` 的 plan_id 输出，抽成 golden 夹具
4. 建 `crates/` 工作区与 `web/` 工程骨架

**验收**：`cargo --version` 可用；`web/` 能 `npm run build` 出 `dist/`。

### P1 `wb-core::pyjson` —— 先做，因为它是地基

`plan_id` 是内容指纹，Python 与 Rust 必须**逐字节一致**。要复刻 CPython `json.dumps` 的：

- 浮点 `repr`（最短往返表示，指数格式差异是重点）
- 非 ASCII 转义策略
- 递归 `sort_keys`
- `separators` 行为
- 保序 map 的键序

**验收**：对 Python 冻结的 golden 夹具逐字节比对通过。**这一项不通过，后面全部没有意义。**

### P2 `wb-core` 其余模块

home → plan → apply → backup → verify → restore → memory → fsutil。

**验收**：用现有合成夹具（复制成两组目录）跑 plan → apply → verify → restore，数据库逐行、文件树哈希、记忆文件与 `settings.json`（时间戳归一化后）逐项对账。

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

### P6 打包

`wry` + `tao` 自带窗口的 `.app` / `.exe` / Linux 单文件。

**两条已验证的教训必须沿用**：
- 窗口**只能在主线程创建**（Cocoa 硬性要求）→ HTTP 服务跑后台线程，主线程阻塞在窗口事件循环
- 启动窗口后必须**回调确认它真的起来了**：没有图形会话时 `start()` 会二话不说直接返回，若当成「正常关闭」，用户看到的就是一闪而过

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
| **磁盘不足** | **高** | 当前 257 MiB。P0 不做完无法开工 |
| CPython 浮点 repr 复刻不精确 | 高 | P1 单独立项，golden 夹具守着；已有 Go 版实现可对照 |
| `rusqlite bundled` 首次编译慢 | 中 | 接受一次性的长编译；不要在磁盘紧张时跑 |
| wry 窗口静默失败 | 中 | 沿用回调确认 + 无图形会话退回浏览器 |
| 测试口径要改 | 中 | `test_go_ui_parity.py` 的**逐字节比对**在 TS 化后失效，必须改成「TS 构建产物为唯一真相 + 功能等价断言」 |
| Windows 侧未真机验证 | 中 | 数据目录与 `WorkBuddy.exe` 进程名仍是推断值；自动探测失败时可显式指定路径 |
| 依赖树失控 | 中 | 选型一律优先「依赖少」；`axum` 若过重则退 `tiny_http` |

---

## 七、明确不做的事

- 不做原生多端框架（Tauri / Electron）：用户已选定 wry + tao，依赖树最小
- 不在 Rust 侧重新设计语义：Python 是唯一真相，Rust 只做等价复刻
- 不在这一轮动 `legacy-python/` 的内容：它是基准，冻结不改
- 不手工复制前端产物：TS 构建是唯一来源，禁止两边各维护一份
