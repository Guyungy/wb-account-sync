//! 受控失败：参数错误、安全检查拒绝、状态漂移。
//!
//! 对应 Python 的 `BridgeError`。**错误文案是对外契约的一部分** ——
//! CLI 的 `--json` 输出与界面提示都直接展示它，跨实现等价性测试也会比对，
//! 所以这里的措辞要跟 Python 逐字对齐，不是随手写的调试信息。

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoreError {
    msg: String,
}

impl CoreError {
    pub fn new(msg: impl Into<String>) -> Self {
        Self { msg: msg.into() }
    }

    pub fn message(&self) -> &str {
        &self.msg
    }
}

impl fmt::Display for CoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.msg)
    }
}

impl std::error::Error for CoreError {}

/// `errf!("[%s] 缺少数据库：%s", label, path)` 那样的构造糖。
macro_rules! errf {
    ($($arg:tt)*) => {
        $crate::error::CoreError::new(format!($($arg)*))
    };
}

pub(crate) use errf;
