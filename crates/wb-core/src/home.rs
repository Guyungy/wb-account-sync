//! 客户端数据目录（home）的表示与基础扫描。
//!
//! 对应 Python 的 `Home` 与 `wb_home_bridge.py` 里那几个自由函数。
//!
//! **这一层为什么值得单独存在**：`dir_size` 算出来的 `approx_bytes` 会进
//! 计划正文，而计划正文决定 `plan_id` —— 所以目录扫描的**口径**（尤其符号
//! 链接怎么算）不是实现细节，是跨实现契约。差一个字节，plan_id 就对不上。
//!
//! 本模块**不依赖 SQLite**：数据库相关的部分（`connect` / 表计数 / 列裁剪）
//! 随 `plan` / `apply` 一起落地。

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value as Json;

use crate::error::{errf, CoreError};

/// 与 Python 侧同号，便于对账。
pub const VERSION: &str = "0.1.0";

/// 客户端数据库文件名。
pub const DB_NAME: &str = "workbuddy.db";

/// 账号快照相对 home 的路径。它是「当前登录的是谁」的**唯一权威源**。
pub const ACCOUNT_SNAPSHOT: &str = "storage/skeleton/account-snapshot.json";

/// 一个客户端的数据目录。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Home {
    /// 给人看的名字，出现在错误文案里。
    pub label: String,
    /// 机器用的短名（`wb` / `wb_ai`）。
    pub slug: String,
    pub path: PathBuf,
    /// 客户端显示名。
    pub app: String,
}

impl Home {
    pub fn new(
        label: impl Into<String>,
        slug: impl Into<String>,
        path: impl Into<PathBuf>,
        app: impl Into<String>,
    ) -> Self {
        Self {
            label: label.into(),
            slug: slug.into(),
            path: path.into(),
            app: app.into(),
        }
    }

    pub fn db_path(&self) -> PathBuf {
        self.path.join(DB_NAME)
    }

    pub fn snapshot_path(&self) -> PathBuf {
        self.path.join(ACCOUNT_SNAPSHOT)
    }

    /// 数据目录或数据库缺失时报错。
    ///
    /// 顺序与 Python 一致：先目录、后数据库。反过来的话，
    /// 目录整个不在时会报「缺少数据库」，用户会去查一个根本不存在的路径。
    pub fn require_valid(&self) -> Result<(), CoreError> {
        if !self.path.is_dir() {
            return Err(errf!(
                "[{}] 数据目录不存在：{}",
                self.label,
                self.path.display()
            ));
        }
        if !self.db_path().is_file() {
            return Err(errf!(
                "[{}] 缺少数据库：{}",
                self.label,
                self.db_path().display()
            ));
        }
        Ok(())
    }

    /// 读账号快照。私有的：调用方要的是 uid 或昵称，不是整份文档。
    fn snapshot(&self) -> Result<Json, CoreError> {
        let path = self.snapshot_path();
        if !path.is_file() {
            return Err(errf!(
                "[{}] 缺少账号快照：{}\n请先启动该客户端并完成登录，再运行本工具。",
                self.label,
                path.display()
            ));
        }
        let text = fs::read_to_string(&path).map_err(|e| {
            errf!("[{}] 无法解析账号快照：{}", self.label, e)
        })?;
        serde_json::from_str(&text).map_err(|e| {
            errf!("[{}] 无法解析账号快照：{}", self.label, e)
        })
    }

    /// 从账号快照里取 `primary.uid`。
    pub fn current_uid(&self) -> Result<String, CoreError> {
        let doc = self.snapshot()?;
        let uid = doc
            .get("primary")
            .and_then(|p| p.get("uid"))
            .and_then(Json::as_str)
            .unwrap_or("");
        if uid.is_empty() {
            return Err(errf!("[{}] 账号快照中没有 primary.uid", self.label));
        }
        Ok(uid.to_string())
    }

    /// 取账号昵称。**缺失时返回空串而不是报错** —— 昵称只是展示用，
    /// 它取不到不该让整条流程失败（`current_uid` 才是不许失败的那个）。
    pub fn nickname(&self) -> String {
        self.snapshot()
            .ok()
            .and_then(|doc| {
                doc.get("primary")
                    .and_then(|p| p.get("nickname"))
                    .and_then(Json::as_str)
                    .map(str::to_string)
            })
            .unwrap_or_default()
    }
}

