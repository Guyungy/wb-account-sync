# 跨 App 数据目录打通（wb-home-bridge）

> 状态：**实验工具**，与主 CLI（`wb-account-sync`）互相独立。
> 主 CLI 的"本版不支持跨目录搬家"结论仅针对主 CLI；本工具是另一条实现路径，
> 有自己的安全边界。未获 WorkBuddy 或其厂商背书。

## 要解决的问题

本机装了两个**互相独立**的客户端（以本机 macOS 安装为例）：

| | WorkBuddy | WorkBuddy AI |
|---|---|---|
| macOS App | `/Applications/WorkBuddy.app` | `/Applications/WorkBuddy AI.app` |
| Windows 可执行文件 | `WorkBuddy.exe` | `WorkBuddy AI.exe` |
| macOS 数据目录 | `~/.workbuddy` | `~/.workbuddy-ai` |
| Windows 数据目录（推断） | `~/.workbuddy` 或 `%APPDATA%\WorkBuddy` | 类似 |

表格中的版本号为 2026-09-16 本机安装时的快照，以你实际安装的版本为准。

它们各自读写自己的目录，**零共享通道**：在 WorkBuddy 里聊过的会话，打开 WorkBuddy AI 看不到。

本工具让两边**互相看到对方的全部历史会话**（双向），并把长期记忆与技能打通。

## 为什么这次能做到「两边都保留」

这是与"同一 home 内跨账号迁移"的关键区别：

- **同一 home 内**：一条会话行只有一个 `user_id`，改它必然是**搬走**。所以主 CLI 的
  `share-preview` 被判为"不可执行"。
- **跨 home**：两个 SQLite 各自独立。同一 `session.id` 可以在两边**各存一份**，
  各自的 `user_id` 指向各自的当前账号 → **两边都留**。

## 数据分层（实测，2026-09-16）

| 内容 | 位置 | 是否搬 |
|---|---|---|
| 会话行（含归属） | `<home>/workbuddy.db` → `sessions` | ✅ 只 INSERT 目标缺的行 |
| 用量统计 | 同库 `session_usage`（PK `session_id`） | ✅ 跟随会话 |
| **对话正文** | `<home>/projects/<cwd-slug>/<id>.jsonl` | ✅ 核心 |
| 正文配套 | 同目录 `<id>.meta.json`、`<id>.file-rollback.ndjson`、`<id>/tool-results/`、`<id>/subagents/` | ✅ |
| Todo 列表 | `<home>/tasks/<id>/<n>.json` | ✅ |
| 改动详情 / 索引 | `<home>/changes-detail/<id>/`、`changes-index/<id>/` | ✅（`--no-changes` 可省） |
| 文件快照回滚 | `<home>/file-history/<id>/` | ✅（同上） |
| 产物索引 | `<home>/artifact-index/<id>.json` | ✅ |
| 大对象 | `<home>/blobs/**`（hash 命名，内容寻址） | ✅ 目录并集 |
| 长期记忆 | `<home>/memory/<uid>_memory.md` | ✅ 行级并集 + 改写 uid |
| 用户技能 | `<home>/skills/**` | ✅ 目录并集（同名各留各的） |
| 渠道绑定 | `<home>/settings.json` → `claw.users.<uid>` | ✅ 补缺失 |
| 自动化 | 同库 `automations` 等 4 张表 | ⚠️ 默认**不搬**（见下） |
| 连接器凭据 | `<home>/connectors/<uid>/.credentials.v3.json` | ❌ 默认不搬（见下） |
| trace 遥测 | `<home>/traces/<workerPid>/` | ❌ 不搬（按进程 PID 分桶，跨 App 无意义，省 ~470MB） |
| 日志 / 应用缓存 | `logs/`、`app/` | ❌ 不搬 |

### 为什么不需要路径映射

`sessions.cwd` 指向本机真实目录（如 `$HOME/WorkBuddy/2026-09-16-09-08-41`），
**与是哪个 App 创建的无关**。所以 cwd 原样搬运即可，不需要改写。

实测：WorkBuddy 有 37 条活会话、WorkBuddy AI 有 28 条；两边 9 个同名项目桶中
**同 id 重叠为 0**，所以 `projects/` 目录合并天然不冲突。

### 为什么自动化默认不搬

