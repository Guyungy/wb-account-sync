# 更新记录

## 未发布 — Go 底座（第二步：执行路径）

把 apply / backup / verify / restore 迁到 Go。Plan 的等价性只证明"两边看数据
的方式一样"，真正会损坏用户数据的是 apply，所以这一轮的重点全部在**结果可比对**。

- Go 侧新增 `internal/bridge/{apply,backup,restore,verify,fsutil,memory}.go`，
  CLI 补齐 `apply` / `backup` / `verify` / `restore` 四个子命令，与 Python 版参数同名同义。
- `apply` 保留了"执行前重新生成计划并比对 plan_id"的漂移检测：计划是某一刻的快照，
  源侧之后又写入就会**不报错地漏数据**，所以宁可整个拒绝。这个语义两边一致，测试里
  专门断言了"第二次 apply 在两边都以漂移为由被拒，且拒绝时一个字节都没动"。
- 数据库写入用 `BEGIN IMMEDIATE`：客户端可能正在后台跑，先拿写锁才能在
  "发现冲突"和"改了半截"之间留出明确边界。
- 备份改用 `VACUUM INTO`，不再复制文件：数据库处于 WAL 模式，只拷 `.db` 会丢掉
  还在 WAL 里的已提交事务——备份看着成功了，恢复出来却少一截。
- 新增 `internal/pyjson/ordered.go`：保序 JSON 往返。`settings.json` 的渠道绑定合并
  要**重写用户的配置文件**，而 Go 的 `map[string]any` 会丢键序（"加一条绑定"变成
  "整份文件重排"）、还会把大整数变成 float64 丢精度。这里保留原始数字文本与键序。
- `pyjson.Normalize` 加了一层反射兜底：日志记录里随手写的 `[]string`、`map[string]int`
  以前会让编码器直接 panic——而那是**迁移执行到一半**的时刻，数据库已经写了半截。
  现在统一折成标准形状，编码器的类型覆盖面不再成为执行路径的软肋。
- `restore` 明确不负责两件事（与 Python 版一致，且是刻意的）：并集目录
  （blobs / skills / connectors-skills）里的新增文件不删——这些目录内容寻址，
  删一个可能影响另一侧已有引用；被合并改写的记忆原文只留 `.before-bridge-*` 备份。
- 新增 `tests/test_go_apply_parity.py`（8 项）：同一份合成夹具复制成两组互不相干的
  目录，两边各跑完整 plan → apply → verify → restore，逐项对账数据库内容、
  文件树内容哈希、记忆文件与 settings.json（时间戳归一化后逐字节比）、
  备份清单；并单独断言凭据（`.master.key`）绝不跨 home 复制。

已知且**未修**的问题（原样保留以保证两边行为一致，待单独决策）：
`memory` 合并时正则取的是 `RAW_JSON_START` 之前的内容，会把 `<!--` 这个
注释起始符留在正文里，导致每次合并都在记忆文件里多出一行残留。
不臆改是因为它会改变写入用户长期记忆的语义。

## 未发布 — macOS / Windows 桌面应用

把工具打成各平台的原生应用，用户不需要装 Python，双击图标即用。

- 新增 `tools/app_main.py`：打包产物的桌面入口。窗口模式没有终端，异常会被静默
  吞掉，现象只是"双击没反应"，所以这里包一层——捕获全部异常与 `SystemExit`，
  用原生对话框（macOS `osascript`、Windows `MessageBoxW`）摊开给用户，
  再返回非零退出码。同时补了 stdout/stderr 兜底：Windows `--noconsole` 构建下
  这两个流是 `None`，连 `argparse --version` 都会抛 `AttributeError`。
- 新增 `--selftest`：不启动服务，只验证打包完整性（四个模块能否 import、
  客户端能否探测），输出 JSON，`ok: false` 即失败。打包漏模块时这是唯一的可见信号，
  CI 拿它做冒烟测试。
- 新增 `packaging/wb-account-sync.spec`：macOS 出 `wb-account-sync.app`
  （onedir + BUNDLE），Windows 出单文件 `wb-account-sync.exe`（无控制台）。
  剔除 `tkinter` 等无用体积；`hiddenimports` 显式列出 `tools/` 里的同目录模块
  ——它们靠运行时 `sys.path` 注入加载，PyInstaller 的静态分析追不到这条路径。
  版本号从 `wb_account_sync/__init__.py` 读，写进 `.app` 的 `Info.plist`。
