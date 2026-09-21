//! 构建迁移计划 —— `plan_id` 的产生地。
//!
//! 对应 Python 的 `collect_rows` / `build_direction_entries` / `build_plan`。
//!
//! ## 这里最要紧的一件事
//!
//! `plan_id` 是**内容指纹**：对规范化后的计划正文取 SHA-256。正文里**不含**
//! 时间戳、不含行数据（`rows`），只含这几样：
//!
//! ```text
//! version, home_a, home_b, uid_a, uid_b, options,
//! source_fingerprints, entries_fingerprint
//! ```
//!
//! 所以跨实现等价的抓手非常明确：只要这几样的规范化编码一致，`plan_id` 就一致。
//! 也因此 `created_at` / `rows` / `summary` 这些**可以**有实现差异，而
//! `entries` 的顺序与内容、指纹的算法**不可以**。
//!
//! ## 两种 JSON 表示，别混
//!
//! 同一份数据有两处用途，序列化规则不同：
//!
//! | 用途 | 规则 | 用什么 |
//! |---|---|---|
//! | 写进计划文件 / `--json` 输出 | 保序（可读、可 diff） | [`PyValue::Ordered`] |
//! | 算指纹 | `sort_keys=True` | [`PyValue::Map`]，或先 `canonicalize` |
//!
//! 用错不会有编译错误，只会让 `plan_id` 静默对不上 —— 所以每处都标了用的是哪种。

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use rusqlite::types::Value as SqlValue;
use rusqlite::Connection;

use crate::error::{errf, CoreError};
use crate::home::{dir_size, human, project_slug_for, Home, VERSION};
use crate::pyjson::{self, PyMap, PyValue};

// --------------------------------------------------------------------------
// 迁移范围常量 —— 与 Python 逐项对应，改一处就要两边一起改
// --------------------------------------------------------------------------

/// 按会话 id 归档的内容资产。第二项表示「受 `include_changes` 控制」。
const PER_SESSION_TREES: &[(&str, bool)] = &[
    ("tasks/{cid}", false),
    ("changes-detail/{cid}", true),
    ("changes-index/{cid}", true),
    ("file-history/{cid}", true),
];

const PER_SESSION_FILES: &[&str] = &["artifact-index/{cid}.json"];
const PROJECT_SESSION_SUFFIXES: &[&str] = &[".jsonl", ".meta.json", ".file-rollback.ndjson"];

/// 跨 home 直接并集（内容寻址，同名即同内容）。
const BASE_UNION_TREES: &[&str] = &["blobs"];

/// 可选并集：目录名 → 开关名。
const OPTIONAL_UNION_TREES: &[(&str, &str)] = &[
    ("skills", "include_skills"),
    ("plugins/cache", "include_plugins"),
];

/// 连接器只并状态与 mcp 配置，**绝不搬 `.master.key` / 凭据**。
const CONNECTOR_SHARED_FILES: &[&str] = &["connector-states.json", "mcp.json"];

/// 迁移范围开关。
///
/// 默认值不是"全开"也不是"全关"，而是照 Python 的 argparse 默认值：
/// 会话正文 / 技能 / claw / 记忆默认带上，插件 / 自动化 / 账号存储 / 连接器
/// 默认不带（它们要么体积大，要么涉及授权，不该在"点一下就走"的默认路径里）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanOptions {
    pub include_changes: bool,
    pub include_skills: bool,
    pub include_plugins: bool,
    pub include_automations: bool,
    pub include_storage: bool,
    pub include_connectors: bool,
    pub include_claw: bool,
    pub include_memory: bool,
    pub overwrite_assets: bool,
}

impl Default for PlanOptions {
    fn default() -> Self {
        Self {
            include_changes: true,
            include_skills: true,
            include_plugins: false,
            include_automations: false,
            include_storage: false,
            include_connectors: false,
            include_claw: true,
            include_memory: true,
            overwrite_assets: false,
        }
    }
}

