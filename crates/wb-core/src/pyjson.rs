//! 复刻 CPython `json.dumps` 的默认输出，用来算跨实现可比的哈希。
//!
//! 为什么不能直接用 `serde_json`：`plan_id` 是 Python 侧对
//! `json.dumps(body, ensure_ascii=False, sort_keys=True)` 做 SHA-256 得到的。
//! 标准库有两个不可调和的差异 ——
//!
//!  1. **分隔符**。CPython 默认是 `", "` 和 `": "`（都带空格），`serde_json` 一个空格都不给。
//!  2. **浮点**。CPython 用 `repr` 规则：`-4 <= exp < 16` 走定点，否则走科学计数，
//!     且指数至少两位（`1e-05`）。Rust 的 `{:e}` 会把 `1e15` 写成 `1e15`、
//!     `1e-5` 写成 `1e-5`，直接照抄就错。
//!
//! 哈希差一个字节就是完全不同的一串。所以这里按 CPython 的规则自己编码 ——
//! 只要两个实现对同一份数据吐出同一个 `plan_id`，「移植正确」就是**可证的**，
//! 而不是「看起来差不多」。
//!
//! 数据模型用 `BTreeMap` 表达「按键排序」（等价于 `sort_keys=True`），
//! 用 `PyValue::Ordered` 表达「显式保序」（`survey --json` 这类输出用）。
//! 两者不能混用：`plan_id` 只走前者。

use std::collections::BTreeMap;
use std::fmt::Write as _;

use sha2::{Digest, Sha256};

/// 与 Python `json.dumps` 可编码类型一一对应的值。
#[derive(Debug, Clone, PartialEq)]
pub enum PyValue {
    Null,
    Bool(bool),
    /// Python 的 `int`。SQLite 的 INTEGER 列落在这里。
    Int(i64),
    /// Python 的 `float`。
    Float(f64),
    /// Python 的 `str`。
    Str(String),
    /// Python 的 `list`。
    Array(Vec<PyValue>),
    /// Python 的 `dict` + `sort_keys=True`：**按键排序**输出。
    Map(BTreeMap<String, PyValue>),
    /// Python 的 `dict`，但保持插入顺序（不排序）。
    ///
    /// 用于 `survey --json` 这类「形状要对齐 Python 输出」的场景。
    /// 注意 `plan_id` 用的是 `Map`，不是这个。
    Ordered(Vec<(String, PyValue)>),
}

impl PyValue {
    pub fn str(s: impl Into<String>) -> Self {
        PyValue::Str(s.into())
    }

    pub fn int(v: i64) -> Self {
        PyValue::Int(v)
    }

    pub fn float(v: f64) -> Self {
        PyValue::Float(v)
    }

    pub fn array(items: impl IntoIterator<Item = PyValue>) -> Self {
        PyValue::Array(items.into_iter().collect())
    }