- 新增 `.github/workflows/app-build.yml`：`macos-latest` + `windows-latest` 矩阵
  （PyInstaller 不支持交叉编译，只能各平台各自构建）。构建后自检、ad-hoc 签名、
  用 `ditto` 打包（保留符号链接与权限位），推 `v*` 标签时自动附加到同名 Release
  并附上 `SHA256SUMS-desktop.txt`。
- 新增 `tools/ui.bat`：Windows 的免打包启动器，自动探测 Python 3.10+。
  提示文本刻意全用 ASCII——cmd.exe 按活动 OEM 代码页逐行解码 `.bat`，中文不可靠。
- `tools/wb_ui.py`：
  - 自动同步状态在非 macOS 返回 `supported: false`，界面改说"本平台暂不支持"
    而不是"模块不可用"；非 macOS 下不再 import `wb_autosync`（内含 launchctl / osascript）。
  - 新增 `IS_FROZEN`：打包版没有 `tools/` 目录，自动同步的安装指引改为说明文案，
    不再给出跑不通的命令。
  - **修复 `pick_port(0)`**：`bind(("127.0.0.1", 0))` 一定会成功（0 的含义就是
    "随便给一个"），而函数此前直接返回传进来的 0，于是自动挑端口时打印出来、
    并交给浏览器的地址是 `http://127.0.0.1:0/`——一个打不开的地址，
    用户看到的现象正是"双击没反应"。现在统一回读 `getsockname()` 取内核真正
    分配的端口。
  - **修复浏览器打开**：新增 `open_browser()` / `_spawn()`。macOS 优先用
    `/usr/bin/open`（直接经 LaunchServices），因为 `webbrowser` 在 mac 上走的是
    osascript AppleEvent，打包后实测失败：`execution error: AppleEvent已超时 (-1712)`。
    其余平台退回 `webbrowser`，再不行用 `os.startfile` / `xdg-open`。
  - **自动开浏览器失败时亮出地址**：打包版没有终端，带 token 的地址只打印在
    stdout 里——浏览器一旦没弹出来，用户既看不到界面也拿不到地址，现象和
    "双击没反应"一模一样。新增 `NOTIFY_HOOK` 注入点，`app_main.py` 把原生弹窗
    （macOS `osascript` / Windows `MessageBoxW`）交给界面层，失败时把完整 URL
    摆在用户面前让他自己复制。命令行运行不注入钩子，保持静默（终端里本来就
    看得见地址）。
  - **修复 `BrokenPipeError` 被误报成失败**：`--selftest | grep '"ok"'` 这类
    下游提前关管道的正常用法，此前会走进 `except Exception`，被弹成"自检失败"、
    退出码变 1——CI 因此误判为构建失败。现在 `--selftest` 与界面主流程都单独
    吞掉 `BrokenPipeError`。
- **界面改为应用自己的窗口**（不再往外跳浏览器）：新增 `--window` 与
  `open_window()`，用 pywebview 开窗口——macOS WKWebView、Windows WebView2，
  都是系统自带组件，不用另外装运行时。窗口只能在主线程创建（Cocoa 的硬性
  要求），所以 HTTP 服务挪到后台线程、主线程阻塞在 `webview.start()`。
  打包入口默认注入 `--window`，加 `--no-window` 回到浏览器方式。
  - `open_window()` 靠 `webview.start()` 的回调确认窗口**真的起来了**：
    某些没有图形会话的环境下 `start()` 会二话不说直接返回，若把它当成
    "窗口正常关闭"，用户看到的是一闪而过或干脆什么都没有——又绕回
    "双击没反应"。没确认起来就返回 `False`，退回浏览器并弹窗给地址。
  - `packaging/wb-account-sync.spec` 用 `collect_all("webview")` 收齐它的 js
    注入脚本，并按平台显式声明后端（`webview.platforms.cocoa` /
    `webview.platforms.edgechromium`）——后端是**按平台动态挑**的，
    静态分析追不到，漏了就和漏打 `wb_ui` 一样：不报错，只是静默退回浏览器。
  - `--selftest` 新增 `webview` 字段；CI 断言它必须是 `ok`——漏装 pywebview
    时构建照样能绿，只有这一项拦得住。
  - CI 的构建依赖从 `pyinstaller` 变成 `pyinstaller pywebview`。
  - 包体积：macOS 从 20 MB 涨到 **39 MB**（内嵌 pywebview 与 pyobjc）。
- 新增 `docs/DESKTOP_APP.md`：下载方式、首次放行（Gatekeeper / SmartScreen）、
  平台功能对照、自行构建与已知限制。