impl PlanOptions {
    /// 键序与 Python `build_plan` 里那个 dict 字面量一致。
    fn pairs(&self) -> [(&'static str, bool); 9] {
        [
            ("include_changes", self.include_changes),
            ("include_skills", self.include_skills),
            ("include_plugins", self.include_plugins),
            ("include_automations", self.include_automations),
            ("include_storage", self.include_storage),
            ("include_connectors", self.include_connectors),
            ("include_claw", self.include_claw),
            ("include_memory", self.include_memory),
            ("overwrite_assets", self.overwrite_assets),
        ]
    }

    pub fn get(&self, flag: &str) -> bool {
        self.pairs()
            .iter()
            .find(|(k, _)| *k == flag)
            .map(|(_, v)| *v)
            .unwrap_or(false)
    }

    /// 进指纹用：按键排序。
    fn to_map(&self) -> PyValue {
        PyValue::Map(
            self.pairs()
                .iter()
                .map(|(k, v)| (k.to_string(), PyValue::Bool(*v)))
                .collect(),
        )
    }

    /// 进计划文件用：保序。
    fn to_ordered(&self) -> PyValue {
        PyValue::Ordered(
            self.pairs()
                .iter()
                .map(|(k, v)| (k.to_string(), PyValue::Bool(*v)))
                .collect(),
        )
    }
}

// --------------------------------------------------------------------------
// 计划条目
// --------------------------------------------------------------------------

/// 一条要执行的动作。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanEntry {
    /// `file` 或 `tree`。
    pub kind: String,
    pub src: String,
    pub dst: String,
    /// `copy_if_missing` / `copy_tree_if_missing` / `merge_tree`。
    pub mode: String,
    pub note: String,
}

impl PlanEntry {
    fn file(src: String, dst: String, note: &str) -> Self {
        Self { kind: "file".into(), src, dst, mode: "copy_if_missing".into(), note: note.into() }
    }

    fn tree(src: String, dst: String, note: &str) -> Self {
        Self {
            kind: "tree".into(),
            src,
            dst,
            mode: "copy_tree_if_missing".into(),
            note: note.into(),
        }
    }

    /// Python 的 `as_dict()`：键序 `kind, mode, src, dst, [note]`。
    ///
    /// `note` 为空时**整个键不出现** —— 空串与"没有这个键"在哈希里是两回事。
    pub fn as_value(&self) -> PyValue {
        let mut pairs = vec![
            ("kind".to_string(), PyValue::Str(self.kind.clone())),
            ("mode".to_string(), PyValue::Str(self.mode.clone())),
            ("src".to_string(), PyValue::Str(self.src.clone())),
            ("dst".to_string(), PyValue::Str(self.dst.clone())),
        ];
        if !self.note.is_empty() {
            pairs.push(("note".to_string(), PyValue::Str(self.note.clone())));
        }
        PyValue::Ordered(pairs)
    }
}

// --------------------------------------------------------------------------
// 计划
// --------------------------------------------------------------------------

/// 一个方向的统计。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DirectionStats {
    pub sessions: u64,
    pub missing_assets: u64,
    pub cwd_invalid: u64,
}

/// `collect_rows` 的产物。
#[derive(Debug, Clone, Default)]
pub struct CollectedRows {
    /// 表名 → 行。**保序**：与 Python 里那个 dict 的插入序一致。
    pub tables: Vec<(String, Vec<PyValue>)>,
    /// 跳过计数，例如 `[("sessions", 12)]`。
    pub skipped: Vec<(String, u64)>,
    /// 源侧指纹，例如 `[("sessions_all", "…")]`。
    pub fingerprints: Vec<(String, String)>,
}

#[derive(Debug, Clone, Default)]
pub struct Plan {
    pub version: String,
    pub home_a: String,
    pub home_b: String,
    pub uid_a: String,
    pub uid_b: String,
    pub created_at: String,
    pub options: PlanOptions,
    /// 方向 → 表 → 行。
    pub rows: Vec<(String, Vec<(String, Vec<PyValue>)>)>,
    pub skipped: Vec<(String, Vec<(String, u64)>)>,
    pub fingerprints: Vec<(String, Vec<(String, String)>)>,
    pub entries: Vec<PlanEntry>,
    /// `[("a2b", …), ("b2a", …), ("totals", …)]`，保序。
    pub summary: Vec<(String, PyValue)>,
    pub plan_id: String,
}