    /// 构造一个保序 map。
    pub fn ordered(pairs: impl IntoIterator<Item = (&'static str, PyValue)>) -> Self {
        PyValue::Ordered(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }

    /// 这个值是不是「空容器」—— `dumps_indent` 要靠它决定压不压缩。
    fn is_empty_container(&self) -> bool {
        match self {
            PyValue::Array(items) => items.is_empty(),
            PyValue::Map(m) => m.is_empty(),
            PyValue::Ordered(m) => m.is_empty(),
            _ => false,
        }
    }
}

impl From<&str> for PyValue {
    fn from(s: &str) -> Self {
        PyValue::Str(s.to_string())
    }
}

impl From<String> for PyValue {
    fn from(s: String) -> Self {
        PyValue::Str(s)
    }
}

impl From<bool> for PyValue {
    fn from(v: bool) -> Self {
        PyValue::Bool(v)
    }
}

impl From<i64> for PyValue {
    fn from(v: i64) -> Self {
        PyValue::Int(v)
    }
}

impl From<f64> for PyValue {
    fn from(v: f64) -> Self {
        PyValue::Float(v)
    }
}

impl From<Option<PyValue>> for PyValue {
    fn from(v: Option<PyValue>) -> Self {
        v.unwrap_or(PyValue::Null)
    }
}

/// 一把顺手的小构造器：`PyMap::new().set("a", 1i64).build()`
///
/// 存在的理由：计划体是十几个字段的嵌套结构，直接手写 `BTreeMap::from`
/// 会让「哪个字段漏了」变得很难看出来。
#[derive(Default)]
pub struct PyMap(BTreeMap<String, PyValue>);

impl PyMap {
    pub fn new() -> Self {
        Self(BTreeMap::new())
    }

    pub fn set(mut self, key: &str, value: impl Into<PyValue>) -> Self {
        self.0.insert(key.to_string(), value.into());
        self
    }

    pub fn build(self) -> PyValue {
        PyValue::Map(self.0)
    }
}

// --------------------------------------------------------------------------
// 编码
// --------------------------------------------------------------------------

/// 等价于 Python 的 `json.dumps(v, ensure_ascii=False, sort_keys=True)`。
pub fn dumps(v: &PyValue) -> String {
    let mut out = String::new();
    encode(&mut out, v);
    out
}

/// 等价于 Python 的
/// `json.dumps(v, ensure_ascii=False, sort_keys=True, indent=indent)`。
///
/// `indent` 一旦给定，CPython 会把分隔符切成 `(',', ': ')`，
/// 并且**空容器仍然压成 `{}` / `[]`**（不带换行）—— 这两个细节都是
/// `undo.json` 与记忆块能否逐字节还原的关键，不能想当然。
///
/// 另外注意：本函数不追加结尾换行。Python 的 `json.dump(...)` 也不加，
/// 是调用方另外写 `\n` 的（见 `wb_home_bridge.py` 的记录路径）。
pub fn dumps_indent(v: &PyValue, indent: usize) -> String {
    let mut out = String::new();
    encode_indent(&mut out, v, indent, 0);
    out
}

fn encode(out: &mut String, v: &PyValue) {
    match v {
        PyValue::Null => out.push_str("null"),
        PyValue::Bool(true) => out.push_str("true"),
        PyValue::Bool(false) => out.push_str("false"),
        PyValue::Int(i) => {
            let _ = write!(out, "{i}");
        }
        PyValue::Float(f) => out.push_str(&float_repr(*f)),
        PyValue::Str(s) => write_string(out, s),
        PyValue::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                encode(out, item);
            }
            out.push(']');
        }
        PyValue::Map(m) => {
            out.push('{');
            // BTreeMap 的迭代序就是键的字节序；UTF-8 的字节序与码点序一致，
            // 这正是 Python `sort_keys` 的排序口径。
            for (i, (k, val)) in m.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_string(out, k);
                out.push_str(": ");
                encode(out, val);
            }
            out.push('}');
        }
        PyValue::Ordered(pairs) => {
            out.push('{');
            for (i, (k, val)) in pairs.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_string(out, k);
                out.push_str(": ");
                encode(out, val);
            }
            out.push('}');
        }
    }
}

fn encode_indent(out: &mut String, v: &PyValue, indent: usize, depth: usize) {
    if v.is_empty_container() {
        // 缩进模式下空容器仍是 `{}` / `[]`，不能写成「花括号 + 换行 + 花括号」。
        out.push_str(match v {
            PyValue::Map(_) | PyValue::Ordered(_) => "{}",
            _ => "[]",
        });
        return;
    }
    match v {
        PyValue::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                pad(out, indent, depth + 1);
                encode_indent(out, item, indent, depth + 1);
            }
            pad(out, indent, depth);
            out.push(']');
        }
        PyValue::Map(m) => {
            out.push('{');
            for (i, (k, val)) in m.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                pad(out, indent, depth + 1);
                write_string(out, k);
                out.push_str(": ");
                encode_indent(out, val, indent, depth + 1);
            }
            pad(out, indent, depth);
            out.push('}');
        }
        PyValue::Ordered(pairs) => {
            out.push('{');
            for (i, (k, val)) in pairs.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                pad(out, indent, depth + 1);
                write_string(out, k);
                out.push_str(": ");
                encode_indent(out, val, indent, depth + 1);
            }
            pad(out, indent, depth);
            out.push('}');
        }
        other => encode(out, other),
    }
}