- **Windows 侧仍未在真机验证**：数据目录候选（`%APPDATA%\WorkBuddy` 等）与
  客户端进程名（`WorkBuddy.exe`）都是推断值。文档已把"先用任务管理器核对进程名"
  列为 Windows 首次使用的前置检查——进程名对不上会导致"客户端在运行却显示已退出"，
  这是危险方向的错误。

## 0.3.0a1 — 2026-09-16

首个公开发布版（Alpha）。在 0.2.0a1 的基础上保持范围收紧，新增跨 App 数据目录打通
与机会式自动同步。**不是生产稳定版**，请勿在不可替代的数据上首次运行。

### 自动同步（机会式 launchd 代理）

- 新增 `tools/wb_autosync.py`：跨 App 双 home 的**机会式自动同步**。
  - 只支持 macOS 的 `install` / `uninstall` / `status` / `pause` / `resume` / `run-now`
    子命令；核心 `run` 子命令跨平台，Windows / Linux 可用系统调度器调用。
  - 每 120 秒（可 `--interval`）检查一次；两个客户端都退出时才执行，否则只扫一次
    `ps` 就退出，不碰数据。
  - 首次运行、或检测到会话/技能/记忆变化、或距上次完整扫描超过 24 小时 →
    走完整「生成计划 → 漂移校验 → 执行 → 核验」流程。
  - 免去 `plan_id` 人工确认，但保留：漂移拒绝、两个 `workbuddy.db` 的 sqlite 在线备份、
    undo journal、一键回滚命令、最近 `--keep` 次运行记录保留。
  - 状态摘要（会话 id 集合、技能名单、记忆文件）用于无变化跳过，避免空转。
- 新增 `tools/autosync_check.py`：10 项合成夹具端到端断言，不碰真实账号。
- `tools/wb_ui.py`：界面新增「自动同步」卡片，显示是否安装/暂停、最近一次结果、
  会话数、回滚命令。
- 更新 `docs/HOME_BRIDGE.md`、`README.md`、界面截图 `docs/images/ui.png`。

### 许可证变更为 GPL-3.0-or-later

- **许可证从 MIT 改为 GNU GPL v3.0 或更新版本**（`GPL-3.0-or-later`）。
  `LICENSE` 替换为 GPL-3.0 官方全文；`pyproject.toml` 的 `license` 同步更新，
  README 增加版权与授权说明。
  - 影响：衍生作品需以相同许可证开源。若需闭源商用，需另行取得授权。
  - 说明：此前已按 MIT 发布的版本（历史提交）仍可依 MIT 使用，本次变更仅对之后版本生效。
- 新增 `tools/ui.command`：macOS 双击启动浏览器界面，自动探测 Python 3.10+。
- `tools/wb_ui.py` 新增 `--token` 与 `--handshake`（就绪后向 stdout 输出一行
  `WBUI_READY {json}`，供宿主程序解析）。
- 去敏：移除文档与脚本中的本机绝对路径，改为 `$HOME` 与自动探测。
- README 重写：徽章、界面截图、目录、快速开始、安全模型。
- `.gitignore` 忽略项目级 `.workbuddy/`。

### 跨 App 数据目录打通（实验）

与主 CLI 互相独立的另一条路径，针对"两个独立客户端各用一个 home"的场景。

- 新增 `tools/wb_home_bridge.py`：跨 home **双向**打通。实测两个客户端
  （`WorkBuddy.app` / `WorkBuddy AI.app`）home 独立且零共享通道，但两个 SQLite
  各自独立，同一会话 id 可各存一份 → **两边都保留**可实现（与同一 home 内只能搬走不同）。
- 定位到会话正文真实位置：`projects/<cwd-slug>/<conversationId>.jsonl`（不在 DB 里）；
  `sessions.id` == 该文件名（实测 82/84、37/37 命中）。`traces/` 按 workerPid 分桶，
  跨 App 复制无意义，不搬（省 ~470MB）。
- 命令：`survey` / `backup` / `plan` / `apply` / `verify` / `restore`。
- 安全：DB 只 `INSERT OR IGNORE` 缺失主键，文件只新增；完整 `plan_id` 确认；
  源侧指纹漂移即拒绝；磁盘空间预检；undo journal 可逐行回滚。
- 自动化默认不搬（否则两个 App 会各跑一遍定时任务）；连接器凭据不搬
  （各 home 的 `.master.key` 不同，密文跨 home 解不开）。