/// 递归统计文件字节数。
///
/// **口径必须与 Python 的 `os.walk` 一致**（`followlinks=False`）：
///
/// - 指向**目录**的符号链接：不递归进去，也不计入大小；
/// - 指向**文件**的符号链接：按目标（`os.stat`，跟随链接）的大小计入；
/// - 坏链接：跳过。
///
/// 这三点任何一条算错，`approx_bytes` 就会跟 Python 差一点，
/// 而它进了计划正文 —— plan_id 直接对不上。
pub fn dir_size(path: &Path) -> u64 {
    if !path.is_dir() {
        return 0;
    }
    let mut total = 0u64;
    walk_sizes(path, &mut |_, size| total += size);
    total
}

/// 返回「相对路径 → 文件大小」。键是相对 `path` 的路径，用 `/` 分隔。
///
/// 用途是核验：拿两边执行前后的清单比对，判断文件树是否真的到位。
pub fn tree_manifest(path: &Path) -> BTreeMap<String, u64> {
    let mut out = BTreeMap::new();
    if !path.is_dir() {
        return out;
    }
    let base = path.to_path_buf();
    walk_sizes(path, &mut |full, size| {
        if let Ok(rel) = full.strip_prefix(&base) {
            out.insert(rel.to_string_lossy().into_owned(), size);
        }
    });
    out
}

/// 遍历目录，对每个**文件**回调一次。
///
/// 排序是为了让遍历序稳定（Python 侧 `dirs.sort()` + `sorted(files)`）。
/// 单看哈希其实不需要 —— `sort_keys` 会在编码时重排 —— 但稳定的遍历序
/// 让「同一份树两次扫描得到同一个 manifest」成立，核验才有意义。
fn walk_sizes(dir: &Path, on_file: &mut impl FnMut(&Path, u64)) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    let mut items: Vec<_> = entries.flatten().collect();
    items.sort_by_key(|e| e.file_name());

    for entry in items {
        let full = entry.path();
        // file_type() **不跟随**符号链接，正是判断「是不是链接」所需要的。
        let Ok(ft) = entry.file_type() else {
            continue;
        };

        if ft.is_symlink() {
            // 跟随一次看它指向什么：指向目录就不进（也不计），指向文件就计目标大小。
            match fs::metadata(&full) {
                Ok(meta) if meta.is_dir() => continue,
                Ok(meta) => on_file(&full, meta.len()),
                Err(_) => continue,
            }
        } else if ft.is_dir() {
            walk_sizes(&full, on_file);
        } else if let Ok(meta) = fs::metadata(&full) {
            on_file(&full, meta.len());
        }
    }
}

/// 找出含该会话正文的项目桶（**可能多于一个**）。
///
/// 同一个会话可能被多个项目桶收录，所以返回列表而不是单个值。
/// 返回已排序，保证调用方拿到的顺序稳定。
pub fn project_slug_for(home: &Home, cid: &str) -> Vec<String> {
    let base = home.path.join("projects");
    let Ok(entries) = fs::read_dir(&base) else {
        return Vec::new();
    };
    let mut slugs = Vec::new();
    for entry in entries.flatten() {
        let Ok(name) = entry.file_name().into_string() else {
            continue;
        };
        if base.join(&name).join(format!("{cid}.jsonl")).is_file() {
            slugs.push(name);
        }
    }
    slugs.sort();
    slugs
}

/// 人类可读的体积。
///
/// 复刻 Python 的 `human()`：1024 进制，`B` 不带小数（截断取整），
/// 其余保留一位小数。
///
/// **注意**：它与 `dir_size` 的口径不同 —— 这个只用于展示，不进哈希。
/// 真正进计划正文的是 `dir_size` 的原始字节数。
pub fn human(n: f64) -> String {
    let mut value = n;
    for unit in ["B", "KB", "MB", "GB"] {
        if value < 1024.0 || unit == "GB" {
            if unit == "B" {
                // Python 的 int() 向零截断；Rust 的 as 转换同样是向零截断。
                return format!("{}B", value as i64);
            }
            return format!("{value:.1}{unit}");
        }
        value /= 1024.0;
    }
    format!("{value:.1}GB")
}