自动化是定时任务。若同一定时任务在两个 App 各存在一份，**两个 App 都会触发**——
例如"日报"会在两个客户端各发一遍。默认跳过；需要时用 `--include-automations`，
复制过去统一落地为 `PAUSED`，由你手工决定启用哪个。

### 为什么连接器凭据不搬

`connectors/<uid>/.credentials.v3.json` 是用该 home 的 `.master.key` 加密的。
跨 home 复制密文而目标 home 用自己的密钥，**解不开**。所以：

- 默认完全不碰 `connectors/`。
- `--include-connectors` 只并 `connector-states.json` 与 `mcp.json`（开关状态），
  **不碰** `.master.key` 与凭据；目标 App 需要重新授权连接器。

## 安全模型

- **只新增**：DB 只 `INSERT OR IGNORE` 目标不存在的主键；文件已存在即跳过。
  目标侧原有数据不会被 UPDATE / DELETE。
- **写前必须先退出两个客户端**。主进程可执行文件名是 `Electron` 而非 `WorkBuddy`，
  本工具按完整路径匹配，不会静默失效。逃生开关：`--allow-client-running`（不推荐）。
- **计划冻结**：`plan_id` 由两个 home 的账号、选项、源侧完整行指纹、文件条目清单
  共同决定。执行前重算，任何漂移直接拒绝。
- **确认串**：`apply` 必须传完整 `plan_id`，不支持短前缀或通用 `--yes`。
- **磁盘预检**：目标卷剩余空间不足（需 ≥ 计划体积 + 1GB 余量）直接拒绝。
- **undo journal**：`<state-dir>/runs/<plan_id>/` 记录插入的行主键与新建路径，
  `restore` 可逐行撤销。**不是整库备份**，也不回滚被合并改写的记忆文件
  （记忆改写前会留 `.before-bridge-*` 副本）。

## 使用

### 浏览器界面（推荐）

最直观，且能实时看到两个客户端是否已完全退出。

**macOS：在 Finder 里双击 `tools/ui.command`** —— 会自动挑解释器、启动服务、打开浏览器。
也可以把它拖到 Dock 上，当成一个 App 用。

命令行等价写法（三平台通用）：

```sh
cd wb-account-sync
python3 tools/wb_ui.py            # 默认自动挑端口
python3 tools/wb_ui.py --port 8788 # 固定端口
```

启动后终端会打印完整链接（形如 `http://127.0.0.1:8788/?t=...`），浏览器也会自动打开。
**这个链接必须带 `t=` 参数**，它是本次会话的一次性访问凭据。

服务**只监听 `127.0.0.1`**，每次启动生成一次性 token，
所有 `/api/*` 接口都校验 token；token 不匹配或过期无法访问。

页面流程：**读取盘点 → 勾选迁移范围 → 生成计划 → 审阅 plan_id →
粘贴确认 → 执行迁移 → 核验 / 回滚**。

「执行」按钮在两个客户端完全退出前保持锁定；页面每 2 秒轮询一次进程状态。
Windows 用户同样适用：直接在 PowerShell 里运行 `python tools\wb_ui.py`。

**常见问题**

| 现象 | 原因 / 处理 |
|---|---|
| 打不开 / 连接被拒 | 服务已停。重新双击 `tools/ui.command`，或重跑上面的命令 |
| 403 token 无效 | 用的是旧链接。每次启动 token 都会变，用终端新打印的那条 |
| 端口被占用 | 工具会自动换端口；也可 `--port` 指定。`WB_UI_PORT=9123 ./tools/ui.command` |
| 想换数据目录 | `python3 tools/wb_ui.py --home-a /path/A --home-b /path/B`（探测不到时必填） |
| 「执行」按钮是灰的 | 两个客户端还有进程在跑。在 WorkBuddy 与 WorkBuddy AI 里各按 ⌘Q |

### 一条命令

适合不想开浏览器，或在 CI/脚本中运行：

**macOS / Linux：**

```sh
cd wb-account-sync
./tools/bridge-run.sh
```

**Windows：**

```powershell
cd C:\Path\To\wb-account-sync
python tools\bridge_run.py
```

流程：**客户端退出检查 → 备份 → 生成计划 → 人工确认 plan_id → 执行 → 核验**。

