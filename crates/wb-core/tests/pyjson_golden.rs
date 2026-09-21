//! pyjson 的跨实现等价性测试。
//!
//! 夹具 `tests/fixtures/pyjson_golden.json` 的期望值由 **CPython 现场算出**
//! （见 `tools/gen_pyjson_golden.py`），不是手抄的 —— 手抄的期望值一旦抄错，
//! 两边一起错，测试反而是绿的。
//!
//! 这里做的事：把夹具里的 JSON 读成 `PyValue`，按 CPython 规则编码，
//! 逐字节比对。哈希也一起比，因为 `plan_id` 的最终形态是哈希，
//! 编码对了不等于哈希对了（理论上等价，但多一层校验只花几微秒）。
//!
//! 夹具里的 JSON 对象一律折成 `PyValue::Map`（按键排序），
//! 因为 `plan_id` 走的 `sort_keys=True` 就是这条路径。
//! `PyValue::Ordered`（显式保序）的测试在 `src/pyjson.rs` 的单元测试里。

use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use serde_json::Value as Json;
use wb_core::pyjson::{dumps, dumps_indent, sha256_text, PyValue};

fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/pyjson_golden.json")
}

fn load_fixture() -> Json {
    let path = fixture_path();
    let text = fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("读不到夹具 {}: {e}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("夹具不是合法 JSON: {e}"))
}

/// 把 serde_json 的值折成 PyValue。
///
/// 两个要点：
/// - JSON 对象折成 `PyValue::Map`（`BTreeMap`），迭代序即键的排序序 —— 这正是 `sort_keys`。
/// - 数字要区分 int / float。`1` 是 int、`1.0` 是 float，
///   两者的 repr 不同（`1` vs `1.0`），混了就全错。
fn to_pyvalue(v: &Json) -> PyValue {
    match v {
        Json::Null => PyValue::Null,
        Json::Bool(b) => PyValue::Bool(*b),
        Json::Number(n) => {
            if n.is_f64() {
                PyValue::Float(n.as_f64().expect("is_f64 却说不是 f64"))
            } else {
                match n.as_i64() {
                    Some(i) => PyValue::Int(i),
                    // 超出 i64 的整数：真实数据不会出现（SQLite INTEGER 就是 i64），
                    // 这里显式失败而不是静默降级成浮点 —— 静默降级会把哈希悄悄改掉。
                    None => panic!("夹具里有超出 i64 的整数 {n}，请改夹具"),
                }
            }
        }
        Json::String(s) => PyValue::Str(s.clone()),
        Json::Array(items) => PyValue::Array(items.iter().map(to_pyvalue).collect()),
        Json::Object(map) => PyValue::Map(
            map.iter()
                .map(|(k, v)| (k.clone(), to_pyvalue(v)))
                .collect::<BTreeMap<_, _>>(),
        ),
    }
}

fn assert_group(fixture: &Json, group: &str) {
    let cases = fixture
        .get(group)
        .and_then(Json::as_array)
        .unwrap_or_else(|| panic!("夹具里没有 {group} 分组"));

    assert!(!cases.is_empty(), "{group} 分组是空的，等于没测");

    let mut failures: Vec<String> = Vec::new();

    for case in cases {
        let label = case.get("label").and_then(Json::as_str).unwrap_or("<无标签>");
        let raw = case.get("json").and_then(Json::as_str).expect("缺 json 字段");
        let expected = case.get("expected").and_then(Json::as_str).expect("缺 expected 字段");
        let expected_hash = case.get("sha256").and_then(Json::as_str).expect("缺 sha256 字段");

        // 输入也要经 serde_json 再解析一遍：夹具里的 json 字段是**文本**，
        // 这样测的才是「同一段 JSON 文本 → 同一串输出」，
        // 而不是「我构造的对象 → 输出」。
        let parsed: Json = match serde_json::from_str(raw) {
            Ok(v) => v,
            Err(e) => {
                failures.push(format!("[{label}] 输入不是合法 JSON: {e}\n    输入: {raw}"));
                continue;
            }
        };
        let value = to_pyvalue(&parsed);

        let actual = if group == "indent2" {
            let indent = case.get("indent").and_then(Json::as_u64).unwrap_or(2) as usize;
            dumps_indent(&value, indent)
        } else {
            dumps(&value)
        };

        if actual != expected {
            failures.push(format!(
                "[{label}] 编码不一致\n    输入    : {raw}\n    期望(CPython): {expected:?}\n    实际(Rust)   : {actual:?}"
            ));
        }

        let actual_hash = sha256_text(&actual);
        if actual_hash != expected_hash {
            failures.push(format!(
                "[{label}] 哈希不一致\n    期望: {expected_hash}\n    实际: {actual_hash}"
            ));
        }
    }

    if !failures.is_empty() {
        panic!(
            "{} 分组有 {} / {} 例不通过：\n\n{}",
            group,
            failures.len(),
            cases.len(),
            failures.join("\n\n")
        );
    }
}

#[test]
fn compact_matches_cpython_dumps() {
    let fixture = load_fixture();
    assert_group(&fixture, "compact");
}

#[test]
fn indent_matches_cpython_dumps() {
    let fixture = load_fixture();
    assert_group(&fixture, "indent2");
}

/// 夹具自己也要是「最新的」—— 防止我把用例改了却忘了重跑生成器，
/// 导致测试跑的是上一版的期望值。这条与
/// `tests/test_pyjson_golden.py` 里的同名断言互为镜像。
#[test]
fn fixture_covers_critical_boundaries() {
    let fixture = load_fixture();
    let labels: Vec<&str> = fixture["compact"]
        .as_array()
        .expect("compact 应为数组")
        .iter()
        .filter_map(|c| c.get("label").and_then(Json::as_str))
        .collect();

    // 这几条是曾经真出过问题或最容易出问题的边界，必须一直在。
    for must in [
        "float_1e15",
        "float_1e16",
        "float_1e-4",
        "float_1e-5",
        "float_neg_zero",
        "str_c0_1f",
        "str_del_7f",
        "obj_keysort_ascii",
        "obj_plan_body",
    ] {
        assert!(labels.contains(&must), "夹具缺关键用例: {must}");
    }
}