fn pad(out: &mut String, indent: usize, depth: usize) {
    out.push('\n');
    for _ in 0..indent * depth {
        out.push(' ');
    }
}

/// 复刻 CPython 的 `py_encode_basestring`（`ensure_ascii=False`）：
/// 只转义 `"`、`\` 与 C0 控制字符，**非 ASCII 原样输出**。
///
/// 特别注意：`/` 与 DEL(0x7f) 都不转义。顺手「美化」成 `\/` 会让哈希全错。
fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{0008}' => out.push_str("\\b"),
            '\u{000c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

// --------------------------------------------------------------------------
// 浮点
// --------------------------------------------------------------------------

/// 复刻 CPython 的 `float` repr。
///
/// 两边取的「最短可回环十进制」是同一套算法，所以先借 Rust 的 `{:e}`
/// 拿到尾数与指数，再按 CPython 的定点/科学计数切换规则重新排版。
///
/// CPython 的规则（`float_repr_style` 为 short 时）：
/// `-4 <= exp < 16` 走定点，其余走科学计数，指数至少两位且带符号。
pub fn float_repr(f: f64) -> String {
    if f.is_nan() {
        return "NaN".to_string();
    }
    if f.is_infinite() {
        return if f.is_sign_positive() { "Infinity" } else { "-Infinity" }.to_string();
    }
    // 零要特判：{:e} 会给出 "0e0"，而 Python 要的是 "0.0" / "-0.0"。
    if f == 0.0 {
        return if f.is_sign_negative() { "-0.0" } else { "0.0" }.to_string();
    }

    let sci = format!("{f:e}");
    let Some((mant, exp_str)) = sci.split_once('e') else {
        return sci;
    };
    let Ok(exp) = exp_str.parse::<i32>() else {
        return sci;
    };

    let neg = mant.starts_with('-');
    let mant = mant.strip_prefix('-').unwrap_or(mant);
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    if digits.is_empty() {
        return sci;
    }
    let prefix = if neg { "-" } else { "" };

    if (-4..16).contains(&exp) {
        return format!("{prefix}{}", fixed(&digits, exp));
    }

    let (head, rest) = digits.split_at(1);
    if rest.is_empty() {
        format!("{prefix}{head}e{}", exp_part(exp))
    } else {
        format!("{prefix}{head}.{rest}e{}", exp_part(exp))
    }
}

/// 生成 `+05` / `-05` / `+308` 形式的指数 —— Python 至少补到两位。
fn exp_part(exp: i32) -> String {
    if exp < 0 {
        format!("-{:02}", -exp)
    } else {
        format!("+{exp:02}")
    }
}

/// 把 `digits` 加 `exp`（即尾数 `d.ddd` 乘以 10^exp）排成定点形式。
///
/// 例：`fixed("1234", 5)` → `123400.0`；`fixed("1", -4)` → `0.0001`。
fn fixed(digits: &str, exp: i32) -> String {
    if exp < 0 {
        format!("0.{}{}", "0".repeat((-exp - 1) as usize), digits)
    } else {
        let point = exp as usize + 1;
        if point >= digits.len() {
            format!("{}{}.0", digits, "0".repeat(point - digits.len()))
        } else {
            format!("{}.{}", &digits[..point], &digits[point..])
        }
    }
}

// --------------------------------------------------------------------------
// 哈希
// --------------------------------------------------------------------------

/// 等价于 Python 的 `hashlib.sha256(text.encode("utf-8")).hexdigest()`。
pub fn sha256_text(text: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(text.as_bytes());
    let digest = hasher.finalize();
    let mut out = String::with_capacity(64);
    for b in digest {
        let _ = write!(out, "{b:02x}");
    }
    out
}

