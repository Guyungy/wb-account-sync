# wb-account-sync

**0.2.0a1 · 安全预览版（Alpha），不是生产稳定版。**

从个人脚本走向可安装、可验证的本地迁移工具。目前只支持：**同一客户端数据目录内，离线重归属普通会话的 `sessions.user_id`**。

Offline, explicitly confirmed ownership migration for ordinary WorkBuddy sessions. Zero runtime dependencies. Not a full-account sync or backup product.

> 项目非官方，未获 WorkBuddy 或其厂商背书。只用于你本人有权管理的账户。客户端私有存储协议、界面展示和云同步行为尚未经过正式兼容认证。

## 先了解本次变化

旧版的“所有账号数据持续互通”承诺收回。新版优先保护数据，**不会自动迁移自动化、连接器密钥、渠道绑定和云端记忆缓存**。

- 已拆分为 Python 包、命令入口、安全检查、执行核心和测试。
- 原 `sync`、`live`、`daemon-*`、`adopt`、`backup`、`snapshots`、`revert` 命令已拒绝执行，退出码为 `2`。
- 保留的 `restore` 是普通会话归属撤销，不是旧版整库快照恢复。
- 安装/修改源码**不会停止已经在内存运行的旧守护进程**；旧进程下次重启执行新入口会拒绝 `live`，不会继续旧同步。升级前请阅读[旧版迁移指南](docs/MIGRATION.md)。

## 支持边界

| 本版支持 | 本版不支持 |
|---|---|
| 显式选择单一 `--home` | 新旧客户端跨目录搬家、跨设备迁移 |
| 普通、未删除、非后台自动化会话 | 自动化任务及其运行/投递数据 |
| 只修改会话所属账号 | 复制正文、附件、工作区文件 |
| 当前登录快照中的账号作为目标 | 任意猜测账号、自动取消账户隔离 |
| 校验行指纹和归属 | 保证界面可见、云同步一致、登录授权可用 |
| 撤销本次记录的归属修改 | 全库备份、完整灾难恢复、自动无损回滚 |

**运行要求：** Python 3.10+；写操作仅支持 macOS。Linux 用于合成数据测试及只读能力验证。运行零第三方依赖，源码构建需要标准 Python 构建工具。

这不是“所有 5.5.x 都兼容”：仅检查已列入白名单的会话字段、键结构和 schema 指纹。未知结构、触发器或可能引发级联副作用的键结构会拒绝操作。

## 下载安装

### 方法一：下载 wheel（推荐体验）

