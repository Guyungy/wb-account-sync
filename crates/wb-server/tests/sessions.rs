use rusqlite::Connection;
use serde_json::{json, Value};
use std::fs;
use tempfile::TempDir;
use wb_server::{accounts::Account, clients::Client, files, sessions};

struct Fixture {
    tmp: TempDir,
    clients: Vec<Client>,
    targets: Vec<Account>,
}
impl Fixture {
    fn new() -> Self {
        let tmp =
            tempfile::tempdir_in(std::fs::canonicalize(std::env::temp_dir()).unwrap()).unwrap();
        let mut clients = Vec::new();
        for v in ["cn", "ai"] {
            let home = tmp.path().join(v);
            fs::create_dir_all(home.join("projects/work")).unwrap();
            let con = Connection::open(home.join("workbuddy.db")).unwrap();
            con.execute_batch("CREATE TABLE sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,cwd TEXT,title TEXT,created_at INTEGER,updated_at INTEGER,deleted_at INTEGER,status TEXT); CREATE TABLE session_usage(session_id TEXT PRIMARY KEY,credit_json TEXT);").unwrap();
            clients.push(Client {
                variant: v.into(),
                home,
                auth: tmp.path().join(format!("{v}.info")),
                app: "/nonexistent-test-app".into(),
            });
        }
        let targets = vec![
            account("cn", "alice"),
            account("cn", "bob"),
            account("ai", "carol"),
        ];
        Self {
            tmp,
            clients,
            targets,
        }
    }
    fn state(&self) -> std::path::PathBuf {
        let p = self.tmp.path().join("state");
        fs::create_dir_all(&p).unwrap();
        p
    }
    fn source(&self, variant: usize, id: &str, uid: &str) {
        let home = &self.clients[variant].home;
        let con = Connection::open(home.join("workbuddy.db")).unwrap();
        con.execute(
            "INSERT INTO sessions VALUES(?1,?2,?3,'历史',1,1,NULL,'completed')",
            rusqlite::params![id, uid, self.tmp.path().to_str().unwrap()],
        )
        .unwrap();
        con.execute(
            "INSERT INTO session_usage VALUES(?1,'{\"request\":42}')",
            [id],
        )
        .unwrap();
        self.body(variant, id, "你好");
        let tools = home.join("projects/work").join(id).join("tool-results");
        fs::create_dir_all(&tools).unwrap();
        fs::write(tools.join("1.txt"), b"tool output").unwrap();
        fs::create_dir_all(home.join("file-history").join(id)).unwrap();
        fs::write(
            home.join("file-history").join(id).join("a.txt"),
            b"before edit",
        )
        .unwrap();
        fs::create_dir_all(home.join("blobs")).unwrap();
        fs::write(home.join("blobs/abc"), b"attachment").unwrap();
    }
    fn body(&self, variant: usize, id: &str, content: &str) {
        let value = json!({"sessionId":id,"message":{"content":content},"resultPath":format!("/projects/work/{id}/tool-results/1.txt")});
        fs::write(
            self.clients[variant]
                .home
                .join(format!("projects/work/{id}.jsonl")),
            format!("{value}\n"),
        )
        .unwrap();
    }
    fn ids(&self, variant: usize, uid: &str) -> Vec<String> {
        let con = Connection::open(self.clients[variant].home.join("workbuddy.db")).unwrap();
        let result = con
            .prepare("SELECT id FROM sessions WHERE user_id=?1 ORDER BY id")
            .unwrap()
            .query_map([uid], |r| r.get(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        result
    }
    fn apply(&self) -> Value {
        let plan = sessions::build(&self.clients, &self.targets, &self.state()).unwrap();
        sessions::apply(&plan, &plan.plan_id, &self.clients, &self.state()).unwrap()
    }
}
fn account(variant: &str, uid: &str) -> Account {
    Account::from_session(
        variant,
        json!({"account":{"uid":uid,"nickname":uid},"auth":{"accessToken":"test-token"}}),
    )
    .unwrap()
}

#[test]
fn full_sync_preserves_owner_rewrites_ids_and_copies_assets() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    f.source(0, "source-2", "old-account");
    let result = f.apply();
    assert_eq!(result["new_copies"], 5);
    assert_eq!(f.ids(0, "alice").len(), 2);
    assert_eq!(f.ids(0, "old-account"), ["source-2"]);
    assert_eq!(f.ids(0, "bob").len(), 2);
    assert_eq!(f.ids(1, "carol").len(), 2);
    for id in f.ids(1, "carol") {
        let home = &f.clients[1].home;
        let body: Value = serde_json::from_str(
            &fs::read_to_string(home.join(format!("projects/work/{id}.jsonl"))).unwrap(),
        )
        .unwrap();
        assert_eq!(body["sessionId"], id);
        assert!(body["resultPath"].as_str().unwrap().contains(&id));
        assert!(home
            .join(format!("projects/work/{id}/tool-results/1.txt"))
            .is_file());
        assert!(home.join(format!("file-history/{id}/a.txt")).is_file());
    }
    assert_eq!(
        fs::read(f.clients[1].home.join("blobs/abc")).unwrap(),
        b"attachment"
    );
    // Copying content must not fabricate credit consumption on another account.
    let con = Connection::open(f.clients[1].home.join("workbuddy.db")).unwrap();
    assert_eq!(
        con.query_row("SELECT count(*) FROM session_usage", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
}
#[test]
fn repeating_sync_is_idempotent() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    f.apply();
    let plan = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    assert!(plan.rows.is_empty());
    assert!(plan.files.is_empty());
    assert_eq!(f.apply()["status"], "unchanged");
    assert_eq!(f.ids(0, "bob").len(), 1);
}
#[test]
fn edits_from_any_copy_propagate_without_duplicate_rows() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    f.apply();
    let bob = f.ids(0, "bob")[0].clone();
    f.body(0, &bob, "新增内容");
    let result = f.apply();
    assert_eq!(result["updated_copies"], 2);
    assert!(
        fs::read_to_string(f.clients[0].home.join("projects/work/source-1.jsonl"))
            .unwrap()
            .contains("新增内容")
    );
    assert_eq!(f.ids(0, "alice"), ["source-1"]);
    assert_eq!(f.ids(0, "bob"), [bob]);
    assert_eq!(f.apply()["status"], "unchanged");
}
#[test]
fn divergent_edits_are_reported_and_preserved() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    f.apply();
    f.body(0, "source-1", "Alice edit");
    let bob = f.ids(0, "bob")[0].clone();
    f.body(0, &bob, "Bob edit");
    let plan = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    assert_eq!(plan.conflicts.len(), 1);
    assert!(plan.rows.is_empty());
    assert!(
        fs::read_to_string(f.clients[0].home.join(format!("projects/work/{bob}.jsonl")))
            .unwrap()
            .contains("Bob edit")
    );
}
#[test]
fn old_cross_home_copies_are_adopted_without_duplicate_history() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    f.source(1, "source-1", "carol");
    let plan = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    assert_eq!(plan.rows.len(), 1);
    f.apply();
    assert_eq!(f.ids(1, "carol"), ["source-1"]);
    assert_eq!(f.apply()["status"], "unchanged");
}
#[test]
fn deleted_and_missing_bodies_are_not_resurrected() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    fs::remove_file(f.clients[0].home.join("projects/work/source-1.jsonl")).unwrap();
    let plan = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    assert!(plan.rows.is_empty());
    assert!(!plan.skipped.is_empty());
    f.body(0, "source-1", "body");
    f.apply();
    let con = Connection::open(f.clients[0].home.join("workbuddy.db")).unwrap();
    con.execute("UPDATE sessions SET deleted_at=2 WHERE user_id='bob'", [])
        .unwrap();
    let plan = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    assert!(plan.rows.is_empty());
    assert!(!plan.skipped.is_empty());
}
#[test]
fn database_drift_and_wrong_confirmation_reject_writes() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    let plan = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    assert!(sessions::apply(&plan, "bad", &f.clients, &f.state()).is_err());
    let con = Connection::open(f.clients[0].home.join("workbuddy.db")).unwrap();
    con.execute("UPDATE sessions SET title='changed'", [])
        .unwrap();
    assert!(
        sessions::apply(&plan, &plan.plan_id, &f.clients, &f.state())
            .unwrap_err()
            .contains("changed")
    );
    assert!(f.ids(0, "bob").is_empty());
}
#[test]
fn changed_and_new_assets_reject_stale_preview() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    let plan = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    fs::write(
        f.clients[0]
            .home
            .join("projects/work/source-1/tool-results/new.txt"),
        b"new",
    )
    .unwrap();
    assert!(sessions::apply(&plan, &plan.plan_id, &f.clients, &f.state()).is_err());
    assert!(f.ids(0, "bob").is_empty());
}
#[test]
fn restore_covers_database_new_assets_and_links_and_is_idempotent() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    let before =
        sessions::db_digest(&Connection::open(f.clients[0].home.join("workbuddy.db")).unwrap())
            .unwrap();
    let result = f.apply();
    let id = result["run_id"].as_str().unwrap();
    let bob = f.ids(0, "bob")[0].clone();
    sessions::restore(id, &f.clients, &f.state()).unwrap();
    assert!(f.ids(0, "bob").is_empty());
    assert!(!f.clients[0]
        .home
        .join(format!("projects/work/{bob}.jsonl"))
        .exists());
    assert!(!f.state().join("links.json").exists());
    assert_eq!(
        before,
        sessions::db_digest(&Connection::open(f.clients[0].home.join("workbuddy.db")).unwrap())
            .unwrap()
    );
    sessions::restore(id, &f.clients, &f.state()).unwrap();
}
#[test]
fn restore_refuses_post_sync_edits_before_touching_anything() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    let result = f.apply();
    let id = result["run_id"].as_str().unwrap();
    let bob = f.ids(0, "bob")[0].clone();
    f.body(0, &bob, "later user edit");
    assert!(sessions::restore(id, &f.clients, &f.state())
        .unwrap_err()
        .contains("subsequent"));
    assert!(!f.ids(1, "carol").is_empty());
}
#[test]
fn interrupted_commit_is_recoverable_and_blocks_new_runs() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    let result = f.apply();
    let id = result["run_id"].as_str().unwrap();
    let p = f.state().join("session-runs").join(id).join("journal.json");
    let mut j: Value = files::read_json(&p).unwrap();
    j["status"] = json!("prepared");
    files::atomic_json(&p, &j).unwrap();
    assert_eq!(sessions::pending(&f.state()).unwrap().len(), 1);
    let plan = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    assert!(sessions::apply(&plan, &plan.plan_id, &f.clients, &f.state()).is_err());
    sessions::restore(id, &f.clients, &f.state()).unwrap();
    assert!(sessions::pending(&f.state()).unwrap().is_empty());
    assert!(f.ids(0, "bob").is_empty());
}
#[test]
fn updates_can_be_rolled_back_to_previous_copy_content() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    f.apply();
    f.body(0, "source-1", "new message");
    let result = f.apply();
    let id = result["run_id"].as_str().unwrap();
    sessions::restore(id, &f.clients, &f.state()).unwrap();
    let bob = f.ids(0, "bob")[0].clone();
    assert!(
        fs::read_to_string(f.clients[0].home.join(format!("projects/work/{bob}.jsonl")))
            .unwrap()
            .contains("你好")
    );
    assert!(
        fs::read_to_string(f.clients[0].home.join("projects/work/source-1.jsonl"))
            .unwrap()
            .contains("new message")
    );
}
#[cfg(unix)]
#[test]
fn symlink_assets_are_refused() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    std::os::unix::fs::symlink(
        f.tmp.path(),
        f.clients[0]
            .home
            .join("projects/work/source-1/tool-results/link"),
    )
    .unwrap();
    assert!(sessions::build(&f.clients, &f.targets, &f.state()).is_err());
}
#[test]
fn plain_prompt_text_is_not_rewritten_as_an_identifier() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    f.body(0, "source-1", "source-1");
    f.apply();
    let id = &f.ids(0, "bob")[0];
    let body: Value = serde_json::from_str(
        &fs::read_to_string(f.clients[0].home.join(format!("projects/work/{id}.jsonl"))).unwrap(),
    )
    .unwrap();
    assert_eq!(body["sessionId"], id.as_str());
    assert_eq!(body["message"]["content"], "source-1");
}
#[test]
fn operation_lock_prevents_competing_writes() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    let plan = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    let _lock = files::Lock::acquire(&f.clients[0].home.join(".wb-operations.lock")).unwrap();
    assert!(sessions::apply(&plan, &plan.plan_id, &f.clients, &f.state()).is_err());
    assert!(f.ids(0, "bob").is_empty());
}
#[test]
fn backup_tampering_is_refused() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    let result = f.apply();
    let id = result["run_id"].as_str().unwrap();
    let dir = f.state().join("session-runs").join(id);
    let con = Connection::open(dir.join("cn.db")).unwrap();
    con.execute("DELETE FROM sessions", []).unwrap();
    drop(con);
    assert!(sessions::restore(id, &f.clients, &f.state())
        .unwrap_err()
        .contains("damaged"));
    assert!(!f.ids(0, "bob").is_empty());
}
// All fixtures live in TempDir; tests never use real auth files or client homes.
#[test]
fn referenced_buddy_snapshots_and_direct_project_bodies_are_copied() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    for client in &f.clients {
        let con = Connection::open(client.home.join("workbuddy.db")).unwrap();
        con.execute_batch("ALTER TABLE sessions ADD COLUMN buddy_snapshot_id TEXT; CREATE TABLE buddy_snapshots(snapshot_id TEXT PRIMARY KEY,payload TEXT);").unwrap();
    }
    let con = Connection::open(f.clients[0].home.join("workbuddy.db")).unwrap();
    con.execute(
        "INSERT INTO buddy_snapshots VALUES('buddy-1','payload')",
        [],
    )
    .unwrap();
    con.execute("UPDATE sessions SET buddy_snapshot_id='buddy-1'", [])
        .unwrap();
    drop(con);
    fs::rename(
        f.clients[0].home.join("projects/work/source-1.jsonl"),
        f.clients[0].home.join("projects/source-1.jsonl"),
    )
    .unwrap();
    f.apply();
    let target = Connection::open(f.clients[1].home.join("workbuddy.db")).unwrap();
    assert_eq!(
        target
            .query_row(
                "SELECT payload FROM buddy_snapshots WHERE snapshot_id='buddy-1'",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
        "payload"
    );
    let id = &f.ids(1, "carol")[0];
    assert!(f.clients[1]
        .home
        .join(format!("projects/{id}.jsonl"))
        .is_file());
    assert_eq!(f.apply()["status"], "unchanged");
}
#[test]
fn process_exit_at_each_durable_phase_recovers_from_real_partial_writes() {
    for point in ["files_written", "database_committed", "links_written"] {
        let f = Fixture::new();
        f.source(0, "source-1", "alice");
        let plan = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
        files::atomic_json(&f.tmp.path().join("child-plan.json"), &plan).unwrap();
        files::atomic_json(&f.tmp.path().join("child-clients.json"), &f.clients).unwrap();
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["crash_worker", "--exact", "--nocapture"])
            .env("WB_SYNC_TEST_ROOT", f.tmp.path())
            .env("WB_SYNC_TEST_POINT", point)
            .status()
            .unwrap();
        assert_eq!(status.code(), Some(79), "{point}");
        let pending = sessions::pending(&f.state()).unwrap();
        assert_eq!(pending.len(), 1);
        sessions::restore(&pending[0].run_id, &f.clients, &f.state()).unwrap();
        assert_eq!(f.ids(0, "alice"), ["source-1"]);
        assert!(f.ids(0, "bob").is_empty());
        assert!(f.ids(1, "carol").is_empty());
        assert!(!f.state().join("links.json").exists());
        assert!(sessions::pending(&f.state()).unwrap().is_empty());
    }
}
#[test]
fn crash_worker() {
    let Ok(root) = std::env::var("WB_SYNC_TEST_ROOT") else {
        return;
    };
    let root = std::path::PathBuf::from(root);
    // This test helper is only present in the test executable, never in the app.
    let plan: sessions::Plan = files::read_json(&root.join("child-plan.json")).unwrap();
    let clients: Vec<Client> = files::read_json(&root.join("child-clients.json")).unwrap();
    let point = std::env::var("WB_SYNC_TEST_POINT").unwrap();
    sessions::apply_with_checkpoint(
        &plan,
        &plan.plan_id,
        &clients,
        &root.join("state"),
        |phase| {
            if phase == point {
                std::process::exit(79);
            }
        },
    )
    .unwrap();
    panic!("Crash checkpoint was not reached");
}
#[test]
fn wal_database_backup_retains_rows_without_removing_sidecars() {
    let f = Fixture::new();
    let con = Connection::open(f.clients[0].home.join("workbuddy.db")).unwrap();
    con.execute_batch("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;")
        .unwrap();
    f.source(0, "source-1", "alice");
    con.execute("UPDATE sessions SET updated_at=2 WHERE id='source-1'", [])
        .unwrap();
    assert!(f.clients[0].home.join("workbuddy.db-wal").exists());
    let result = f.apply();
    sessions::restore(result["run_id"].as_str().unwrap(), &f.clients, &f.state()).unwrap();
    assert_eq!(f.ids(0, "alice"), ["source-1"]);
    assert!(f.ids(0, "bob").is_empty());
    assert_eq!(
        con.query_row("PRAGMA journal_mode", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "wal"
    );
}
#[test]
fn cross_home_asset_paths_point_to_target_without_changing_user_prose() {
    let f = Fixture::new();
    f.source(0, "source-1", "alice");
    let old = f.clients[0]
        .home
        .join("projects/work/source-1/tool-results/1.txt")
        .to_string_lossy()
        .to_string();
    let value = json!({"sessionId":"source-1","resultPath":old,"folderPath":f.clients[0].home.join("tasks/source-1"),"content":old});
    fs::write(
        f.clients[0].home.join("projects/work/source-1.jsonl"),
        format!("{value}\n"),
    )
    .unwrap();
    f.apply();
    let id = &f.ids(1, "carol")[0];
    let doc: Value = serde_json::from_str(
        &fs::read_to_string(f.clients[1].home.join(format!("projects/work/{id}.jsonl"))).unwrap(),
    )
    .unwrap();
    assert_eq!(
        doc["resultPath"],
        f.clients[1]
            .home
            .join(format!("projects/work/{id}/tool-results/1.txt"))
            .to_string_lossy()
            .as_ref()
    );
    assert_eq!(
        doc["folderPath"],
        f.clients[1]
            .home
            .join(format!("tasks/{id}"))
            .to_string_lossy()
            .as_ref()
    );
    assert_eq!(doc["content"], old);
    assert_eq!(f.apply()["status"], "unchanged");
}

#[test]
fn different_schemas_allow_empty_metadata_and_target_defaults() {
    let f = Fixture::new();
    f.source(0, "source", "alice");
    let source = Connection::open(f.clients[0].home.join("workbuddy.db")).unwrap();
    source.execute_batch("ALTER TABLE sessions ADD COLUMN transport TEXT NOT NULL DEFAULT 'local'; ALTER TABLE sessions ADD COLUMN unread INTEGER NOT NULL DEFAULT 0; ALTER TABLE sessions ADD COLUMN group_title TEXT; UPDATE sessions SET unread=1;").unwrap();
    let target = Connection::open(f.clients[1].home.join("workbuddy.db")).unwrap();
    target
        .execute_batch("ALTER TABLE sessions ADD COLUMN client_flag INTEGER NOT NULL DEFAULT 7;")
        .unwrap();
    f.apply();
    let id = &f.ids(1, "carol")[0];
    assert_eq!(
        target
            .query_row("SELECT client_flag FROM sessions WHERE id=?1", [id], |r| {
                r.get::<_, i64>(0)
            })
            .unwrap(),
        7
    );
    // The projected copy must remain stable during another preview.
    let second = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    assert_eq!(second.public()["updated_copies"], 0);
    sessions::apply(&second, &second.plan_id, &f.clients, &f.state()).unwrap();
    let third = sessions::build(&f.clients, &f.targets, &f.state()).unwrap();
    assert_eq!(third.public()["updated_copies"], 0);
    f.body(0, "source", "新增正文");
    f.apply();
    assert!(fs::read_to_string(f.clients[1].home.join(format!("projects/work/{id}.jsonl"))).unwrap().contains("新增正文"));
    assert_eq!(target.query_row("SELECT client_flag FROM sessions WHERE id=?1", [id], |r| r.get::<_, i64>(0)).unwrap(), 7);
}

#[test]
fn different_schemas_refuse_loss_of_nonempty_metadata() {
    let f = Fixture::new();
    f.source(0, "source", "alice");
    let source = Connection::open(f.clients[0].home.join("workbuddy.db")).unwrap();
    source.execute_batch("ALTER TABLE sessions ADD COLUMN group_title TEXT; UPDATE sessions SET group_title='Important group';").unwrap();
    let error = sessions::build(&f.clients, &f.targets, &f.state())
        .err()
        .expect("incompatible schema must fail");
    assert!(error.contains("group_title"));
    assert!(f.ids(1, "carol").is_empty());
}

#[test]
fn different_schemas_refuse_required_target_column_without_default() {
    let f = Fixture::new();
    f.source(0, "source", "alice");
    let target = Connection::open(f.clients[1].home.join("workbuddy.db")).unwrap();
    target
        .execute_batch("ALTER TABLE sessions ADD COLUMN required_flag TEXT NOT NULL;")
        .unwrap();
    let error = sessions::build(&f.clients, &f.targets, &f.state())
        .err()
        .expect("incompatible schema must fail");
    assert!(error.contains("required_flag"));
}

#[test]
fn database_digest_uses_existing_transaction_snapshot() {
    let f = Fixture::new();
    f.source(0, "source", "alice");
    let mut reader = Connection::open(f.clients[0].home.join("workbuddy.db")).unwrap();
    reader.execute_batch("PRAGMA journal_mode=WAL;").unwrap();
    let initial = sessions::db_digest(&reader).unwrap();
    let tx = reader.transaction().unwrap();
    assert_eq!(sessions::db_digest(&tx).unwrap(), initial);
    let writer = Connection::open(f.clients[0].home.join("workbuddy.db")).unwrap();
    writer.execute("UPDATE sessions SET title='changed' WHERE id='source'", []).unwrap();
    assert_eq!(sessions::db_digest(&tx).unwrap(), initial);
    tx.rollback().unwrap();
    assert_ne!(sessions::db_digest(&reader).unwrap(), initial);
}
