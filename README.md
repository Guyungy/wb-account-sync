# wb-account-sync

> WorkBuddy 跨账号数据保留 / 持续同步 / 备份 / 过户工具
> Keep **all** your task history, automations, memory and connector logins when you switch accounts.
>
> 让同一台机器上的所有 WorkBuddy 账号共享同一份完整数据 —— 装一次，之后切账号再也不丢东西。

![platform](https://img.shields.io/badge/platform-macOS-black)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![deps](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen)
![license](https://img.shields.io/badge/license-MIT-green)

---

## 问题是什么

WorkBuddy 桌面端**同一时间只能登录一个账号**，而账号级数据是按 `user_id` 分开存的：

- 任务与对话历史存在 `sessions.user_id`
- 自动化归 `automations.owner_user_id`
- 长期记忆在 `memory/<uid>_memory.md`
- 连接器授权在 `connectors/<uid>/`
- 账号个人存储、渠道绑定也各账号一份

于是切换账号 = 界面变空。老账号那几十条任务、自动化和记忆都还在磁盘上，只是**看不见、也带不过去**。

本工具就是解决这件事，并提供三种力度：

| 力度 | 命令 | 适用 |
|---|---|---|
| **持续同步**（推荐） | `daemon-install` | 装一次，之后账号一变就自动把全部账号数据带过来 |
| 一次性同步 | `sync` | 手动跑一轮，可 `--dry-run` 先看 |
| 备份 / 过户 / 回滚 | `backup` `adopt` `revert` `restore` | 需要单次搬运或反悔时 |

## 效果

| | 切换账号前 | 装完持续同步后 |
|---|---|---|
| 任务 / 对话历史 | 只剩当前账号自己那几条 | **全部账号的历史都在**（实测 62 条） |
| 自动化 | 归原账号，新账号看不到 | 全部归当前账号并生效 |
| 长期记忆 | 各账号一份，互不相通 | 各账号记忆块合并后共享 |
| 连接器授权 | 各账号独立，要重登 | 凭据随账号整包带过来 |
| 项目记录 / 工作区列表 | 本来就共享（设备级） | 不变 |

## 原理：统一数据池 + 自动跟随当前账号

```
              ┌────────────────────────────────┐
   账号 A ────┤                                │
   账号 B ────┤   统一数据池（只增不删）        │
   账号 C ────┤   ~/.workbuddy-account-sync/   │
              │      pool/                     │
              └────────────────────────────────┘
                 ①吸 pull         ②回 push
                 ▲                     │
                 │                     ▼
        各账号目录 / 数据库行     「当前登录账号」
```

- **① 吸（pull）**：把每个账号的账号级资产并入数据池。记忆按行去重取并集，连接器状态项并集，账号个人存储目录树并集 —— **只增不删，永不丢数据**。
- **② 回（push）**：把池子内容回写到「当前登录账号」。数据库行做 `UPDATE` 重挂，文件做 upsert。
- 当前账号的唯一权威来源是 `~/.workbuddy/storage/skeleton/account-snapshot.json` 里的 `primary.uid`（客户端登录 / 切换时会重写它）。

结果：**无论你登录哪个账号，看到的都是同一份完整数据。**

### 三层数据边界

| 层级 | 内容 | 处理方式 |
|---|---|---|
| **账号级** | 任务/对话、自动化、长期记忆、连接器授权、账号个人存储、渠道绑定 | 本工具负责同步 |
| **设备级** | 工作区列表（`workspaces` 表无 uid）、`skills/`、`mcp.json`、`models.json`、插件设置 | 天然全账号共享，**不需要也不应该动** |
| **工作区级** | 项目文件、`<workDir>/.workbuddy/memory/` | 跟目录走，不跨工作区 / 设备 |

会话内容资产（`tasks/` `traces/` `blobs/` `changes-*/` `file-history/` `artifact-index/`）
是**设备级**、按 session id 命名、不含 `user_id` —— 重挂 `user_id` 后自然可见，无需搬运。

## 快速开始

**环境要求**：macOS + WorkBuddy 桌面端；Python 3.9+（只用标准库，零依赖）。

```bash
git clone https://github.com/Guyungy/wb-account-sync.git
cd wb-account-sync
chmod +x wb-account-sync.sh
```

```bash
# 0. 看现状（只读，随时可跑）
./wb-account-sync.sh status

# 1. 留个回滚点
./wb-account-sync.sh backup --slim --label before-live-sync

# 2. 先演练，看清会改什么
./wb-account-sync.sh sync --dry-run

# 3. 开启持续同步（会先跑首轮同步，再装 launchd 常驻服务）
./wb-account-sync.sh daemon-install
```

装完 **重启一次 WorkBuddy**，任务列表就会出现全部历史。

## 持续同步

```bash
./wb-account-sync.sh daemon-install     # 开启（开机自启 + 常驻）
./wb-account-sync.sh daemon-status      # 状态 / 数据池 / 上次同步
./wb-account-sync.sh daemon-log -n 50   # 日志
./wb-account-sync.sh daemon-uninstall   # 关闭（数据池与日志保留）
```

**三路触发，互不依赖**：

| 时机 | 说明 |
|---|---|
| 检测到当前账号变化 | 登录 / 切换账号后约 10 秒内同步（连续 2 次观测确认，避免读到中间态） |
| 检测到客户端刚退出 | 最干净的窗口，立刻做一轮 |
| 每 300 秒兜底校准 | 捕捉运行期间新产生的跨账号数据 |

**可调参数**：`daemon-install --interval 5 --reconcile-every 300`

**与 `adopt` / `restore` 的关键区别**：持续同步**不做任何删除**，只用行级 `UPDATE` 与 upsert，
配合 `PRAGMA busy_timeout`，因此**可以在 WorkBuddy 运行时使用**。
`adopt` / `restore` / `revert` 是破坏性操作，仍要求先 ⌘Q 退出客户端。

## 命令参考

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `status` | 列出账号与资产统计、数据分层、快照 | — |
| `sync` | 执行一轮全量同步 | `--to <uid\|current>` `--dry-run` |
| `live` | 前台常驻进程（调试用） | `--interval` `--settle` `--reconcile-every` `--dry-run` |
| `daemon-install` | 安装 launchd 后台服务 | `--interval` `--reconcile-every` `--dry-run` `--no-first-sync` |
| `daemon-uninstall` | 卸载后台服务 | — |
| `daemon-status` | 查看服务与数据池状态 | — |
| `daemon-log` | 查看同步日志 | `-n <行数>` |
| `backup` | 生成全量快照 | `--label` `--slim` `--out` |
| `snapshots` | 列出快照 | `--out` |
| `restore` | 从快照恢复 | `--yes` `--prune` `--prune-accounts` `--clean-migrated` `--overwrite-db` `--force` |
| `adopt` | 把源账号资产过户给目标账号 | `--from <uid\|all>` `--to <uid\|current>` `--yes` `--force` |
| `revert` | 撤销最近一次 adopt | `--snapshot` `--yes` `--force` |

`<uid>` 支持前缀匹配，例如 `a1b2c3d4` 代表一大串 UUID 里的某个账号。

### 一次性同步

```bash
./wb-account-sync.sh sync --dry-run          # 先看会改什么
./wb-account-sync.sh sync                    # 同步到当前登录账号
./wb-account-sync.sh sync --to a1b2c3d4      # 指定目标账号
```

> 后台服务在跑时会持有单实例锁，此时手动 `sync` 会被拒绝（服务已在做同样的事）。
> 需要手动跑请先 `daemon-uninstall`。

### 一次性过户（不装服务）

```bash
./wb-account-sync.sh backup --label before-switch
./wb-account-sync.sh adopt --from all --to current     # 演练，看清变更清单
# ⌘Q 退出 WorkBuddy → 打开系统终端 → 真正执行
./wb-account-sync.sh adopt --from all --to current --yes
./wb-account-sync.sh revert --yes                       # 反悔
```

## 数据分层：具体位置

给想自己排查 / 扩展的人：

| 资产 | 位置 | 备注 |
|---|---|---|
| 任务 / 对话历史 | `~/.workbuddy/workbuddy.db` → `sessions.user_id` | 项目记录在 `sessions.cwd` / `project_id` |
| 自动化 | 同库 `automations.owner_user_id`（+ `owner_status`） | 待投递在 `automation_delivery_outbox.owner_user_id` |
| 自动化运行记录 | `automation_runs`（PK `thread_id`）、`automation_runtime_state` | 靠 `automation_id` 间接归属，无需单独处理 |
| 长期记忆 | `~/.workbuddy/memory/<uid>_memory.md` | 文件尾部有 `RAW_JSON` 块，**`uid` 与 `memoryBlock` 两处都要写** |
| 连接器授权 | `~/.workbuddy/connectors/<uid>/` | `.master.key` + `.credentials.v3.json` + `connector-states.json` |
| 账号个人存储 | `~/.workbuddy/storage/user-<uid>[-personal]/` | |
| 渠道绑定 | `~/.workbuddy/settings.json` → `claw.users.<uid>` | |
| 当前账号 | `~/.workbuddy/storage/skeleton/account-snapshot.json` → `primary.uid` | 唯一权威来源 |
| 工作区列表 | 同库 `workspaces(path, last_opened_at)` | 无 uid，设备级 |
| 云端同步映射 | `~/.workbuddy/edge-sync-mapping-v4.db` | `msg_channel` 形如 `convmsg:<uid>`，说明会话消息按账号上传云端 |

`connectors/default/` 和 `connectors/skills/` **不是账号**，不要当成 uid 处理。
数据库里也**没有 users 表**，账号只能从「数据里出现过的 uid」推导。

## 安全设计

- **零删除**：同步链路任何一步都不删数据；账号级文件只「有则跳过、缺则补齐」。
- **限定范围**：数据库 `UPDATE` 强制 `user_id IN (本机已发现的账号)`，不会误抓陌生 uid 的行。
- **不整库覆盖**：只用行级 `UPDATE`，不 copy 整个 db 文件，客户端运行中也安全。
- **单实例锁**：`~/.workbuddy-account-sync/.lock`（`fcntl.flock`），避免并发写入。
- **写前必备份**：`restore` 生成 `pre-restore` 快照，`adopt` 生成 `pre-adopt-<src>-to-<dst>` 快照。
- **默认演练**：`adopt` 不加 `--yes` 只打印变更清单。
- **行级 diff 恢复**：`restore` 默认按主键逐行回写，不整库覆盖，保住快照之后的新数据。
- **不删源数据**：`adopt` 把源目录改名为 `.migrated-*` 中转保留，确认后再手动清理。
- **主密钥有备份**：整包接过连接器凭据时，原 `.master.key` 另存为 `.master.key.before-sync-<时间戳>`。

## ⚠️ 风险与免责

1. **这是非官方工具**。它读写 WorkBuddy 客户端的私有本地数据，与腾讯 / WorkBuddy 官方无关，未获其授权或背书。
2. **客户端升级后可能失效**。本工具依赖内部表结构与目录布局（见上表），WorkBuddy 更新后这些可能变化。使用前先 `backup`。
3. **所有账号将变成等价身份**。持续同步的本意就是让各账号共享同一份数据，因此**账号间的隔离被取消**：连接器授权、渠道绑定、长期记忆全部互通。不适合把不同账号用于互相隔离的场景。
4. **仅 macOS**。后台服务用 `launchd` 的 LaunchAgent 实现。
5. **请在写入前备份**。任何自动化工具都不该在没有回滚点的情况下操作个人数据。

## 常见问题

**同步完了，但任务列表还是空的？**
客户端对列表有内存缓存。**重启一次 WorkBuddy** 即可看到全部历史。

**装服务后为什么手动 `sync` 被拒绝？**
单实例锁。后台服务已经在做同样的事，先 `daemon-uninstall` 再手动跑。

**`restore` / `adopt` 报「检测到 WorkBuddy 正在运行」？**
这两个是破坏性操作，要求先 ⌘Q 完全退出客户端，并用系统终端（Terminal.app / iTerm）运行。
另外注意：WorkBuddy 的 macOS 主进程可执行文件叫 `Electron`（`/Applications/WorkBuddy.app/Contents/MacOS/Electron`），
不是 `WorkBuddy`，所以本工具是按「应用包内、非 Frameworks 目录下的可执行文件」识别进程的。

**会不会把云端数据也改了？**
不会。本工具只动本机 SQLite 与本地文件，客户端的云端同步由它自己负责。

**要彻底关掉？**
`daemon-uninstall`，然后按需 `restore` 回滚到同步前的快照。

## 回滚

```bash
./wb-account-sync.sh snapshots                        # 列出快照
# ⌘Q 退出 WorkBuddy
./wb-account-sync.sh restore <快照名|latest> --yes
```

快照默认落在 `~/.workbuddy-account-snapshots/<时间戳>-<标签>/`：

```
manifest.json          快照元信息（时间、账号、清单、slim 标记）
workbuddy.db           SQLite 一致性副本（backup API，对 WAL 安全）
accounts/              账号级资产（memory / connectors / storage）
settings.json
payload/               会话内容资产（--slim 时无此项）
adopt-ledger.json      仅 pre-adopt 快照里有
```

## License

[MIT](LICENSE)