impl Plan {
    /// 计划正文 —— `plan_id` 就是对它的规范化编码取哈希。
    ///
    /// **刻意不含 `created_at`**：否则同一份数据在不同时刻会得到不同指纹，
    /// 「先退客户端再建计划、执行前复核指纹」这套防漂移机制立刻失效。
    pub fn body(&self) -> PyValue {
        let entries: Vec<PyValue> = self
            .entries
            .iter()
            .map(|e| pyjson::canonicalize(&e.as_value()))
            .collect();

        PyMap::new()
            .set("version", self.version.clone())
            .set("home_a", self.home_a.clone())
            .set("home_b", self.home_b.clone())
            .set("uid_a", self.uid_a.clone())
            .set("uid_b", self.uid_b.clone())
            .set("options", self.options.to_map())
            .set("source_fingerprints", fingerprints_value(&self.fingerprints))
            .set(
                "entries_fingerprint",
                pyjson::hash_json(&PyValue::Array(entries)),
            )
            .build()
    }

    /// 算 `plan_id`。
    pub fn finalize(&mut self) {
        self.plan_id = pyjson::hash_json(&self.body());
    }

    /// 完整计划文档（写进 `plan.json` 的那个）。
    ///
    /// 保序输出，与 Python 的 `json.dump(doc, indent=2)` 键序一致。
    pub fn as_dict(&self) -> PyValue {
        PyValue::Ordered(vec![
            ("plan_id".into(), PyValue::Str(self.plan_id.clone())),
            ("version".into(), PyValue::Str(self.version.clone())),
            ("created_at".into(), PyValue::Str(self.created_at.clone())),
            ("homes".into(), self.homes_value()),
            ("options".into(), self.options.to_ordered()),
            (
                "summary".into(),
                PyValue::Ordered(self.summary.clone()),
            ),
            ("source_fingerprints".into(), fingerprints_value(&self.fingerprints)),
            ("rows".into(), rows_value(&self.rows)),
            (
                "skipped".into(),
                PyValue::Ordered(
                    self.skipped
                        .iter()
                        .map(|(side, counts)| {
                            (
                                side.clone(),
                                PyValue::Ordered(
                                    counts
                                        .iter()
                                        .map(|(k, v)| (k.clone(), PyValue::Int(*v as i64)))
                                        .collect(),
                                ),
                            )
                        })
                        .collect(),
                ),
            ),
            (
                "entries".into(),
                PyValue::Array(self.entries.iter().map(|e| e.as_value()).collect()),
            ),
        ])
    }

    fn homes_value(&self) -> PyValue {
        // Python 里 label 与 path 同值 —— 界面上显示 label，引擎用 path。
        let side = |label: &str, uid: &str| {
            PyValue::Ordered(vec![
                ("label".into(), PyValue::Str(label.to_string())),
                ("path".into(), PyValue::Str(label.to_string())),
                ("uid".into(), PyValue::Str(uid.to_string())),
            ])
        };
        PyValue::Ordered(vec![
            ("a".into(), side(&self.home_a, &self.uid_a)),
            ("b".into(), side(&self.home_b, &self.uid_b)),
        ])
    }

    /// 取某个方向某张表的行（供 apply 使用）。
    pub fn table(&self, side: &str, table: &str) -> Option<&Vec<PyValue>> {
        self.rows
            .iter()
            .find(|(s, _)| s == side)?
            .1
            .iter()
            .find(|(t, _)| t == table)
            .map(|(_, rows)| rows)
    }
}

/// 指纹集合 → 值。**进哈希**，所以用 `Map`（排序）。
fn fingerprints_value(fps: &[(String, Vec<(String, String)>)]) -> PyValue {
    PyValue::Map(
        fps.iter()
            .map(|(side, items)| {
                (
                    side.clone(),
                    PyValue::Map(
                        items
                            .iter()
                            .map(|(k, v)| (k.clone(), PyValue::Str(v.clone())))
                            .collect(),
                    ),
                )
            })
            .collect(),
    )
}

/// 行数据 → 值。**进文件**，保序。
fn rows_value(rows: &[(String, Vec<(String, Vec<PyValue>)>)]) -> PyValue {
    PyValue::Ordered(
        rows.iter()
            .map(|(side, tables)| {
                (
                    side.clone(),
                    PyValue::Ordered(
                        tables
                            .iter()
                            .map(|(t, rs)| (t.clone(), PyValue::Array(rs.clone())))
                            .collect(),
                    ),
                )
            })
            .collect(),
    )
}

// --------------------------------------------------------------------------
// SQLite
// --------------------------------------------------------------------------