⚠️ 必须在**客户端外部终端**运行：macOS 用 Terminal.app，Windows 用 PowerShell / CMD。
WorkBuddy 与 WorkBuddy AI 都要完全退出；不能在 WorkBuddy 内部会话里跑——
退出客户端会连带杀掉正在执行的进程。

常用参数：

```sh
./tools/bridge-run.sh --no-backup      # 跳过备份（不推荐）
./tools/bridge-run.sh --no-changes     # 不搬 changes-detail/file-history，省约 150MB
```

### 手工分步

```sh
P=python3   # 或 ~/.workbuddy/binaries/python/versions/*/bin/python3
S=~/.wb-home-bridge

$P tools/wb_home_bridge.py survey                      # 只读盘点两个 home
$P tools/wb_home_bridge.py backup --dest ~/wb-home-bridge-backups --label before-bridge
$P tools/wb_home_bridge.py plan --output $S/plan.json  # 只读，生成计划
$P tools/wb_home_bridge.py apply --plan $S/plan.json --state-dir $S --confirm <plan_id>
$P tools/wb_home_bridge.py verify --plan $S/plan.json
$P tools/wb_home_bridge.py restore --run-dir $S/runs/<plan_id> --confirm <plan_id>
```

### 选项

| 选项 | 作用 |
|---|---|
| `--no-changes` | 不搬 `changes-detail` / `changes-index` / `file-history` |
| `--no-skills` | 不合并用户技能目录 |
| `--no-memory` | 不合并长期记忆 |
| `--no-claw` | 不合并 `settings.json` 渠道绑定 |
| `--include-automations` | 复制自动化定义（落地为 `PAUSED`） |
| `--include-storage` | 复制账号个人存储（改名合并，已存在不覆盖） |
| `--include-connectors` | 只并连接器开关状态，不搬凭据 |
| `--include-plugins` | 合并 `plugins/cache` |
| `--overwrite-assets` | 已存在的资产文件也覆盖（默认跳过） |

### 备份范围

默认排除 `app/`、`logs/`、`traces/`（本机实测共 ~5GB，与迁移无关）。
需要连日志留档时加 `--include-heavy`。

## 自检

```sh
python3 tools/synth_check.py
```

用**真实 schema** 造两个假 home，端到端跑 survey/plan/apply/verify/restore，
覆盖 25 项断言：双向归属重映射、正文与资产到位、记忆并集与 uid 改写、技能/blobs 并集、
连接器主密钥不被覆盖、幂等、漂移拒绝、回滚。不接触真实账号。

## 跨平台说明

* **macOS**：数据目录 `~/.workbuddy` / `~/.workbuddy-ai`、进程按 `.app` 路径匹配，已在本机实测。
* **Windows**：官网提供 `WorkBuddySetup.exe`，但数据目录布局**未经本机验证**
  （无 Windows 环境）。工具会按候选列表探测 `~/.workbuddy`、`%APPDATA%\WorkBuddy` 等位置；
  若探测失败，请显式指定 `--home-a` / `--home-b`。
* **Linux**：仅有尽力而为的进程检测，不能保证识别 Electron 主进程；
  主要用于合成夹具测试与只读能力验证。

所有入口共用同一套引擎：`wb_home_bridge.py` 负责迁移逻辑，`wb_platform.py`
负责跨平台抽象，`wb_ui.py` / `bridge_run.py` 只是界面/壳子。

## 未覆盖 / 已知限制

- **界面展示未经验证**：执行后需**重启两个 App**，客户端有内存缓存。
- **云端同步行为未知**：新写入的会话可能被上传到目标账号的云端，可能产生重复记录。
  同步映射见 `<home>/edge-sync-mapping-v*.db`。
- **跨 App 的权限模型**：会话归属改成目标账号，权限随之变化，未验证。
- **已删除会话不搬**（`deleted_at IS NOT NULL`，WorkBuddy 侧 47 条 / AI 侧 9 条）。
- **9 条会话的 cwd 已不存在**（如 `$HOME/Code/LensCheck`）：行与正文照搬，
  但点开可能提示目录不存在。工具不会替你创建目录。
- **Windows 数据目录推断**：如果候选路径都不对，需要手工指定 `--home-a` / `--home-b`。
- 无跨设备迁移、无 PyPI 安装、无签名 GUI。
