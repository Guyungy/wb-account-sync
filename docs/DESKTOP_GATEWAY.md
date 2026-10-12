# 本地桌面工作台

新增功能由 **Rust + Tauri 2 + TypeScript** 实现。双击 `.app` 打开原生窗口；账号管理、切换、同步和恢复通过 Tauri IPC 调用 Rust，不提供网页管理入口。`web/` 是桌面窗口的静态资源源码目录。

## 启动与构建

```bash
export PATH="$HOME/.cargo/bin:$PATH"
npm --prefix web ci --no-audit --no-fund
npm --prefix web run check
cd desktop
CARGO_TARGET_DIR=../target npm run build -- --bundles app,dmg
```

macOS 产物位于 `target/release/bundle/macos/wb-account-sync.app` 与 `target/release/bundle/dmg/*.dmg`。运行时无需 Python、Node 或 Rust。开发时双击 `tools/desktop.command`，它启动原生窗口。

当前本机打包目标是 Apple Silicon，未做开发者证书签名与公证。Windows / Linux 的真实客户端切换未验证。新构建不自动覆盖 `/Applications` 或用户已有应用。

macOS 状态目录：`~/Library/Application Support/io.github.guyungy.wb-account-sync/`。其他系统由 Tauri 的 `app_data_dir` 选择。

| 文件 | 用途 |
|---|---|
| `accounts.json` | 本人授权账号的登录态 |
| `gateway.json` | 网关设置与独立 API Key |
| `links.json` | 会话副本关联与基线 |
| `session-runs/` | 数据库备份、文件备份和恢复记录 |
| `login-backups/` | 切换前的认证备份 |

Unix 上凭据文件为 `0600`，状态目录为 `0700`；Windows 使用用户目录权限。备份不自动清理。

## OpenAI 兼容反向代理

桌面应用启动时监听 `127.0.0.1:8789`，关闭后停止。仅提供 `GET /healthz`、`GET /v1/models` 与 `POST /v1/chat/completions`。该端口不提供 HTML 或管理 API。Base URL 与独立 API Key 在桌面「API 网关」页获取。

支持账号轮转、每账号并发上限、会话粘性、HTTP 限流与故障冷却、过期及 401 时刷新令牌。上游强制 SSE；非流式请求在本机聚合，保留结构化工具调用、推理字段与真实用量。空流和截断返回错误。聊天请求不携带 refresh token，刷新接口才使用它；不跟随上游重定向。

