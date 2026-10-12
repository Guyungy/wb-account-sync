//! Linked copies retain original ownership. Divergent edits require review.
//! The undo manifest is durable before writes; recovery checks both pre/post states.
use crate::{
    accounts::Account,
    clients::Client,
    files::{self, Result},
};
use rusqlite::{types::Value as SqlValue, Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Cell {
    Null,
    Int(i64),
    Real(f64),
    Text(String),
    Blob(Vec<u8>),
}
impl Cell {
    fn from_sql(v: SqlValue) -> Self {
        match v {
            SqlValue::Null => Self::Null,
            SqlValue::Integer(v) => Self::Int(v),
            SqlValue::Real(v) => Self::Real(v),
            SqlValue::Text(v) => Self::Text(v),
            SqlValue::Blob(v) => Self::Blob(v),
        }
    }
    fn to_sql(&self) -> SqlValue {
        match self {
            Self::Null => SqlValue::Null,
            Self::Int(v) => SqlValue::Integer(*v),
            Self::Real(v) => SqlValue::Real(*v),
            Self::Text(v) => SqlValue::Text(v.clone()),
            Self::Blob(v) => SqlValue::Blob(v.clone()),
        }
    }
    fn text(&self) -> Option<&str> {
        if let Self::Text(v) = self {
            Some(v)
        } else {
            None
        }
    }
}
type Row = BTreeMap<String, Cell>;
fn quote(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\""))
}
fn rows(con: &Connection, sql: &str) -> Result<Vec<Row>> {
    let mut stmt = con.prepare(sql).map_err(|e| e.to_string())?;
    let columns: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
    let mapped = stmt
        .query_map([], |r| {
            columns
                .iter()
                .enumerate()
                .map(|(i, n)| {
                    r.get::<_, SqlValue>(i)
                        .map(|v| (n.clone(), Cell::from_sql(v)))
                })
                .collect::<rusqlite::Result<Row>>()
        })
        .map_err(|e| e.to_string())?;
    mapped
        .collect::<rusqlite::Result<_>>()
        .map_err(|e| e.to_string())
}
fn text<'a>(row: &'a Row, key: &str) -> &'a str {
    row.get(key).and_then(Cell::text).unwrap_or("")
}
fn open(home: &Path, write: bool) -> Result<Connection> {
    let path = home.join("workbuddy.db");
    files::plain(&path)?;
    let con = Connection::open_with_flags(
        path,
        if write {
            OpenFlags::SQLITE_OPEN_READ_WRITE
        } else {
            OpenFlags::SQLITE_OPEN_READ_ONLY
        },
    )
    .map_err(|e| e.to_string())?;
    con.busy_timeout(std::time::Duration::from_secs(10))
        .map_err(|e| e.to_string())?;
    if write {
        con.execute_batch("PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;")
            .map_err(|e| e.to_string())?;
    }
    Ok(con)
}
fn schema_ok(con: &Connection) -> Result<()> {
    let columns = rows(con, "PRAGMA table_info(sessions)")?;
    for name in ["id", "user_id", "cwd", "deleted_at"] {
        if !columns.iter().any(|c| text(c, "name") == name) {
            return Err(format!("Unsupported sessions schema: missing {name}"));
        }
    }
    if !columns
        .iter()
        .any(|c| text(c, "name") == "id" && c.get("pk") == Some(&Cell::Int(1)))
    {
        return Err("sessions.id must be a primary key".into());
    }
    let triggers: i64 = con
        .query_row(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='sessions'",
            [],
            |r| r.get(0),
        )
        .map_err(|e| e.to_string())?;
    if triggers != 0 {
        return Err("Sessions triggers are unsupported".into());
    }
    for index in rows(con, "PRAGMA index_list(sessions)")? {
        if index.get("unique") == Some(&Cell::Int(1)) {
            let cols = rows(
                con,
                &format!("PRAGMA index_info({})", quote(text(&index, "name"))),
            )?;
            if cols.len() != 1 || text(&cols[0], "name") != "id" {
                return Err("Alternate unique session keys are unsupported".into());
            }
        }
    }
    Ok(())
}
pub fn db_digest(con: &Connection) -> Result<String> {
    if con.is_autocommit() {
        let tx = con.unchecked_transaction().map_err(|e| e.to_string())?;
        return db_digest_snapshot(&tx);
    }
    db_digest_snapshot(con)
}
fn db_digest_snapshot(con: &Connection) -> Result<String> {
    let schema = rows(
        con,
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name",
    )?;
    let mut tables = BTreeMap::new();
    for row in &schema {
        if text(row, "type") == "table" {
            let name = text(row, "name");
            let mut data = rows(con, &format!("SELECT * FROM {}", quote(name)))?;
            data.sort_by_cached_key(|r| serde_json::to_vec(r).unwrap());
            tables.insert(name.to_string(), data);
        }
    }
    Ok(files::hash(
        &serde_json::to_vec(&(schema, tables)).map_err(|e| e.to_string())?,
    ))
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Member {
    pub variant: String,
    pub uid: String,
    pub id: String,
    pub baseline: String,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Group {
    pub id: String,
    pub members: Vec<Member>,
}
#[derive(Clone, Default, Serialize, Deserialize)]
pub struct Links {
    pub groups: Vec<Group>,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct FileChange {
    pub path: PathBuf,
    pub before: Option<String>,
    pub bytes: Vec<u8>,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct RowChange {
    pub variant: String,
    pub table: String,
    pub pk: String,
    pub before: Option<Row>,
    pub after: Row,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Plan {
    pub plan_id: String,
    pub homes: BTreeMap<String, PathBuf>,
    pub databases: BTreeMap<String, String>,
    pub watches: BTreeMap<PathBuf, Option<String>>,
    pub files: Vec<FileChange>,
    pub rows: Vec<RowChange>,
    pub links: Links,
    pub conflicts: Vec<Value>,
    pub skipped: Vec<Value>,
    pub source_sessions: usize,
    pub inventories: BTreeMap<PathBuf, Vec<PathBuf>>,
}
impl Plan {
    pub fn public(&self) -> Value {
        json!({"plan_id":self.plan_id,"source_sessions":self.source_sessions,
            "new_copies":self.rows.iter().filter(|r|r.table=="sessions" && r.before.is_none()).count(),
            "updated_copies":self.rows.iter().filter(|r|r.table=="sessions" && r.before.is_some()).count(),
            "files":self.files.len(),"bytes":self.files.iter().map(|f|f.bytes.len()).sum::<usize>(),
            "conflicts":self.conflicts,"skipped":self.skipped,
            "copies":self.rows.iter().filter(|r|r.table=="sessions").map(|r|json!({"variant":r.variant,"uid":text(&r.after,"user_id"),"id":text(&r.after,"id"),"title":text(&r.after,"title"),"update":r.before.is_some()})).collect::<Vec<_>>()})
    }
}
fn file_hash(path: &Path) -> Result<Option<String>> {
    files::plain(path)?;
    match fs::read(path) {
        Ok(b) => Ok(Some(files::hash(&b))),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}
fn walk(path: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    files::plain(path)?;
    if !path.exists() {
        return Ok(());
    }
    if path.is_file() {
        out.push(path.into());
        return Ok(());
    }
    let mut entries: Vec<_> = fs::read_dir(path)
        .map_err(|e| e.to_string())?
        .collect::<std::result::Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    entries.sort_by_key(|e| e.path());
    for entry in entries {
        walk(&entry.path(), out)?;
    }
    Ok(())
}
fn safe_id(id: &str) -> Result<()> {
    if id.is_empty()
        || id.len() > 200
        || !id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err("Invalid session ID".into());
    }
    Ok(())
}
fn asset_paths(home: &Path, id: &str) -> Result<Vec<PathBuf>> {
    safe_id(id)?;
    let mut found = Vec::new();
    let projects = home.join("projects");
    files::plain(&projects)?;
    if projects.exists() {
        for suffix in [".jsonl", ".meta.json", ".file-rollback.ndjson"] {
            walk(&projects.join(format!("{id}{suffix}")), &mut found)?;
        }
        walk(&projects.join(id), &mut found)?;
        for e in fs::read_dir(projects).map_err(|e| e.to_string())? {
            let path = e.map_err(|e| e.to_string())?.path();
            files::plain(&path)?;
            if !path.is_dir() {
                continue;
            }
            for suffix in [".jsonl", ".meta.json", ".file-rollback.ndjson"] {
                walk(&path.join(format!("{id}{suffix}")), &mut found)?;
            }
            walk(&path.join(id), &mut found)?;
        }
    }
    for tree in ["tasks", "changes-detail", "changes-index", "file-history"] {
        walk(&home.join(tree).join(id), &mut found)?;
    }
    walk(
        &home.join("artifact-index").join(format!("{id}.json")),
        &mut found,
    )?;
    found.sort();
    Ok(found)
}
/// Rewrite structural identifiers and asset paths, leaving prose/prompt text intact.
fn rewrite(v: &mut Value, old: &str, new: &str, key: &str, homes: (&str, &str)) {
    match v {
        Value::Object(obj) => {
            for (k, val) in obj {
                rewrite(val, old, new, k, homes);
            }
        }
        Value::Array(vals) => {
            for val in vals {
                rewrite(val, old, new, key, homes);
            }
        }
        Value::String(s) => {
            if matches!(
                key,
                "sessionId"
                    | "session_id"
                    | "conversationId"
                    | "conversation_id"
                    | "threadId"
                    | "thread_id"
                    | "parentSessionId"
            ) && s == old
            {
                *s = new.into();
            } else if key.to_ascii_lowercase().contains("path") || key.ends_with("Uri") {
                for root in [
                    "projects",
                    "tasks",
                    "changes-detail",
                    "changes-index",
                    "file-history",
                    "artifact-index",
                    "blobs",
                ] {
                    for uri_prefix in ["", "file://"] {
                        let prefix = format!("{uri_prefix}{}/{root}/", homes.0);
                        if let Some(rest) = s.strip_prefix(&prefix) {
                            *s = format!("{uri_prefix}{}/{root}/{rest}", homes.1);
                        }
                    }
                }
                *s = s
                    .replace(&format!("/{old}/"), &format!("/{new}/"))
                    .replace(&format!("/{old}."), &format!("/{new}."))
                    .replace(&format!("\\{old}\\"), &format!("\\{new}\\"))
                    .replace(&format!("\\{old}."), &format!("\\{new}."));
                for separator in ["/", "\\"] {
                    if s.ends_with(&format!("{separator}{old}")) {
                        s.truncate(s.len() - old.len());
                        s.push_str(new);
                    }
                    if let Some(rest) = s.strip_prefix(&format!("{old}{separator}")) {
                        *s = format!("{new}{separator}{rest}");
                    }
                }
            }
        }
        _ => {}
    }
}
fn transform(
    path: &Path,
    bytes: &[u8],
    old: &str,
    new: &str,
    homes: (&str, &str),
) -> Result<Vec<u8>> {
    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("");
    if matches!(ext, "jsonl" | "ndjson") {
        let s = std::str::from_utf8(bytes).map_err(|_| "Invalid UTF-8 in conversation")?;
        let mut output = Vec::new();
        for line in s.lines().filter(|s| !s.trim().is_empty()) {
            let mut value: Value = serde_json::from_str(line)
                .map_err(|_| "Invalid JSONL in conversation; incomplete writes cannot be copied")?;
            rewrite(&mut value, old, new, "", homes);
            output.extend(serde_json::to_vec(&value).map_err(|e| e.to_string())?);
            output.push(b'\n');
        }
        Ok(output)
    } else if ext == "json" {
        let mut value: Value = serde_json::from_slice(bytes).map_err(|_| "Invalid JSON asset")?;
        rewrite(&mut value, old, new, "", homes);
        serde_json::to_vec(&value).map_err(|e| e.to_string())
    } else {
        Ok(bytes.into())
    }
}
fn rel_copy(path: &Path, old: &str, new: &str) -> PathBuf {
    path.components()
        .map(|c| {
            let s = c.as_os_str().to_string_lossy();
            if s == old {
                new.into()
            } else if let Some(suffix) = s.strip_prefix(&format!("{old}.")) {
                format!("{new}.{suffix}")
            } else {
                s.into_owned()
            }
        })
        .collect()
}
#[derive(Clone)]
struct Snapshot {
    row: Row,
    assets: BTreeMap<PathBuf, Vec<u8>>,
    hash: String,
    normalized: BTreeMap<PathBuf, String>,
}
fn snapshot(home: &Path, row: &Row) -> Result<Snapshot> {
    let id = text(row, "id");
    let mut normalized = BTreeMap::new();
    let mut assets = BTreeMap::new();
    for path in asset_paths(home, id)? {
        let bytes = fs::read(&path).map_err(|e| e.to_string())?;
        let rel = path
            .strip_prefix(home)
            .map_err(|e| e.to_string())?
            .to_path_buf();
        let norm = transform(
            &path,
            &bytes,
            id,
            "SESSION",
            (&home.to_string_lossy(), "HOME"),
        )?;
        normalized.insert(rel_copy(&rel, id, "SESSION"), files::hash(&norm));
        assets.insert(rel, bytes);
    }
    if !assets.keys().any(|p| {
        p.components()
            .next()
            .is_some_and(|c| c.as_os_str() == "projects")
            && p.extension().is_some_and(|e| e == "jsonl")
            && p.file_stem().is_some_and(|s| s == id)
    }) {
        return Err("Conversation body is missing".into());
    }
    let hash = snapshot_hash(&normalized, row)?;
    Ok(Snapshot {
        row: row.clone(),
        assets,
        hash,
        normalized,
    })
}
fn snapshot_hash(normalized: &BTreeMap<PathBuf, String>, row: &Row) -> Result<String> {
    let mut metadata = row.clone();
    for key in [
        "id",
        "user_id",
        "created_at",
        "updated_at",
        "title",
        "custom_title",
    ] {
        metadata.remove(key);
    }
    Ok(files::hash(
        &serde_json::to_vec(&(normalized, metadata)).map_err(|e| e.to_string())?,
    ))
}
fn copy_id(group: &str, variant: &str, uid: &str) -> String {
    let hex = files::hash(format!("wb-linked-copy:{group}:{variant}:{uid}").as_bytes());
    format!(
        "{}-{}-4{}-a{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[13..16],
        &hex[17..20],
        &hex[20..32]
    )
}
pub fn build(clients: &[Client], targets: &[Account], state: &Path) -> Result<Plan> {
    if targets.is_empty() {
        return Err("Select at least one target account".into());
    }
    let mut homes = BTreeMap::new();
    let mut databases = BTreeMap::new();
    let mut sessions = BTreeMap::new();
    for c in clients {
        if !c.home.join("workbuddy.db").is_file() {
            continue;
        }
        let home = fs::canonicalize(&c.home).map_err(|e| e.to_string())?;
        files::plain(&c.home)?;
        let con = open(&home, false)?;
        schema_ok(&con)?;
        databases.insert(c.variant.clone(), db_digest(&con)?);
        homes.insert(c.variant.clone(), home);
        for row in rows(
            &con,
            "SELECT * FROM sessions WHERE deleted_at IS NULL ORDER BY id",
        )? {
            if text(&row, "user_id").is_empty() {
                continue;
            }
            sessions.insert((c.variant.clone(), text(&row, "id").to_string()), row);
        }
    }
    for target in targets {
        if !homes.contains_key(&target.variant) {
            return Err(format!("No database for {}", target.variant));
        }
        if target.session["account"]["enterpriseId"]
            .as_str()
            .is_some_and(|s| !s.is_empty())
        {
            return Err("Enterprise accounts require a separate scope; this sync supports personal accounts".into());
        }
    }
    let links_path = state.join("links.json");
    let mut links: Links = if links_path.exists() {
        files::read_json(&links_path)?
    } else {
        Links::default()
    };
    let known: BTreeSet<_> = links
        .groups
        .iter()
        .flat_map(|g| g.members.iter().map(|m| (m.variant.clone(), m.id.clone())))
        .collect();
    for ((variant, id), row) in &sessions {
        if !known.contains(&(variant.clone(), id.clone())) {
            let member = Member {
                variant: variant.clone(),
                uid: text(row, "user_id").into(),
                id: id.clone(),
                baseline: String::new(),
            };
            // Earlier cross-home bridges kept the same session ID. Adopt those existing copies.
            if let Some(group) = links
                .groups
                .iter_mut()
                .find(|g| g.members.iter().any(|m| m.id == *id))
            {
                group.members.push(member);
            } else {
                links.groups.push(Group {
                    id: files::hash(id.as_bytes()),
                    members: vec![member],
                });
            }
        }
    }
    let mut plan = Plan {
        plan_id: String::new(),
        homes,
        databases,
        watches: BTreeMap::new(),
        files: Vec::new(),
        rows: Vec::new(),
        links: Links::default(),
        conflicts: Vec::new(),
        skipped: Vec::new(),
        source_sessions: sessions.len(),
        inventories: BTreeMap::new(),
    };
    plan.watches
        .insert(links_path.clone(), file_hash(&links_path)?);
    for group in &mut links.groups {
        let mut snapshots = Vec::new();
        let mut unavailable = false;
        for (i, member) in group.members.iter().enumerate() {
            let key = (member.variant.clone(), member.id.clone());
            let Some(row) = sessions.get(&key) else {
                unavailable = true;
                plan.skipped.push(json!({"id":member.id,"reason":"Linked member was deleted or its database is unavailable; group skipped"}));
                continue;
            };
            if text(row, "user_id") != member.uid {
                return Err("Linked session ownership changed".into());
            }
            match snapshot(&plan.homes[&member.variant], row) {
                Ok(s) => {
                    for (rel, bytes) in &s.assets {
                        plan.watches.insert(
                            plan.homes[&member.variant].join(rel),
                            Some(files::hash(bytes)),
                        );
                    }
                    snapshots.push((i, s));
                }
                Err(e) => {
                    unavailable = true;
                    plan.skipped.push(json!({"id":member.id,"reason":e}));
                }
            }
        }
        if unavailable || snapshots.is_empty() {
            continue;
        }
        if snapshots.iter().any(|(_, s)| {
            text(&s.row, "cwd")
                .trim()
                .trim_end_matches(['/', '\\'])
                .rsplit(['/', '\\'])
                .next()
                .is_some_and(|n| n.eq_ignore_ascii_case("claw"))
                || !text(&s.row, "enterprise_id").is_empty()
        }) {
            plan.skipped.push(json!({"group":group.id,"reason":"Claw conversations are tied to account channel bindings"}));
            continue;
        }
        let changed: Vec<_> = snapshots
            .iter()
            .filter(|(i, s)| {
                !group.members[*i].baseline.is_empty() && s.hash != group.members[*i].baseline
            })
            .collect();
        let distinct: BTreeSet<_> = changed.iter().map(|(_, s)| s.hash.clone()).collect();
        let initial: BTreeSet<_> = snapshots.iter().map(|(_, s)| s.hash.clone()).collect();
        if distinct.len() > 1
            || (group.members.iter().all(|m| m.baseline.is_empty()) && initial.len() > 1)
        {
            plan.conflicts.push(json!({"group":group.id,"title":text(&snapshots[0].1.row,"title"),"members":group.members,"reason":"Different copies were edited; no member of this group will be overwritten"}));
            continue;
        }
        let (source_i, source) = changed.first().copied().unwrap_or(&snapshots[0]);
        let source_member = group.members[*source_i].clone();
        let source_id = &source_member.id;
        for target in targets {
            if !group
                .members
                .iter()
                .any(|m| m.variant == target.variant && m.uid == target.uid)
            {
                group.members.push(Member {
                    variant: target.variant.clone(),
                    uid: target.uid.clone(),
                    id: copy_id(&group.id, &target.variant, &target.uid),
                    baseline: String::new(),
                });
            }
        }
        for (i, member) in group.members.iter_mut().enumerate() {
            if i == *source_i {
                member.baseline = source.hash.clone();
                continue;
            }
            let existing = snapshots.iter().find(|(idx, _)| *idx == i).map(|(_, s)| s);
            if existing.is_some_and(|s| {
                s.hash == source.hash || (changed.is_empty() && s.hash == member.baseline)
            }) {
                member.baseline = existing.unwrap().hash.clone();
                continue;
            }
            let target_home = &plan.homes[&member.variant];
            let mut row = source.row.clone();
            row.insert("id".into(), Cell::Text(member.id.clone()));
            row.insert("user_id".into(), Cell::Text(member.uid.clone()));
            if let Some(existing) = existing {
                for k in ["created_at", "title", "custom_title"] {
                    if let Some(v) = existing.row.get(k) {
                        row.insert(k.into(), v.clone());
                    }
                }
            } else if row.contains_key("created_at") {
                row.insert("created_at".into(), Cell::Int(files::now_ms()));
            }
            if row.contains_key("updated_at") {
                row.insert("updated_at".into(), Cell::Int(files::now_ms()));
            }
            let target_con = open(target_home, false)?;
            row = compatible_session_row(&target_con, &row)?;
            if let Some(existing) = existing {
                for (name, value) in &existing.row {
                    if !source.row.contains_key(name) {
                        row.insert(name.clone(), value.clone());
                    }
                }
            }
            let snapshot_id = text(&row, "buddy_snapshot_id");
            if !snapshot_id.is_empty() && existing.is_none() {
                let exists: bool = target_con
                    .query_row(
                        "SELECT EXISTS(SELECT 1 FROM buddy_snapshots WHERE snapshot_id=?1)",
                        [snapshot_id],
                        |r| r.get(0),
                    )
                    .map_err(|e| e.to_string())?;
                if !exists {
                    let source_con = open(&plan.homes[&source_member.variant], false)?;
                    let snapshot = rows(&source_con, "SELECT * FROM buddy_snapshots")?
                        .into_iter()
                        .find(|r| text(r, "snapshot_id") == snapshot_id)
                        .ok_or("Referenced buddy snapshot is missing in source client")?;
                    let cols: BTreeSet<_> =
                        rows(&target_con, "PRAGMA table_info(buddy_snapshots)")?
                            .iter()
                            .map(|r| text(r, "name").to_string())
                            .collect();
                    if snapshot.keys().any(|k| !cols.contains(k)) || cols.len() != snapshot.len() {
                        return Err("Buddy snapshot schemas differ".into());
                    }
                    if !plan.rows.iter().any(|r| {
                        r.variant == member.variant
                            && r.table == "buddy_snapshots"
                            && text(&r.after, "snapshot_id") == snapshot_id
                    }) {
                        plan.rows.push(RowChange {
                            variant: member.variant.clone(),
                            table: "buddy_snapshots".into(),
                            pk: "snapshot_id".into(),
                            before: None,
                            after: snapshot,
                        });
                    }
                }
            }
            for (rel, bytes) in &source.assets {
                let path = target_home.join(rel_copy(rel, source_id, &member.id));
                let before = file_hash(&path)?;
                if existing.is_none() && before.is_some() {
                    return Err(format!(
                        "Unlinked target asset already exists: {}",
                        path.display()
                    ));
                }
                let bytes = transform(
                    rel,
                    bytes,
                    source_id,
                    &member.id,
                    (
                        &plan.homes[&source_member.variant].to_string_lossy(),
                        &target_home.to_string_lossy(),
                    ),
                )?;
                if before.as_deref() == Some(&files::hash(&bytes)) {
                    continue;
                }
                plan.watches.insert(path.clone(), before.clone());
                plan.files.push(FileChange {
                    path,
                    before,
                    bytes,
                });
            }
            // Remove no assets. A deletion on one side is a conflict, rather than resurrection.
            if existing.is_some_and(|s| {
                s.assets.keys().any(|p| {
                    !source
                        .assets
                        .contains_key(&rel_copy(p, &member.id, source_id))
                })
            }) {
                return Err(
                    "A linked copy has extra assets; resolve its divergence before syncing".into(),
                );
            }
            plan.rows.push(RowChange {
                variant: member.variant.clone(),
                table: "sessions".into(),
                pk: "id".into(),
                before: existing.map(|s| s.row.clone()),
                after: row.clone(),
            });
            member.baseline = snapshot_hash(&source.normalized, &row)?;
        }
    }
    // Content-addressed attachments are shared by every copy in that client.
    let mut blobs = BTreeMap::new();
    for home in plan.homes.values() {
        let mut paths = Vec::new();
        walk(&home.join("blobs"), &mut paths)?;
        for path in paths {
            let rel = path.strip_prefix(home).unwrap().to_path_buf();
            let bytes = fs::read(&path).map_err(|e| e.to_string())?;
            plan.watches.insert(path, Some(files::hash(&bytes)));
            if blobs.get(&rel).is_some_and(|prev| prev != &bytes) {
                return Err("Content-addressed attachment collision".into());
            }
            blobs.insert(rel, bytes);
        }
    }
    let target_variants: BTreeSet<_> = targets.iter().map(|a| a.variant.clone()).collect();
    for variant in target_variants {
        for (rel, bytes) in &blobs {
            let path = plan.homes[&variant].join(rel);
            let before = file_hash(&path)?;
            if before.is_none() {
                plan.watches.insert(path.clone(), None);
                plan.files.push(FileChange {
                    path,
                    before: None,
                    bytes: bytes.clone(),
                });
            } else if before != Some(files::hash(bytes)) {
                return Err("Target attachment differs from source".into());
            }
        }
    }
    plan.links = links;
    // Detect new/deleted assets as well as content edits after a preview.
    for home in plan.homes.values() {
        for root in [
            "projects",
            "tasks",
            "changes-detail",
            "changes-index",
            "file-history",
            "artifact-index",
            "blobs",
        ] {
            let path = home.join(root);
            let mut entries = Vec::new();
            walk(&path, &mut entries)?;
            plan.inventories.insert(path, entries);
        }
    }
    plan.plan_id = files::hash(&serde_json::to_vec(&plan).map_err(|e| e.to_string())?);
    Ok(plan)
}

#[derive(Clone, Serialize, Deserialize)]
pub struct UndoFile {
    path: PathBuf,
    before: Option<String>,
    after: String,
    backup: Option<PathBuf>,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Journal {
    pub run_id: String,
    pub status: String,
    pub homes: BTreeMap<String, PathBuf>,
    pub before_db: BTreeMap<String, String>,
    pub after_db: BTreeMap<String, String>,
    pub backups: BTreeMap<String, PathBuf>,
    pub files: Vec<UndoFile>,
}
pub fn pending(state: &Path) -> Result<Vec<Journal>> {
    let dir = state.join("session-runs");
    if !dir.exists() {
        return Ok(Vec::new());
    }
    let mut result = Vec::new();
    for e in fs::read_dir(dir).map_err(|e| e.to_string())? {
        let p = e.map_err(|e| e.to_string())?.path().join("journal.json");
        if p.is_file() {
            let j: Journal = files::read_json(&p)?;
            if j.status == "prepared" || j.status == "restoring" {
                result.push(j);
            }
        }
    }
    Ok(result)
}
fn lock_homes(homes: &BTreeMap<String, PathBuf>) -> Result<Vec<files::Lock>> {
    let sorted: BTreeSet<_> = homes.values().collect();
    sorted
        .into_iter()
        .map(|h| files::Lock::acquire(&h.join(".wb-operations.lock")))
        .collect()
}
fn check_offline(clients: &[Client], homes: &BTreeMap<String, PathBuf>) -> Result<()> {
    for c in clients {
        if homes.contains_key(&c.variant) {
            c.require_stopped()?;
        }
    }
    Ok(())
}
fn validate_plan(plan: &Plan, state: &Path) -> Result<()> {
    if !pending(state)?.is_empty() {
        return Err("An interrupted sync needs recovery first".into());
    }
    validate_before(plan)
}
// Older clients can accept a projection only when no meaningful metadata is lost.
fn compatible_session_row(con: &Connection, source: &Row) -> Result<Row> {
    let columns = rows(con, "PRAGMA table_info(sessions)")?;
    let names: BTreeSet<_> = columns.iter().map(|r| text(r, "name").to_owned()).collect();
    let unsupported: Vec<_> = source
        .iter()
        .filter(|(name, value)| {
            if names.contains(*name) || **value == Cell::Null {
                return false;
            }
            !matches!((name.as_str(), *value),
            ("transport", Cell::Text(v)) if v == "local")
                && !matches!((name.as_str(), *value), ("unread", Cell::Int(0 | 1)))
        })
        .map(|(name, _)| name.as_str())
        .collect();
    if !unsupported.is_empty() {
        return Err(format!("客户端会话结构不兼容：目标客户端无法保存字段 {} 的有效数据。请升级目标客户端，或只同步到同一客户端的账号。", unsupported.join("、")));
    }
    for column in &columns {
        let name = text(column, "name");
        if !source.contains_key(name)
            && column.get("notnull") == Some(&Cell::Int(1))
            && column.get("dflt_value") == Some(&Cell::Null)
        {
            return Err(format!("客户端会话结构不兼容：目标客户端要求字段 {name}，但源客户端没有提供。请升级两个客户端后重试。"));
        }
    }
    let mut result: Row = source
        .iter()
        .filter(|(name, _)| names.contains(*name))
        .map(|(name, value)| (name.clone(), value.clone()))
        .collect();
    for column in &columns {
        let name = text(column, "name");
        if !result.contains_key(name) {
            let value = if let Some(Cell::Text(default)) = column.get("dflt_value") {
                let v: SqlValue = con
                    .query_row(&format!("SELECT {default}"), [], |r| r.get(0))
                    .map_err(|e| e.to_string())?;
                Cell::from_sql(v)
            } else {
                Cell::Null
            };
            result.insert(name.to_owned(), value);
        }
    }
    Ok(result)
}
fn put_row(con: &Connection, change: &RowChange) -> Result<()> {
    let row = &change.after;
    let columns: Vec<_> = row.keys().collect();
    let params: Vec<_> = row.values().map(Cell::to_sql).collect();
    if change.before.is_none() {
        let sql = format!(
            "INSERT INTO {} ({}) VALUES ({})",
            quote(&change.table),
            columns
                .iter()
                .map(|s| quote(s))
                .collect::<Vec<_>>()
                .join(","),
            vec!["?"; columns.len()].join(",")
        );
        con.execute(&sql, rusqlite::params_from_iter(params))
            .map_err(|e| e.to_string())?;
    } else {
        let sql = format!(
            "UPDATE {} SET {} WHERE {}=?",
            quote(&change.table),
            columns
                .iter()
                .map(|s| format!("{}=?", quote(s)))
                .collect::<Vec<_>>()
                .join(","),
            quote(&change.pk)
        );
        let mut params = params;
        params.push(SqlValue::Text(text(row, &change.pk).into()));
        let n = con
            .execute(&sql, rusqlite::params_from_iter(params))
            .map_err(|e| e.to_string())?;
        if n != 1 {
            return Err("Target session disappeared".into());
        }
    }
    Ok(())
}
/// Account switching has no user-reviewed preview: rebuild only after pre-write drift.
pub fn sync_for_switch(clients: &[Client], targets: &[Account], state: &Path) -> Result<Value> {
    retry_before_writes(|| {
        if !pending(state)?.is_empty() {
            return Err("存在中断的同步，请先恢复，再切换账号。".into());
        }
        for client in clients {
            client.require_stopped()?;
        }
        let plan = build(clients, targets, state)?;
        apply(&plan, &plan.plan_id, clients, state)
    })
}
fn retry_before_writes(mut work: impl FnMut() -> Result<Value>) -> Result<Value> {
    for attempt in 0..3 {
        match work() {
            Err(error) if error == "Database changed after preview; regenerate the plan" => {
                if attempt == 2 {
                    return Err("客户端退出后数据库仍在变化，账号尚未切换。请确认 WorkBuddy 及其辅助进程已完全退出后重试。".into());
                }
                std::thread::sleep(std::time::Duration::from_millis(300));
            }
            result => return result,
        }
    }
    unreachable!()
}

pub fn apply(plan: &Plan, confirm: &str, clients: &[Client], state: &Path) -> Result<Value> {
    apply_with_checkpoint(plan, confirm, clients, state, |_| {})
}
/// Checkpoints are for observation/testing; there is no environment-controlled failpoint.
pub fn apply_with_checkpoint(
    plan: &Plan,
    confirm: &str,
    clients: &[Client],
    state: &Path,
    checkpoint: impl Fn(&str),
) -> Result<Value> {
    if confirm != plan.plan_id {
        return Err("Full plan_id confirmation is required".into());
    }
    let _locks = lock_homes(&plan.homes)?;
    check_offline(clients, &plan.homes)?;
    validate_plan(plan, state)?;
    if plan.rows.is_empty() && plan.files.is_empty() {
        return Ok(json!({"status":"unchanged","conflicts":plan.conflicts,"skipped":plan.skipped}));
    }
    let run_id = uuid::Uuid::new_v4().to_string();
    let dir = state.join("session-runs").join(&run_id);
    files::private_dir(&dir)?;
    let bytes = plan.files.iter().map(|f| f.bytes.len() as u64).sum::<u64>();
    let db_bytes = plan
        .homes
        .values()
        .map(|h| {
            fs::metadata(h.join("workbuddy.db"))
                .map(|m| m.len())
                .unwrap_or(0)
        })
        .sum::<u64>();
    for path in std::iter::once(state).chain(plan.homes.values().map(PathBuf::as_path)) {
        if fs2::available_space(path).map_err(|e| e.to_string())?
            < bytes.saturating_mul(2) + db_bytes + 256 * 1024 * 1024
        {
            return Err("Insufficient space for data, backups and 256 MB reserve".into());
        }
    }
    let mut j = Journal {
        run_id: run_id.clone(),
        status: "prepared".into(),
        homes: plan.homes.clone(),
        before_db: plan.databases.clone(),
        after_db: BTreeMap::new(),
        backups: BTreeMap::new(),
        files: Vec::new(),
    };
    // Keep real SQLite backups, including pending WAL data.
    for (v, h) in &plan.homes {
        let path = dir.join(format!("{v}.db"));
        open(h, false)?
            .backup(rusqlite::MAIN_DB, &path, None)
            .map_err(|e| e.to_string())?;
        files::atomic_bytes(&path, &fs::read(&path).map_err(|e| e.to_string())?)?;
        j.backups.insert(v.clone(), path);
    }
    let mut changes = plan.files.clone();
    let links_path = state.join("links.json");
    changes.push(FileChange {
        path: links_path.clone(),
        before: file_hash(&links_path)?,
        bytes: serde_json::to_vec_pretty(&plan.links).map_err(|e| e.to_string())?,
    });
    for (i, f) in changes.iter().enumerate() {
        let backup = if f.before.is_some() {
            let p = dir.join(format!("asset-{i}"));
            files::atomic_bytes(&p, &fs::read(&f.path).map_err(|e| e.to_string())?)?;
            Some(p)
        } else {
            None
        };
        j.files.push(UndoFile {
            path: f.path.clone(),
            before: f.before.clone(),
            after: files::hash(&f.bytes),
            backup,
        });
    }
    let journal = dir.join("journal.json");
    files::atomic_json(&journal, &j)?;
    let result: Result<Value> = (|| {
        check_offline(clients, &plan.homes)?;
        validate_before(plan)?;
        // File writes are recoverable even if a process stops between two database commits.
        for change in changes.iter().take(changes.len() - 1) {
            files::atomic_bytes(&change.path, &change.bytes)?;
        }
        checkpoint("files_written");
        for (v, h) in &plan.homes {
            let mut con = open(h, true)?;
            let tx = con
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(|e| e.to_string())?;
            if db_digest(&tx)? != plan.databases[v] {
                return Err("Database changed while syncing".into());
            }
            for row in &plan.rows {
                if &row.variant == v {
                    put_row(&tx, row)?;
                }
            }
            j.after_db.insert(v.clone(), db_digest(&tx)?);
            files::atomic_json(&journal, &j)?;
            tx.commit().map_err(|e| e.to_string())?;
            checkpoint("database_committed");
        }
        files::atomic_bytes(&links_path, &changes.last().unwrap().bytes)?;
        checkpoint("links_written");
        for f in &j.files {
            if file_hash(&f.path)? != Some(f.after.clone()) {
                return Err("File verification failed".into());
            }
        }
        for (v, h) in &plan.homes {
            if db_digest(&open(h, false)?)? != j.after_db[v] {
                return Err("Database verification failed".into());
            }
        }
        j.status = "committed".into();
        files::atomic_json(&journal, &j)?;
        Ok(
            json!({"status":"committed","run_id":run_id,"new_copies":plan.rows.iter().filter(|r|r.table=="sessions" && r.before.is_none()).count(),"updated_copies":plan.rows.iter().filter(|r|r.table=="sessions" && r.before.is_some()).count(),"conflicts":plan.conflicts,"skipped":plan.skipped}),
        )
    })();
    result.map_err(|e| {
        format!("{e}. Recovery run: {run_id}; clients must remain closed until recovery.")
    })
}
fn validate_before(plan: &Plan) -> Result<()> {
    for (v, h) in &plan.homes {
        if db_digest(&open(h, false)?)? != plan.databases[v] {
            return Err("Database changed after preview; regenerate the plan".into());
        }
    }
    for (p, hash) in &plan.watches {
        if &file_hash(p)? != hash {
            return Err("File drift".into());
        }
    }
    for (p, expected) in &plan.inventories {
        let mut current = Vec::new();
        walk(p, &mut current)?;
        if &current != expected {
            return Err("Asset inventory changed after preview; regenerate the plan".into());
        }
    }
    Ok(())
}
pub fn restore(run_id: &str, clients: &[Client], state: &Path) -> Result<Value> {
    uuid::Uuid::parse_str(run_id).map_err(|_| "Invalid run ID")?;
    let dir = state.join("session-runs").join(run_id);
    let path = dir.join("journal.json");
    let mut j: Journal = files::read_json(&path)?;
    if j.status == "restored" {
        return Ok(json!({"status":"restored","run_id":run_id}));
    }
    let configured: BTreeMap<_, _> = clients
        .iter()
        .filter_map(|c| {
            fs::canonicalize(&c.home)
                .ok()
                .map(|h| (c.variant.clone(), h))
        })
        .collect();
    for (v, h) in &j.homes {
        if configured.get(v) != Some(h) {
            return Err("Recovery home differs from configured client".into());
        }
    }
    let _locks = lock_homes(&j.homes)?;
    check_offline(clients, &j.homes)?;
    // Preflight every object before restoring anything; never clobber post-sync edits.
    for (v, h) in &j.homes {
        let current = db_digest(&open(h, false)?)?;
        if current != j.before_db[v] && j.after_db.get(v) != Some(&current) {
            return Err("Database has subsequent edits; automatic recovery refused".into());
        }
        let backup = j.backups.get(v).ok_or("Missing database backup")?;
        files::plain(backup)?;
        if !backup.starts_with(&dir) {
            return Err("Invalid backup path".into());
        }
        let con = Connection::open_with_flags(backup, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(|e| e.to_string())?;
        if db_digest(&con)? != j.before_db[v] {
            return Err("Database backup is damaged".into());
        }
    }
    for f in &j.files {
        if !j.homes.values().any(|h| f.path.starts_with(h)) && f.path != state.join("links.json") {
            return Err("Invalid recovery asset path".into());
        }
        let current = file_hash(&f.path)?;
        if current != f.before && current != Some(f.after.clone()) {
            return Err(format!("Asset has subsequent edits: {}", f.path.display()));
        }
        if let Some(b) = &f.backup {
            if !b.starts_with(&dir) || file_hash(b)? != f.before {
                return Err("Asset backup is damaged".into());
            }
        }
    }
    j.status = "restoring".into();
    files::atomic_json(&path, &j)?;
    for (v, h) in &j.homes {
        let mut con = open(h, true)?;
        if db_digest(&con)? != j.before_db[v] {
            con.restore(
                rusqlite::MAIN_DB,
                &j.backups[v],
                None::<fn(rusqlite::backup::Progress)>,
            )
            .map_err(|e| e.to_string())?;
        }
    }
    for f in &j.files {
        if let Some(b) = &f.backup {
            files::atomic_bytes(&f.path, &fs::read(b).map_err(|e| e.to_string())?)?;
        } else if f.path.exists() {
            fs::remove_file(&f.path).map_err(|e| e.to_string())?;
            files::sync_dir(f.path.parent().unwrap())?;
        }
    }
    j.status = "restored".into();
    files::atomic_json(&path, &j)?;
    Ok(json!({"status":"restored","run_id":run_id}))
}

#[cfg(test)]
mod switch_retry_tests {
    use super::*;
    #[test]
    fn shutdown_drift_rebuilds_before_writing() {
        let mut attempts = 0;
        let result = retry_before_writes(|| {
            attempts += 1;
            if attempts == 1 {
                Err("Database changed after preview; regenerate the plan".into())
            } else {
                Ok(json!({"status":"committed"}))
            }
        })
        .unwrap();
        assert_eq!(attempts, 2);
        assert_eq!(result["status"], "committed");
    }
    #[test]
    fn drift_after_journal_or_other_errors_never_retry() {
        for error in [
            "Database changed after preview; regenerate the plan. Recovery run: run",
            "Database changed while syncing",
            "File drift",
            "Client is running",
        ] {
            let mut attempts = 0;
            assert_eq!(
                retry_before_writes(|| {
                    attempts += 1;
                    Err(error.into())
                })
                .unwrap_err(),
                error
            );
            assert_eq!(attempts, 1);
        }
    }
    #[test]
    fn continuous_drift_is_bounded() {
        let mut attempts = 0;
        let error = retry_before_writes(|| {
            attempts += 1;
            Err("Database changed after preview; regenerate the plan".into())
        })
        .unwrap_err();
        assert_eq!(attempts, 3);
        assert!(error.contains("账号尚未切换"));
    }
}
