//! `plan_id` 的跨实现等价性测试 —— 本项目的**唯一验收标准**。
//!
//! 夹具由 `tools/gen_plan_golden.py` 用 **CPython 现场算出**，不是手抄的：
//! 它先在同一份固定路径上造出两个合成 home，跑一遍 Python 的 `build_plan`，
//! 把结果（含**被哈希的那串规范编码原文**）落盘。
//! 这里把同一份夹具回放出来，跑 Rust 的实现，逐项比对。
//!
//! ## 为什么存的是"规范编码原文"而不只是哈希
//!
//! 只比哈希的话，失败信息是一串十六进制 —— 你只知道"不一样"，不知道**哪里**不一样。
//! 存下原文，diff 会直接指出是哪个字段、哪个字符。哈希仍然一并比，
//! 因为它是最终产物，多一层校验只花几微秒。
//!
//! ## 关于固定路径
//!
//! `plan_id` 的正文含 `home_a` / `home_b` 的绝对路径，所以夹具固定在
//! `/tmp/wb-plan-parity/`，两边跑在同一份路径上才能对账。
//! 因此**这个测试与 Python 侧的夹具校验不能并发跑**（会互相重建同一目录）——
//! CI 里它们是不同 job、不同机器；本地顺序执行即可。

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use rusqlite::types::Value as SqlValue;
use rusqlite::Connection;
use serde_json::Value as Json;

use wb_core::home::Home;
use wb_core::plan::{build_plan, PlanOptions};
use wb_core::pyjson::{self, PyValue};

fn golden_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/plan_golden.json")
}

