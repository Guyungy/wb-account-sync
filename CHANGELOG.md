# 更新记录

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