- 新增 `tools/bridge-run.sh`（备份 → 计划 → 人工确认 → 执行 → 核验）与
  `tools/synth_check.py`（合成夹具 25 项端到端断言，不碰真实账号）。
- 新增 `tools/wb_platform.py`：跨平台抽象层。macOS 用 `ps` 按 bundle 路径匹配 Electron 主进程，
  Windows 用 `tasklist` 按镜像名匹配，Linux 尽力而为；home 目录自动探测，Windows 候选路径
  为推断值，不对时允许显式 `--home-a` / `--home-b`。
- 新增 `tools/wb_ui.py`：本地浏览器界面。服务只绑 `127.0.0.1`，每次启动生成一次性 token；
  前端可实时看到两个客户端是否已退出、读取盘点、勾选迁移范围、生成并审阅计划、
  粘贴 plan_id 执行、查看进度、回滚。
- 新增 `tools/bridge_run.py`：跨平台的「备份 → 计划 → 人工确认 → 执行 → 核验」
  交互流程，Windows 用户可直接 `python tools\bridge_run.py`。`bridge-run.sh` 改为薄壳，
  内部调用 `bridge_run.py`。
- `wb_home_bridge.py` 默认 home 改为按平台自动探测，进程检测与退出指引改用 `wb_platform`。
- 文档：`docs/HOME_BRIDGE.md`。

### 共享需求预览与隔离演练

- 新增 `demo`：三个纯虚拟账号的十项只读断言，不访问真实账号，不模拟客户端写回成功。
- 新增 `share-preview`：显式账号的普通会话并集、原始归属、排除数量与账号绑定元数据提醒。不能传给迁移执行器。
- 新增 `preferences-preview`：对显式设置文件进行默认拒绝、白名单字段差异分析；冲突保留目标，不导出字段值，不写回。
- 修复父级路径穿越及等价路径表示导致的目录边界校验缺口；JSON 读取同时拒绝符号链接父目录。
- `doctor` 明示自动同步和设置同步未实现；空迁移计划的核验明确返回 `no_op`，不冒充同步成功。
- 增加合成测试，更新真实同步与客户端验收的未完成边界。
- 没有重新启用旧 `live/sync`、复制凭据、安装服务或修改任何真实账号。多账号历史保留、偏好写回、规则/技能/连接器配置同步仍待实现。

## 0.2.0a1 — 安全预览 Alpha

本版是产品化基础，不是生产稳定版。相比原版本，主动收紧能力范围，包含不兼容的命令变更。

### 已实现

- 从旧单文件脚本改为 `wb_account_sync` 包；Python 3.10+，运行零第三方依赖。
- 提供可安装的 wheel、源码发行包和统一命令入口。
- `doctor` / `plan` / `verify` 只读检查；计划仅在明确 `--output` 时写入客户端目录外。
- `apply` / `restore` 只允许同一客户端目录中普通未删除会话的账号归属修改。
- 显式 home、完整 UUID、当前账号快照、数据库身份/schema、逐行双向指纹验证。
- 完整计划 ID 确认；写前检查客户端退出；未知状态、WAL、journal、schema 变化保守拒绝。
- 按 home 协调写入锁，事务中逐行校验，失败回滚。
- 持久化 prepared / committed / restoring / restored journal；支持相同计划重试与保守撤销。
- 合成回归测试：只读副作用、拒绝路径、多行失败、提交前/后故障、撤销冲突和幂等。
- GitHub Actions 配置：macOS/Linux × Python 3.10/3.13，测试后构建并进行 wheel 安装冒烟检查。
- 中文 README、长期产品路线、旧版本迁移及安全边界文档。

### 停用与不支持

- 旧 `sync`、`live`、`daemon-*`、`adopt`、`backup`、`snapshots`、`revert` 明确拒绝执行。
- 不再通过 helper app 绕过系统权限；不管理真实常驻服务。
- 不迁移自动化、凭据、渠道、云记忆或跨目录数据；不提供整库备份。
- `restore` 仅撤销本工具记录的会话归属，不是旧版快照恢复。
- 不保证界面/云同步一致性，不宣称所有客户端版本兼容。

### 升级注意

更新文件不停止已运行旧 daemon；旧进程下次启动会被新入口拒绝。请先按 [迁移指南](docs/MIGRATION.md) 手动停旧服务，不要将 Alpha 静默替换到正在使用的环境。

图形界面、签名公证、完整备份、PyPI、自动更新和真实客户端验收尚未完成。参见 [路线图](docs/ROADMAP.md)。