fn load_golden() -> Json {
    let path = golden_path();
    let text = fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("读不到夹具 {}: {e}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("夹具不是合法 JSON: {e}"))
}

fn json_to_sql(v: &Json) -> SqlValue {
    match v {
        Json::Null => SqlValue::Null,
        Json::Bool(b) => SqlValue::Integer(i64::from(*b)),
        Json::Number(n) => match n.as_i64() {
            Some(i) if !n.is_f64() => SqlValue::Integer(i),
            _ => SqlValue::Real(n.as_f64().expect("数字既不是 i64 也不是 f64")),
        },
        Json::String(s) => SqlValue::Text(s.clone()),
        other => panic!("夹具里有不支持的 SQL 值：{other}"),
    }
}

/// 把夹具回放到它自己的固定路径上。幂等：先清空再重建。
fn materialize(fixture: &Json) -> (Home, Home) {
    let root_str = fixture["root"].as_str().expect("fixture.root 缺失");
    let root = Path::new(root_str);
    let _ = fs::remove_dir_all(root);
    fs::create_dir_all(root.join("cwd-a")).expect("创建 cwd-a");
    // 注意：cwd-gone **刻意不创建** —— 用来覆盖 cwd_invalid 计数

    let schema: Vec<&str> = fixture["schema"]
        .as_array()
        .expect("fixture.schema 应为数组")
        .iter()
        .map(|s| s.as_str().expect("schema 项应为字符串"))
        .collect();

    let homes = fixture["homes"].as_object().expect("fixture.homes 应为对象");
    for (name, spec) in homes {
        let home = root.join(name);
        fs::create_dir_all(&home).expect("创建 home");

        let con = Connection::open(home.join("workbuddy.db")).expect("打开数据库");
        for ddl in &schema {
            con.execute_batch(ddl).expect("建表失败");
        }
        for (table, rows) in spec["tables"].as_object().expect("tables 应为对象") {
            for row in rows.as_array().expect("表应为数组") {
                let obj = row.as_object().expect("行应为对象");
                // 列序沿用夹具里的顺序（Python 侧 dict 的插入序）。
                let cols: Vec<&String> = obj.keys().collect();
                let placeholders: Vec<&str> = cols.iter().map(|_| "?").collect();
                let sql = format!(
                    "INSERT INTO \"{table}\" ({}) VALUES ({})",
                    cols.iter()
                        .map(|c| format!("\"{c}\""))
                        .collect::<Vec<_>>()
                        .join(", "),
                    placeholders.join(", ")
                );
                let values: Vec<SqlValue> = cols.iter().map(|c| json_to_sql(&obj[*c])).collect();
                con.execute(&sql, rusqlite::params_from_iter(values))
                    .unwrap_or_else(|e| panic!("插入 {table} 失败：{e}"));
            }
        }
        drop(con);

        for item in spec["files"].as_array().expect("files 应为数组") {
            let rel = item[0].as_str().expect("文件路径应为字符串");
            let size = item[1].as_u64().expect("文件大小应为整数") as usize;
            let full = home.join(rel);
            fs::create_dir_all(full.parent().expect("文件应有父目录")).expect("创建目录");
            fs::write(&full, vec![b'x'; size]).expect("写文件");
        }

        let snap = home.join("storage/skeleton/account-snapshot.json");
        fs::create_dir_all(snap.parent().expect("快照应有父目录")).expect("创建快照目录");
        let uid = spec["uid"].as_str().expect("uid 缺失");
        let nickname = spec["nickname"].as_str().expect("nickname 缺失");
        // 手写这份 JSON 而不是引 serde_json 序列化：它只有两个字段，
        // 而"快照长什么样"本身就是被测对象的一部分。
        fs::write(
            &snap,
            format!("{{\"primary\": {{\"uid\": \"{uid}\", \"nickname\": \"{nickname}\"}}}}"),
        )
        .expect("写快照");
    }

    let home_a = fixture["home_a"].as_str().expect("fixture.home_a 缺失");
    let home_b = fixture["home_b"].as_str().expect("fixture.home_b 缺失");
    (
        Home::new("WorkBuddy", "wb", home_a, "WorkBuddy"),
        Home::new("WorkBuddy AI", "wb_ai", home_b, "WorkBuddy AI"),
    )
}

fn options_from(v: &Json) -> PlanOptions {
    let flag = |k: &str| v.get(k).and_then(Json::as_bool).unwrap_or(false);
    PlanOptions {
        include_changes: flag("include_changes"),
        include_skills: flag("include_skills"),
        include_plugins: flag("include_plugins"),
        include_automations: flag("include_automations"),
        include_storage: flag("include_storage"),
        include_connectors: flag("include_connectors"),
        include_claw: flag("include_claw"),
        include_memory: flag("include_memory"),
        overwrite_assets: flag("overwrite_assets"),
    }
}

/// 把夹具里的 `{"a2b": {"sessions": 1}}` 折成可比较的 map。
fn skipped_map(v: &Json) -> BTreeMap<String, BTreeMap<String, i64>> {
    v.as_object()
        .into_iter()
        .flatten()
        .map(|(side, counts)| {
            let inner = counts
                .as_object()
                .into_iter()
                .flatten()
                .map(|(k, n)| (k.clone(), n.as_i64().unwrap_or(0)))
                .collect();
            (side.clone(), inner)
        })
        .collect()
}

fn skipped_of(plan: &wb_core::plan::Plan) -> BTreeMap<String, BTreeMap<String, i64>> {
    plan.skipped
        .iter()
        .map(|(side, counts)| {
            (
                side.clone(),
                counts.iter().map(|(k, v)| (k.clone(), *v as i64)).collect(),
            )
        })
        .collect()
}

/// 一个用例的全部比对。分开两个 `#[test]` 会让它们**并发**重建同一份固定夹具，
/// 所以这里用一个测试遍历所有用例，并把所有失败一次性报出来。
#[test]
fn plan_id_matches_cpython_for_every_case() {
    let golden = load_golden();
    let (a, b) = materialize(&golden["fixture"]);

    let cases = golden["cases"].as_array().expect("cases 应为数组");
    assert!(!cases.is_empty(), "夹具里没有用例，等于没测");

    let mut failures: Vec<String> = Vec::new();

    for case in cases {
        let label = case["label"].as_str().unwrap_or("<无标签>");
        let options = options_from(&case["options"]);

        let mut plan = match build_plan(&a, &b, &options) {
            Ok(p) => p,
            Err(e) => {
                failures.push(format!("[{label}] 构建计划失败：{e}"));
                continue;
            }
        };

        // --- 1. 计划正文（被哈希的那串原文） -------------------------------
        let actual_body = pyjson::dumps(&pyjson::canonicalize(&plan.body()));
        let expected_body = case["expected_body_canonical"].as_str().unwrap_or("");
        if actual_body != expected_body {
            failures.push(format!(
                "[{label}] 计划正文不一致\n    期望: {expected_body}\n    实际: {actual_body}"
            ));
        }

        // --- 2. 条目（同样存了原文） --------------------------------------
        let entries_value = PyValue::Array(plan.entries.iter().map(|e| e.as_value()).collect());
        let actual_entries = pyjson::dumps(&pyjson::canonicalize(&entries_value));
        let expected_entries = case["expected_entries_canonical"].as_str().unwrap_or("");
        if actual_entries != expected_entries {
            failures.push(format!(
                "[{label}] 条目不一致\n    期望: {expected_entries}\n    实际: {actual_entries}"
            ));
        }

        // --- 3. 条目数（正文一致时的冗余校验，便宜且能定位） ---------------
        let expected_count = case["expected_entry_count"].as_u64().unwrap_or(0);
        if plan.entries.len() as u64 != expected_count {
            failures.push(format!(
                "[{label}] 条目数不一致：期望 {expected_count}，实际 {}",
                plan.entries.len()
            ));
        }

        // --- 4. 跳过计数 --------------------------------------------------
        let want_skipped = skipped_map(&case["expected_skipped"]);
        let got_skipped = skipped_of(&plan);
        if got_skipped != want_skipped {
            failures.push(format!(
                "[{label}] 跳过计数不一致：期望 {want_skipped:?}，实际 {got_skipped:?}"
            ));
        }

        // --- 5. plan_id（最终产物） ---------------------------------------
        // 单列一条：正文一致却哈希不一致，只能是我们自己的编码或哈希实现有问题。
        plan.finalize();
        let expected_id = case["expected_plan_id"].as_str().unwrap_or("");
        if plan.plan_id != expected_id {
            failures.push(format!(
                "[{label}] plan_id 不一致\n    期望: {expected_id}\n    实际: {}",
                plan.plan_id
            ));
        }
    }

    assert!(
        failures.is_empty(),
        "{} / {} 个用例不通过：\n\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n\n")
    );
}

/// 夹具必须带齐我们要比对的东西 —— 少一项就等于静默地少测一层。
#[test]
fn fixture_carries_everything_we_compare() {
    let golden = load_golden();
    for case in golden["cases"].as_array().expect("cases 应为数组") {
        let label = case["label"].as_str().unwrap_or("<无标签>");
        for key in [
            "expected_plan_id",
            "expected_body_canonical",
            "expected_entries_canonical",
            "expected_entry_count",
            "expected_skipped",
        ] {
            assert!(case.get(key).is_some(), "[{label}] 夹具缺字段 {key}");
        }
        // 夹具不含环境元数据：路径必须是固定路径，不能随运行环境变化。
        let body = case["expected_body_canonical"].as_str().unwrap();
        assert!(
            body.contains("/tmp/wb-plan-parity/"),
            "[{label}] 正文里看不到固定夹具路径，夹具可能被改成临时目录了"
        );
    }
}
