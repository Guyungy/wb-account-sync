//! `wb-core` —— WorkBuddy 跨 App 数据目录打通的迁移引擎。
//!
//! 唯一真相是 `legacy-python`：这里做的是**等价复刻**，不是重新设计。
//! 验收标准只有一条 —— `plan_id` 与 Python 逐字节一致。
//!
//! 模块按依赖从轻到重分期落地（见 `docs/RUST_MIGRATION.md`）：
//! `pyjson` → `home` → `plan` → `apply` → `backup` → `verify` → `restore` → `memory`。

pub mod error;
pub mod home;
pub mod plan;
pub mod pyjson;