/// 把值按 CPython 规则编码后取 SHA-256。`plan_id` 就是这么来的。
pub fn hash_json(v: &PyValue) -> String {
    sha256_text(&dumps(v))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn map_sorts_keys_but_ordered_does_not() {
        let sorted = PyMap::new()
            .set("b", 1i64)
            .set("a", 2i64)
            .set("C", 3i64)
            .build();
        assert_eq!(dumps(&sorted), r#"{"C": 3, "a": 2, "b": 1}"#);

        let kept = PyValue::ordered([("b", PyValue::int(1)), ("a", PyValue::int(2))]);
        assert_eq!(dumps(&kept), r#"{"b": 1, "a": 2}"#);
    }

    #[test]
    fn int_and_float_repr_differ() {
        // 1 与 1.0 是两种输出，混了哈希就全错 —— 这条守着类型折换。
        assert_eq!(dumps(&PyValue::int(1)), "1");
        assert_eq!(dumps(&PyValue::float(1.0)), "1.0");
    }

    #[test]
    fn float_repr_follows_cpython_switch_rule() {
        // exp < 16 走定点
        assert_eq!(float_repr(1e15), "1000000000000000.0");
        assert_eq!(float_repr(100.0), "100.0");
        assert_eq!(float_repr(0.0001), "0.0001");
        // exp >= 16 走科学计数，指数至少两位且带符号
        assert_eq!(float_repr(1e16), "1e+16");
        assert_eq!(float_repr(1e-5), "1e-05");
        assert_eq!(float_repr(1e-7), "1e-07");
        assert_eq!(float_repr(5e-324), "5e-324");
        assert_eq!(float_repr(1.7976931348623157e308), "1.7976931348623157e+308");
        // 负零必须保住符号位
        assert_eq!(float_repr(0.0), "0.0");
        assert_eq!(float_repr(-0.0), "-0.0");
        // 非有限值沿用 Python 的写法（json.dumps 会输出这三个字面量）
        assert_eq!(float_repr(f64::NAN), "NaN");
        assert_eq!(float_repr(f64::INFINITY), "Infinity");
        assert_eq!(float_repr(f64::NEG_INFINITY), "-Infinity");
    }

    #[test]
    fn string_escaping_stays_minimal() {
        // ensure_ascii=False：非 ASCII 原样输出，DEL 与 `/` 都不转义
        assert_eq!(dumps(&PyValue::str("中文")), "\"中文\"");
        assert_eq!(dumps(&PyValue::str("a/b")), "\"a/b\"");
        assert_eq!(dumps(&PyValue::str("\u{7f}")), "\"\u{7f}\"");
        // C0 走 \u00xx，具名的走短转义
        assert_eq!(dumps(&PyValue::str("\u{1f}")), r#""\u001f""#);
        assert_eq!(dumps(&PyValue::str("\n\t\r")), r#""\n\t\r""#);
        assert_eq!(dumps(&PyValue::str("\u{8}\u{c}")), r#""\b\f""#);
        assert_eq!(dumps(&PyValue::str("\"\\")), r#""\"\\""#);
    }

    #[test]
    fn indent_keeps_empty_containers_compact() {
        let v = PyMap::new()
            .set("a", PyValue::Map(BTreeMap::new()))
            .set("b", PyValue::Array(vec![]))
            .set("c", PyValue::array([PyValue::int(1)]))
            .build();
        assert_eq!(dumps_indent(&v, 2), "{\n  \"a\": {},\n  \"b\": [],\n  \"c\": [\n    1\n  ]\n}");
    }

    #[test]
    fn indent_does_not_append_trailing_newline() {
        // Python 的 json.dump 也不加；加号是调用方另外写的。
        // 少一个 \n 或多一个 \n 都会让 undo.json 回放时对不上。
        assert_eq!(dumps_indent(&PyValue::array([PyValue::int(1)]), 2), "[\n  1\n]");
    }

    #[test]
    fn sha256_text_matches_known_vectors() {
        assert_eq!(
            sha256_text(""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_text("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        // 中文走 UTF-8 字节，不是码点。
        // 这个值由 CPython 算出（tools/gen_pyjson_golden.py 同一套口径）——
        // 不要手写期望哈希：抄错一次，测试就变成自我确认，反而更危险。
        assert_eq!(
            sha256_text("中"),
            "a567bdaa11367f260f0708391f4d10766b5962f565d9e432f17981a3584fe1e2"
        );
    }
}