先通过官方 OAuth 添加账号或导入本人已有凭证，再查询模型：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8789/v1", api_key="<桌面窗口的 API Key>")
result = client.chat.completions.create(
    model="<查询到的模型 ID>",
    messages=[{"role": "user", "content": "你好"}],
)
print(result.choices[0].message.content)
```

支持 `n=1`；未实现 Responses、图片生成或其他 OpenAI API。非公开协议和模型可用性取决于上游；当前自动验证使用本地模拟上游，不代表所有真实模型已实测。本项目不执行奖励任务、不改写用户提示词。

`gateway.json` 的 `listen`、`max_in_flight`、`cooldown_seconds`、`timeout_seconds`、`cn_base`、`ai_base`、`user_agent` 修改后重启生效。默认仅本机；需要远程自托管时可明确修改监听地址，网关仍要求 API Key，管理仍只在桌面窗口内。另有 `wb-server` 无界面推理服务供服务器部署，也不提供网页版本，默认状态目录为 `~/.wb-account-sync-server`。

## 一键切换账号

先保存当前登录态、通过官方授权添加其他账号，或导入本人已有认证 JSON；随后点账号卡片「一键切换」。默认勾选「切换时同步全部会话」：退出两个客户端、先同步到目标账号，再写目标客户端认证并重新打开。关闭该选项则只退出目标客户端、备份与切换认证，不改会话。

macOS 认证位置参考 `workbuddy-switch`：

```
~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop-ai.info
```

保留账号列表和未知根字段，原样保留 WorkBuddy 5.6 加密信封；写入和核验成功后清理对应 `.logged-out` 标记。不会把 `account-snapshot.json` 缓存当作登录执行器。写入失败尝试恢复原认证；启动失败会报告认证已写入、客户端尚未打开。

加密桌面登录态可原样切换，但网关不能直接使用加密 token，需要该账号的官方 OAuth 授权。支持导入官方认证 JSON、`workbuddy-switch` 导出数组和 `workbuddy2api` 单账号凭证。账号列表不返回上游令牌，导入成功后清空输入框。

## 全部会话同步

范围是两个本机数据目录中全部**未删除**会话，目标是用户勾选的本人个人账号。包含正文 JSONL、元信息、工具结果、子 Agent 文件、任务资产、changes、文件历史、artifact index、内容寻址 blobs 及被引用的 buddy snapshot。支持项目桶及直接位于 `projects/` 下的正文。

- 跨账号创建稳定的新 UUID，改写结构化会话 ID 和资产路径，保留原记录、原归属及用户文字。跨客户端也建立关联。
- 旧桥接留下的同 ID、相同内容副本可以加入同一关联组。重复执行不新建副本。
- 某一副本新增内容可同步回其他副本，保留目标标题。不同副本产生不同修改时报告冲突并跳过整组，其他组可以继续。
- 缺正文、删除过的关联成员、Claw 渠道会话、企业作用域、未知 schema 等情况报告跳过或拒绝。
- 不复制自动化调度表、凭据或积分消费行，不手写云端映射。云端登记交给官方客户端；本地核验不等于云端同步成功。

独立执行：「全部会话同步」→ 选择目标 → 预览 → 退出客户端 → 粘贴完整 `plan_id` → 执行。切换时勾选同步，则一键切换动作授权即时生成并执行计划。

执行前检查进程、磁盘余量、数据库及资产指纹；每 home 使用文件锁。SQLite 备份包含 WAL 有效数据，持久化恢复清单先于业务写入。结果给出 `run_id`，可在「恢复与撤销」输入。

恢复通过 SQLite backup API 回写，预先核对全部数据库、文件与备份。存在后续修改时停止。中断记录阻止继续切换或同步，需要先恢复。恢复采用整库备份，因此任何后续数据库修改都会导致保守拒绝。

## 验证与来源

```bash
cargo test --workspace --locked
npm --prefix web run check
npm --prefix web run build
CARGO_TARGET_DIR=target cargo check --manifest-path desktop/src-tauri/Cargo.toml --locked
```

新测试使用临时 home 和模拟上游，覆盖登录态信封、权限、令牌刷新、轮转、SSE 分片、工具调用、截断、完整资产复制、幂等、增量、分歧、漂移拒绝及恢复。不会写真实账号数据或消耗真实额度。

参考 [workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel) 的协议与网关设计，以及 [workbuddy-switch](https://github.com/changexbc/workbuddy-switch) 的认证格式、退出标记与关联副本设计。采用独立 Rust 实现，未集成整个参考项目；MIT 声明保存在 `THIRD_PARTY_NOTICES.md`。

### 桌面操作便利性

- 账号页可按昵称或 UID 搜索，并按客户端筛选。切换前显示目标账号及是否同时同步会话，确认后备份登录态并重启。
- 会话页支持全选与清空选择。预览后勾选「我已检查计划」即可执行，无需复制计划编号；后台仍验证计划标识、客户端退出状态与数据变化。
- 执行期间锁定同步目标；操作显示正在扫描、备份或切换的状态，不显示虚构百分比。
- 本机保存最近 8 个同步运行编号，恢复区域可选择编号后执行恢复。这里只保存编号，不保存账号凭据；记录在当前桌面应用内产生的同步。
- 网关地址、API Key 与接入示例可分别复制。示例保持占位密钥，复制示例不会带出实际凭据。

### 客户端会话结构兼容

跨客户端同步允许源端多出的空字段、`transport=local` 和界面未读标记（0/1）；原会话不变，旧客户端的副本不带未读标记。目标端新增字段使用其数据库默认值或空值。每个副本独立记录基线，避免默认值差异被误判为编辑。若分组、可见性等缺失字段有有效信息，或目标必填字段无默认值，预览停止并报告具体字段，不修改数据库结构。