从本仓库的 [Releases](https://github.com/Guyungy/wb-account-sync/releases) 选择明确标为 **Pre-release** 的安全预览版本；若尚无发行版，可从 [Actions](https://github.com/Guyungy/wb-account-sync/actions) 成功构建的 `wb-account-sync-safety-preview` 附件下载。Artifacts 有保留期限，下载可能需要 GitHub 登录。

下载 `.whl` 和 `SHA256SUMS.txt`，在下载目录用 `shasum -a 256 文件名.whl` 对照校验和。校验和检测传输损坏，**不是代码签名，也不证明来源未遭篡改**。

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --no-index --no-deps ./wb_account_sync-0.2.0a1-py3-none-any.whl
wb-account-sync --version
wb-account-sync --help
```

若只下载了 wheel，无需 `--no-deps` 之外的额外依赖；Python/pip 本身仍须已安装。无 `sudo`，无 `curl | bash`，不安装常驻服务，不启动临时应用绕过权限。

### 方法二：从源码安装

```sh
git clone --branch product/safe-preview-v0.2 https://github.com/Guyungy/wb-account-sync.git
cd wb-account-sync
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
wb-account-sync --version
```

也可在已经安装 `pipx` 的情况下使用 `pipx install .`。**尚未发布 PyPI**，不要假设 `pip install wb-account-sync` 下载的是本项目。

三种入口调用相同核心：安装后的 `wb-account-sync`、源码目录内的 `python3 -m wb_account_sync`、`./wb-account-sync.sh`。源码启动器支持 `WB_PYTHON=/绝对路径/python3` 显式指定解释器。

## 使用流程：检查 → 计划 → 确认 → 核验

### 0. 独立备份并退出客户端

先通过官方导出（若可用）或你自己的备份方案保存数据。**本工具的 undo journal 不是完整备份。**

登录目标账号后，按 Command-Q 完全退出 WorkBuddy。保持离线直到执行、核验和必要的撤销结束。旧版如果装过服务，请在系统终端手动停止：

```sh
launchctl bootout gui/$(id -u)/com.workbuddy.account-sync
```

命令失败不代表停止成功；请核实服务状态，另外停止自己启动的旧 `live` 进程。不要删除 WAL/锁文件，也不要绕过权限拒绝。

### 1. 检查与计划

以下均为占位变量，请替换成自己的**完整绝对路径和完整 UUID**；不要把操作系统用户主目录当作客户端目录。

```sh
WB_HOME='<绝对客户端目录，例如你选择的 .workbuddy-ai>'
STATE_DIR='<客户端之外的独立状态目录；父目录须存在>'
SOURCE_UUID='<完整源账号UUID>'
TARGET_UUID='<当前账号快照中的完整目标UUID>'

wb-account-sync doctor --home "$WB_HOME"
wb-account-sync plan --home "$WB_HOME" --source "$SOURCE_UUID" --target "$TARGET_UUID"
```

`doctor`、`plan`、`verify` 不写客户端数据和运行状态。没有 `--output` 时，计划只输出到终端。它们不是在线快照：发现非空 WAL、活动 journal 或读取期间变化就拒绝；使用前仍须退出客户端。

需要保存时显式导出到客户端目录外；已存在的文件不会覆盖：

```sh
wb-account-sync plan --home "$WB_HOME" --source "$SOURCE_UUID" --target "$TARGET_UUID" --output plan.json
```

计划包含路径、UUID、会话 ID 和行指纹，请仅本地审阅，勿上传到 issue/公共仓库。校验和用于检测计划变化，**不是数字签名**。执行器还会核对每行内容和修改范围。

### 2. 确认执行并核验

确认目录、源/目标、变更行后，复制输出中的完整 `plan_id`；不支持通用 `--yes` 或短前缀。

```sh
PLAN_ID='<已审阅计划的完整plan_id>'
wb-account-sync apply --plan plan.json --state-dir "$STATE_DIR" --confirm "$PLAN_ID"
wb-account-sync verify --plan plan.json
```

`STATE_DIR` 应为尚不存在的新目录，或属于本工具、权限为 `0700` 的既有目录。不要选择普通个人文件夹、旧同步池或其他客户端目录。计划与 journal 文件以 `0600` 创建。

计划冻结数据库路径/inode、schema、账号快照和行指纹；漂移即拒绝。写前持久化 undo 元数据；普通会话归属更新在同一事务中完成，不对其他表执行 SQL。程序不能阻止用户在执行中重新启动客户端，请保持退出状态。

### 3. 必要时撤销

```sh
wb-account-sync restore --run-dir "$STATE_DIR/runs/$PLAN_ID" --confirm "$PLAN_ID"
```

只恢复记录中的 `user_id`。受影响行有后续内容修改、账号快照变化或数据库被替换时，拒绝覆盖。重复执行成功的 apply/restore 是幂等的；已经撤销的 run 不允许再次 apply。

提交后状态记录失败会留下 `prepared` 与已提交数据，可通过相同计划重试确认结果；混合状态需要人工审阅。不要删除运行目录强行重来。

## 命令与返回值

```text
doctor --home PATH [--json]
status --home PATH [--json]                 # doctor 的别名
plan --home PATH --source UUID --target UUID [--output FILE] [--json]
apply --plan FILE --state-dir PATH --confirm PLAN_ID [--json]
verify --plan FILE [--json]
restore --run-dir PATH --confirm PLAN_ID [--json]
--version
```

输出为 JSON；`--json` 额外将受控错误输出为 JSON。退出码：`0` 成功；`2` 参数/安全拒绝或操作失败；`3` verify 检查完成但尚未达到目标状态。成功核验**仅指本地行归属**，不代表正文/附件/界面/云端已经全部同步。

## 测试与发布

```sh
python3 -m unittest discover -s tests -v
python3 -m pip install build
python3 -m build
```

测试使用临时目录与合成会话，写操作中的进程检查以 mock 测试，不对真实账号操作。覆盖只读零副作用、计划漂移、权限拒绝、多行失败回滚、journal 中断续办、重复执行、保守撤销等；mock 通过不代表真实客户端兼容认证。

CI 配置 macOS/Linux × Python 3.10/3.13，成功后构建 wheel、源码包和 SHA-256 清单，并在新虚拟环境进行安装冒烟检查。请以每次提交的实际 CI 结果为准。

## 从预览到产品

**已实现：** 可安装 CLI、安全计划/确认流程、事务执行、持久 undo journal、保守恢复、合成故障测试、CI 构建。

**尚未实现：** 完整跨账号/跨目录迁移、自动化官方接口集成、完整备份、签名公证的 macOS 图形界面、PyPI、自动更新、真实客户端兼容矩阵和长期灰度验收。

路线：P0 安全预览 → P1 恢复与故障验收 → P2 可支持的 CLI Beta → P3 签名桌面产品 → 达到明确门槛后发布 1.0。不会用“已跑通一次”替代生产质量证明。

- [长期产品路线图与 1.0 验收](docs/ROADMAP.md)
- [安全边界及故障处理](docs/SAFETY.md)
- [从旧版迁移](docs/MIGRATION.md)
- [更新记录](CHANGELOG.md)
- [MIT 许可证](LICENSE)