fn connect(home: &Home) -> Result<Connection, CoreError> {
    let con = Connection::open(home.db_path())
        .map_err(|e| errf!("[{}] 无法打开数据库：{}", home.label, e))?;
    // 单连接语义靠调用方保证；这里只设忙等，避免客户端在写时立刻 SQLITE_BUSY。
    con.busy_timeout(Duration::from_millis(60_000))
        .map_err(|e| errf!("[{}] 无法设置 busy_timeout：{}", home.label, e))?;
    Ok(con)
}

fn sql_to_py(v: SqlValue) -> PyValue {
    match v {
        SqlValue::Null => PyValue::Null,
        SqlValue::Integer(i) => PyValue::Int(i),
        SqlValue::Real(f) => PyValue::Float(f),
        SqlValue::Text(s) => PyValue::Str(s),
        // CPython 遇到 bytes 会抛 TypeError（说明真实数据里没有非空 BLOB）。
        // 这里退化成字符串而不是报错 —— 不因一个意外列让整份计划生成失败。
        SqlValue::Blob(b) => PyValue::Str(String::from_utf8_lossy(&b).into_owned()),
    }
}

/// 执行一趟无参查询，返回每行的保序 map（键序 = 列顺序，与 `SELECT *` 一致）。
fn query(con: &Connection, sql: &str) -> Result<Vec<PyValue>, CoreError> {
    let mut stmt = con
        .prepare(sql)
        .map_err(|e| errf!("SQL 准备失败（{sql}）：{e}"))?;
    let cols: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
    let mut rows = stmt
        .query([])
        .map_err(|e| errf!("SQL 执行失败（{sql}）：{e}"))?;

    let mut out = Vec::new();
    while let Some(row) = rows
        .next()
        .map_err(|e| errf!("读取行失败（{sql}）：{e}"))?
    {
        let mut pairs = Vec::with_capacity(cols.len());
        for (i, name) in cols.iter().enumerate() {
            let v: SqlValue = row
                .get(i)
                .map_err(|e| errf!("读取列 {name} 失败：{e}"))?;
            pairs.push((name.clone(), sql_to_py(v)));
        }
        out.push(PyValue::Ordered(pairs));
    }
    Ok(out)
}

/// `PRAGMA table_info` 的列名。表不存在时返回空 —— 调用方据此裁剪。
fn target_columns(con: &Connection, table: &str) -> Vec<String> {
    let sql = format!("PRAGMA table_info(\"{table}\")");
    match query(con, &sql) {
        Ok(rows) => rows
            .iter()
            .filter_map(|r| r.get("name").and_then(PyValue::as_str).map(str::to_string))
            .collect(),
        Err(_) => Vec::new(),
    }
}

/// 取字符串列，缺失即报错。
///
/// 与 Python 的 `r["id"]` 语义一致（KeyError 也是失败）。
/// **不能悄悄当成空串** —— 那会让两条不同的记录算出同一个指纹。
fn col_str(row: &PyValue, name: &str, what: &str) -> Result<String, CoreError> {
    row.get(name)
        .and_then(PyValue::as_str)
        .map(str::to_string)
        .ok_or_else(|| errf!("{what} 缺少字符串列 {name}"))
}

/// 按列名取值集合（用于 `IN` 判定）。
fn col_str_set(rows: &[PyValue], name: &str) -> BTreeSet<String> {
    rows.iter()
        .filter_map(|r| r.get(name).and_then(PyValue::as_str).map(str::to_string))
        .collect()
}

/// 就地改写一列：键已存在则替换（**保持原位置**），不存在则追加。
/// 与 Python 的 `row["k"] = v` 行为一致。
fn with_column(row: &PyValue, name: &str, value: PyValue) -> PyValue {
    match row {
        PyValue::Ordered(pairs) => {
            let mut out = pairs.clone();
            match out.iter_mut().find(|(k, _)| k == name) {
                Some(slot) => slot.1 = value,
                None => out.push((name.to_string(), value)),
            }
            PyValue::Ordered(out)
        }
        other => other.clone(),
    }
}

// --------------------------------------------------------------------------
// 收集行
// --------------------------------------------------------------------------

