# 更新记录

## 未发布 — 跨 App 数据目录打通（实验）

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

## 未发布 — 共享需求预览与隔离演练

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
