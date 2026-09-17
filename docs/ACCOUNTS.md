# 账号与用量面板

> 状态：**只读**功能。它只开 sqlite 的 `mode=ro` 并读文件，不写任何客户端数据，
> 也不需要退出客户端——跟"跨 App 打通"那套写操作是两回事。

界面顶部「账号与用量」卡片的数据来源就是这里；命令行等价物是
`python3 tools/acct_probe.py`（加 `--json` 出机器可读格式，`--no-logs` 跳过日志采样）。

## 在哪里能看到

面板跟"跨 App 打通"共用同一个界面，三种形态都能看到：

| 形态 | 怎么进 | 备注 |
|---|---|---|
| 源码版 | `python3 tools/wb_ui.py`；macOS 可双击 `tools/ui.command` | 立刻可用，改完代码不用重新打包 |
| **桌面应用** | 双击 `wb-account-sync.app`（Windows 为 `.exe`） | 面板随应用一起打包，免装 Python |
| WorkBuddy 官方客户端 | — | 看不到。客户端只显示**当前**账号，没有历史账号、累计消耗与趋势——这正是本面板要补的信息 |

面板固定在页面**最顶部**，不受下面"跨 App 打通"流程的影响；它是只读的，
不需要先退出两个客户端。

> 桌面应用是**打包快照**：面板里新增的功能只有重新打包后才会出现在 `.app` / `.exe` 里。
> 想立刻看到最新数据，用源码版（第一行）。

## 能读到什么

| 项目 | 来源 | 说明 |
|---|---|---|
| **当前登录账号** | `storage/skeleton/account-snapshot.json` → `primary` | 唯一权威源。客户端启动后约 30 秒刷新 |
| 昵称 / 类型 / 版本 / Pro / 企业 ID | 同上 | 只覆盖**当前**那一账号 |
| **本机出现过的全部账号** | 见下方"账号从哪来" | 含已经切走的旧账号 |
| 每账号会话数、自动化数、最后活动 | `workbuddy.db` 的 `sessions` / `automations` | 按 `user_id` 归属 |
| **累计积分消耗** | `workbuddy.db` 的 `session_usage.credit_json` | 按 session 挂账号 |
| 按天消耗趋势 | 同上，按 `updated_at` 落到本地日 | |
| 本机资产分布 | `memory/`、`connectors/`、`storage/`、`settings.json` | 标成表格里的标签 |

## 读不到什么：剩余积分余额

**本机磁盘不缓存余额。** 唯一的来源是实时接口：

```
POST https://copilot.tencent.com/billing/meter/get-user-resource-summary
     Authorization: Bearer <accessToken>
     X-User-Id: <uid>
```

返回里 `Packages[].CycleRemainCapacity` 是剩余额度、`IsPaidUser` 是否付费。
但这个 `accessToken` **只活在 Electron 主进程内存里**：

- `~/.workbuddy/auth/` 本机不存在，没有 `credentials.json`
- macOS 钥匙串里没有 WorkBuddy / CodeBuddy 条目
- localStorage / LevelDB / `local_storage/*.info`（base64 + gzip 全解压过）都没有

所以面板给的是**已消耗**，不是剩余。要知道还剩多少，只能打开客户端看账户页。

## 账号从哪来

没有任何单一文件记录"本机用过哪些账号"，面板是把下面几处并起来：

| 来源 | 提供 |
|---|---|
| `account-snapshot.json` | 当前账号的完整属性 |
| `sessions` / `automations` 表 | uid + 会话/自动化计数 |
| `session_usage` 表 | uid + 积分消耗 |
| `memory/<uid>_memory.md` | 该账号有长期记忆 |
| `connectors/<uid>/` | 该账号配过连接器 |
| `storage/user-<uid>-personal/` | 该账号有个人存储 |
| `settings.json` → `claw.users` | uid + 启用的渠道 |
| `logs/` 采样 | 历史账号的昵称与版本（尽力而为） |

## 为什么历史账号常常没有昵称

客户端只给**当前**账号写 `account-snapshot.json`，切换走后本机就不再留名字，
只剩 uid。唯一的补救是日志：渲染进程会把 account 对象整个 URL 编码后打进日志
（形如 `%2522uid%2522%253A%2522...`），解码两遍就能还原 `nickname` / `editionType`。

但这是**尽力而为**，覆盖不全：

- 日志按 mtime 分层采样（每层最多 250 个文件、单文件读头尾各 512 KB、总预算 120 MB），
  文件太大或命中位置在中间就会漏
- 有些日志只打了昵称没打 uid，无法归属，只能放弃
- 超过 30 天的日志不看

扫不到的账号在表里显示 `—`，用 uid 前 8 位区分。所有日志采样都是一次性的，
约 1 秒，结果缓存 15 秒（点「重新读取」可强制刷新）。

## 跨客户端去重口径

这是最容易读错的地方。打通工具会把同一个会话**复制**进另一个 home，顺带把
`user_id` 改写成目标账号。于是同一个 `session_id` 在两个库里各有一条，
用户归属还不一样——本机实测 49 条里有 45 条是这种重叠。

所以面板给两个口径：

| 口径 | 含义 |
|---|---|
| 各客户端分别列出 | 这个客户端里能看到什么，用于判断"同步到位没有" |
| **整机（跨客户端去重）** | 按 `session_id` 去重后的真实消耗，用于回答"到底用了多少" |

两个客户端直接相加会把同一笔消耗算两遍。本机实测：相加 77,785 → 去重后 42,625。

去重时归属取先出现的那个（`WorkBuddy` 侧优先），所以整机表里某个账号的数字
可能比它在单个客户端里的数字小——那是同一个会话被算到主客户端账号头上了。

## 验证

`tools/acct_probe.py` 是这套逻辑的独立入口，可以直接对着它核对面板上的数字：

```bash
python3 tools/acct_probe.py            # 人读摘要，含近 14 天柱子
python3 tools/acct_probe.py --json     # 面板用的同一份数据
python3 tools/acct_probe.py --no-logs  # 跳过日志采样，快
```