pub fn collect_rows(
    src: &Home,
    dst: &Home,
    dst_uid: &str,
    options: &PlanOptions,
) -> Result<CollectedRows, CoreError> {
    let src_con = connect(src)?;
    let dst_con = connect(dst)?;
    let mut out = CollectedRows::default();

    // --- sessions 与它的源侧指纹 -----------------------------------------
    let src_rows = query(&src_con, "SELECT * FROM sessions WHERE deleted_at IS NULL")?;
    let mut src_sessions: BTreeMap<String, PyValue> = BTreeMap::new();
    for row in src_rows {
        let id = col_str(&row, "id", "sessions")?;
        // 与 Python 的 dict 推导一致：重复 id 后者覆盖前者。
        src_sessions.insert(id, row);
    }
    // 指纹覆盖**源侧全部会话**（含目标已存在的），因为它描述的是"源库现状"，
    // 而不是"这次要搬什么"。执行前的漂移复核靠的就是它。
    out.fingerprints.push((
        "sessions_all".into(),
        pyjson::hash_json(&PyValue::Map(
            src_sessions
                .iter()
                .map(|(k, v)| (k.clone(), pyjson::canonicalize(v)))
                .collect(),
        )),
    ));

    let dst_ids = col_str_set(&query(&dst_con, "SELECT id FROM sessions")?, "id");
    let to_copy: BTreeMap<String, PyValue> = src_sessions
        .iter()
        .filter(|(sid, _)| !dst_ids.contains(*sid))
        .map(|(sid, row)| (sid.clone(), row.clone()))
        .collect();
    out.skipped.push((
        "sessions".into(),
        (src_sessions.len() - to_copy.len()) as u64,
    ));

    let copied: BTreeSet<String> = to_copy.keys().cloned().collect();
    let sessions: Vec<PyValue> = to_copy
        .iter()
        // 归属必须改写：否则搬过去的会话在目标库里仍记在源账号名下，
        // 界面上会"看不见"，等于白搬。
        .map(|(_, row)| with_column(row, "user_id", PyValue::Str(dst_uid.to_string())))
        .collect();
    out.tables.push(("sessions".into(), sessions));

    // --- session_usage 跟随会话 ------------------------------------------
    let dst_usage = col_str_set(
        &query(&dst_con, "SELECT session_id FROM session_usage")?,
        "session_id",
    );
    let mut usage = Vec::new();
    for row in query(&src_con, "SELECT * FROM session_usage")? {
        let sid = col_str(&row, "session_id", "session_usage")?;
        if copied.contains(&sid) && !dst_usage.contains(&sid) {
            usage.push(row);
        }
    }
    out.tables.push(("session_usage".into(), usage));

    // --- workspaces 按 path 并集 -----------------------------------------
    let dst_ws = col_str_set(&query(&dst_con, "SELECT path FROM workspaces")?, "path");
    let mut workspaces = Vec::new();
    for row in query(&src_con, "SELECT * FROM workspaces")? {
        let path = col_str(&row, "path", "workspaces")?;
        if !dst_ws.contains(&path) {
            workspaces.push(row);
        }
    }
    out.tables.push(("workspaces".into(), workspaces));

    // --- buddy_snapshots：只搬被引用且目标缺失的 --------------------------
    // 要先看目标库有没有这一列：旧版本的库里可能整列不存在。
    let sessions_for_ref = out
        .tables
        .iter()
        .find(|(t, _)| t == "sessions")
        .map(|(_, r)| r.clone())
        .unwrap_or_default();
    let mut snapshots = Vec::new();
    if target_columns(&dst_con, "buddy_snapshots")
        .iter()
        .any(|c| c == "snapshot_id")
    {
        let dst_snap = col_str_set(
            &query(&dst_con, "SELECT snapshot_id FROM buddy_snapshots")?,
            "snapshot_id",
        );
        let referenced: BTreeSet<String> = sessions_for_ref
            .iter()
            .filter_map(|r| r.get("buddy_snapshot_id"))
            .filter(|v| v.is_truthy())
            .filter_map(PyValue::as_str)
            .map(str::to_string)
            .collect();
        if !referenced.is_empty() {
            for row in query(&src_con, "SELECT * FROM buddy_snapshots")? {
                let sid = col_str(&row, "snapshot_id", "buddy_snapshots")?;
                if referenced.contains(&sid) && !dst_snap.contains(&sid) {
                    snapshots.push(row);
                }
            }
        }
    }
    out.tables.push(("buddy_snapshots".into(), snapshots));

    // --- 自动化（可选）-----------------------------------------------------
    if options.include_automations {
        let auto_all = query(&src_con, "SELECT * FROM automations WHERE deleted_at IS NULL")?;
        out.fingerprints.push((
            "automations_all".into(),
            pyjson::hash_json(&PyValue::Array(
                auto_all.iter().map(pyjson::canonicalize).collect(),
            )),
        ));

        let dst_auto = col_str_set(&query(&dst_con, "SELECT id FROM automations")?, "id");
        let mut autos = Vec::new();
        for row in &auto_all {
            let id = col_str(row, "id", "automations")?;
            if dst_auto.contains(&id) {
                continue;
            }
            // 三处改写都是为了"搬过去但别让它自己跑起来"：
            // 两个 App 各跑一遍同一个自动化，后果是重复执行。
            let row = with_column(row, "owner_user_id", PyValue::Str(dst_uid.to_string()));
            let row = with_column(&row, "owner_status", PyValue::Str("confirmed".into()));
            let row = with_column(&row, "status", PyValue::Str("PAUSED".into()));
            autos.push(with_column(&row, "next_run_at", PyValue::Null));
        }
        let auto_ids: BTreeSet<String> = autos
            .iter()
            .map(|r| col_str(r, "id", "automations"))
            .collect::<Result<_, _>>()?;

        let dst_runs = col_str_set(
            &query(&dst_con, "SELECT thread_id FROM automation_runs")?,
            "thread_id",
        );
        let mut runs = Vec::new();
        for row in query(&src_con, "SELECT * FROM automation_runs")? {
            let aid = col_str(&row, "automation_id", "automation_runs")?;
            let tid = col_str(&row, "thread_id", "automation_runs")?;
            if auto_ids.contains(&aid) && !dst_runs.contains(&tid) {
                runs.push(row);
            }
        }

        let dst_state = col_str_set(
            &query(&dst_con, "SELECT automation_id FROM automation_runtime_state")?,
            "automation_id",
        );
        let mut states = Vec::new();
        for row in query(&src_con, "SELECT * FROM automation_runtime_state")? {
            let aid = col_str(&row, "automation_id", "automation_runtime_state")?;
            if auto_ids.contains(&aid) && !dst_state.contains(&aid) {
                states.push(with_column(&row, "running", PyValue::Int(0)));
            }
        }

        out.tables.push(("automations".into(), autos));
        out.tables.push(("automation_runs".into(), runs));
        out.tables.push(("automation_runtime_state".into(), states));
    }

    // --- 裁剪到目标库实际存在的列 ----------------------------------------
    // 源库可能比目标库新（多几列），照搬会让 INSERT 直接失败。
    let mut cleaned: Vec<(String, Vec<PyValue>)> = Vec::new();
    for (table, table_rows) in &out.tables {
        let cols = target_columns(&dst_con, table);
        if cols.is_empty() {
            continue;
        }
        let filtered = table_rows
            .iter()
            .map(|row| {
                PyValue::Ordered(
                    cols.iter()
                        .filter_map(|c| row.get(c).map(|v| (c.clone(), v.clone())))
                        .collect(),
                )
            })
            .collect();
        cleaned.push((table.clone(), filtered));
    }
    // `sessions` 即使在目标库里没有这张表也要保留一个空列表：
    // 下游按它取会话 id 列表，缺键会让整条流程断掉。
    if !cleaned.iter().any(|(t, _)| t == "sessions") {
        cleaned.push(("sessions".into(), Vec::new()));
    }
    out.tables = cleaned;
    Ok(out)
}

// --------------------------------------------------------------------------
// 方向条目
// --------------------------------------------------------------------------

pub fn build_direction_entries(
    src: &Home,
    dst: &Home,
    src_uid: &str,
    dst_uid: &str,
    session_ids: &[String],
    options: &PlanOptions,
) -> Result<(Vec<PlanEntry>, DirectionStats), CoreError> {
    let mut entries = Vec::new();
    let mut stats = DirectionStats::default();

    let add_file = |entries: &mut Vec<PlanEntry>, stats: &mut DirectionStats, src_rel: &str, dst_rel: &str, note: &str| {
        let s = src.path.join(src_rel);
        // exists() 跟随符号链接，与 Python 的 os.path.exists 一致。
        if s.exists() {
            entries.push(PlanEntry::file(
                s.display().to_string(),
                dst.path.join(dst_rel).display().to_string(),
                note,
            ));
        } else {
            stats.missing_assets += 1;
        }
    };
    let add_tree = |entries: &mut Vec<PlanEntry>, src_rel: &str, dst_rel: &str, note: &str| {
        let s = src.path.join(src_rel);
        if s.is_dir() {
            entries.push(PlanEntry::tree(
                s.display().to_string(),
                dst.path.join(dst_rel).display().to_string(),
                note,
            ));
        }
    };

    let con = connect(src)?;
    let meta: BTreeMap<String, Option<String>> = query(
        &con,
        "SELECT id, cwd FROM sessions WHERE deleted_at IS NULL",
    )?
    .iter()
    .map(|r| {
        let id = r.get("id").and_then(PyValue::as_str).unwrap_or("").to_string();
        let cwd = r.get("cwd").and_then(PyValue::as_str).map(str::to_string);
        (id, cwd)
    })
    .collect();
    drop(con);

    // 去重后排序：同一个会话若在多个方向出现，条目不能重复。
    let unique: BTreeSet<&String> = session_ids.iter().collect();
    for cid in unique {
        let Some(cwd) = meta.get(cid) else { continue };
        stats.sessions += 1;
        // cwd 可能已被删除（用户删了项目目录），这不是错误，只是提示。
        match cwd {
            Some(p) if std::path::Path::new(p).is_dir() => {}
            _ => stats.cwd_invalid += 1,
        }

        for slug in project_slug_for(src, cid) {
            for suffix in PROJECT_SESSION_SUFFIXES {
                let rel = format!("projects/{slug}/{cid}{suffix}");
                add_file(&mut entries, &mut stats, &rel, &rel, "conversation");
            }
            let rel = format!("projects/{slug}/{cid}");
            add_tree(&mut entries, &rel, &rel, "tool-results");
        }

        for (tmpl, needs_changes) in PER_SESSION_TREES {
            if *needs_changes && !options.include_changes {
                continue;
            }
            let rel = tmpl.replace("{cid}", cid);
            add_tree(&mut entries, &rel, &rel, "session asset");
        }
        for tmpl in PER_SESSION_FILES {
            let rel = tmpl.replace("{cid}", cid);
            add_file(&mut entries, &mut stats, &rel, &rel, "session asset");
        }
    }

    // 全局并集（与会话无关，始终尝试）
    for name in BASE_UNION_TREES {
        add_tree(&mut entries, name, name, "content-addressed union");
    }
    for (name, flag) in OPTIONAL_UNION_TREES {
        if options.get(flag) {
            add_tree(&mut entries, name, name, "union");
        }
    }

    // 连接器技能定义：只在显式要求共享连接器时合并
    //（授权本身无法跨 home 转移，搬了也用不了）。
    if options.include_connectors {
        add_tree(&mut entries, "connectors/skills", "connectors/skills", "connector skills");
    }

    // 账号个人存储：源 uid 目录 → 目标 uid 目录
    if options.include_storage {
        for suffix in ["", "-personal"] {
            add_tree(
                &mut entries,
                &format!("storage/user-{src_uid}{suffix}"),
                &format!("storage/user-{dst_uid}{suffix}"),
                "account storage (merge-if-missing)",
            );
        }
    }

    // 连接器：只并状态与 mcp 配置，绝不搬凭据
    if options.include_connectors {
        for name in CONNECTOR_SHARED_FILES {
            add_file(
                &mut entries,
                &mut stats,
                &format!("connectors/{src_uid}/{name}"),
                &format!("connectors/{dst_uid}/{name}"),
                "connector state (no credentials)",
            );
        }
    }

    Ok((entries, stats))
}

// --------------------------------------------------------------------------
// 组装
// --------------------------------------------------------------------------

pub fn build_plan(a: &Home, b: &Home, options: &PlanOptions) -> Result<Plan, CoreError> {
    a.require_valid()?;
    b.require_valid()?;
    let uid_a = a.current_uid()?;
    let uid_b = b.current_uid()?;

    let mut plan = Plan {
        version: VERSION.to_string(),
        home_a: a.path.display().to_string(),
        home_b: b.path.display().to_string(),
        uid_a: uid_a.clone(),
        uid_b: uid_b.clone(),
        created_at: now_local_stamp(),
        options: options.clone(),
        ..Default::default()
    };

    let mut approx: u64 = 0;
    let mut total_sessions: u64 = 0;

    for (side, src, dst, src_uid, dst_uid) in [
        ("a2b", a, b, uid_a.as_str(), uid_b.as_str()),
        ("b2a", b, a, uid_b.as_str(), uid_a.as_str()),
    ] {
        let collected = collect_rows(src, dst, dst_uid, options)?;
        let cids: Vec<String> = collected
            .tables
            .iter()
            .find(|(t, _)| t == "sessions")
            .map(|(_, rows)| {
                rows.iter()
                    .filter_map(|r| r.get("id").and_then(PyValue::as_str).map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();

        total_sessions += cids.len() as u64;
        let (entries, stats) =
            build_direction_entries(src, dst, src_uid, dst_uid, &cids, options)?;

        for e in &entries {
            if e.kind == "file" {
                if let Ok(m) = std::fs::metadata(&e.src) {
                    if m.is_file() {
                        approx += m.len();
                    }
                }
            } else if e.kind == "tree" {
                approx += dir_size(std::path::Path::new(&e.src));
            }
        }

        plan.summary.push((
            side.to_string(),
            PyValue::Ordered(vec![
                ("from".into(), PyValue::Str(src.label.clone())),
                ("to".into(), PyValue::Str(dst.label.clone())),
                (
                    "sessions_to_copy".into(),
                    PyValue::Int(cids.len() as i64),
                ),
                (
                    "sessions_skipped".into(),
                    PyValue::Int(
                        collected
                            .skipped
                            .iter()
                            .find(|(k, _)| k == "sessions")
                            .map(|(_, v)| *v as i64)
                            .unwrap_or(0),
                    ),
                ),
                (
                    "file_entries".into(),
                    PyValue::Int(entries.iter().filter(|e| e.kind == "file").count() as i64),
                ),
                (
                    "tree_entries".into(),
                    PyValue::Int(entries.iter().filter(|e| e.kind == "tree").count() as i64),
                ),
                ("missing_assets".into(), PyValue::Int(stats.missing_assets as i64)),
                ("cwd_invalid".into(), PyValue::Int(stats.cwd_invalid as i64)),
            ]),
        ));

        plan.entries.extend(entries);
        plan.skipped.push((side.to_string(), collected.skipped.clone()));
        plan.fingerprints
            .push((side.to_string(), collected.fingerprints.clone()));
        plan.rows.push((side.to_string(), collected.tables.clone()));
    }

    plan.summary.push((
        "totals".into(),
        PyValue::Ordered(vec![
            ("sessions_to_copy".into(), PyValue::Int(total_sessions as i64)),
            ("approx_bytes".into(), PyValue::Int(approx as i64)),
            ("approx_human".into(), PyValue::Str(human(approx as f64))),
        ]),
    ));

    plan.finalize();
    Ok(plan)
}

/// 本地时间戳，形如 `2026-09-21T12:34:56+0800`（Python `strftime("%Y-%m-%dT%H:%M:%S%z")`）。
///
/// **不进 `plan_id`**，只用于展示与计划文件。所以这里不引时间库：
/// unix 上直接用 `localtime_r`（顺便拿到时区偏移），其余平台退化成 UTC。
#[cfg(unix)]
fn now_local_stamp() -> String {
    use std::mem::MaybeUninit;
    let now = unsafe { libc::time(std::ptr::null_mut()) };
    let mut tm = MaybeUninit::<libc::tm>::uninit();
    let ok = unsafe { libc::localtime_r(&now, tm.as_mut_ptr()) };
    if ok.is_null() {
        return utc_stamp();
    }
    let tm = unsafe { tm.assume_init() };
    let off = tm.tm_gmtoff;
    let sign = if off < 0 { '-' } else { '+' };
    let abs = off.unsigned_abs();
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}{}{:02}{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        sign,
        abs / 3600,
        (abs % 3600) / 60
    )
}

#[cfg(not(unix))]
fn now_local_stamp() -> String {
    utc_stamp()
}

/// UTC 兜底。格式与 [`now_local_stamp`] 一致，只是偏移恒为 `+0000`。
#[allow(dead_code)]
fn utc_stamp() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let (y, m, d) = civil_from_days(secs.div_euclid(86_400));
    let rem = secs.rem_euclid(86_400);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}+0000",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

/// 天数 → 公历年月日（Howard Hinnant 的 civil_from_days）。
#[allow(dead_code)]
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}
