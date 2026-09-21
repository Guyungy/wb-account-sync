//! `Home` 与目录扫描的行为测试。
//!
//! 重点在两处**跨实现契约**上，而不是"函数能跑"：
//!
//! 1. **错误文案**。它会被 CLI 的 `--json` 与界面直接展示，等价性测试也会比对，
//!    所以措辞与 `[标签]` 前缀都要跟 Python 逐字对齐。
//! 2. **符号链接口径**。`dir_size` 的结果会进计划正文、决定 `plan_id`，
//!    所以"指向目录的链接不递归""指向文件的链接按目标大小算""坏链接跳过"
//!    这三条必须钉死 —— 它们在 macOS 上都有真实触发场景（home 里有链接）。

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use wb_core::home::{dir_size, human, project_slug_for, tree_manifest, Home};

fn make_home(dir: &Path) -> Home {
    Home::new("WorkBuddy", "wb", dir, "WorkBuddy")
}

fn write(path: &Path, bytes: usize) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, vec![b'x'; bytes]).unwrap();
}

// --------------------------------------------------------------------------
// require_valid：错误文案是契约
// --------------------------------------------------------------------------

#[test]
fn require_valid_reports_missing_directory_with_label_prefix() {
    let tmp = tempfile::tempdir().unwrap();
    let home = make_home(&tmp.path().join("does-not-exist"));

    let err = home.require_valid().unwrap_err();
    assert_eq!(
        err.to_string(),
        format!("[WorkBuddy] 数据目录不存在：{}", home.path.display())
    );
}

#[test]
fn require_valid_reports_missing_database() {
    let tmp = tempfile::tempdir().unwrap();
    let home = make_home(tmp.path());

    // 目录在、库不在 —— 报的必须是"缺少数据库"，不能是"目录不存在"。
    let err = home.require_valid().unwrap_err();
    assert_eq!(
        err.to_string(),
        format!("[WorkBuddy] 缺少数据库：{}", home.db_path().display())
    );
}

#[test]
fn require_valid_passes_when_both_exist() {
    let tmp = tempfile::tempdir().unwrap();
    fs::write(tmp.path().join("workbuddy.db"), b"").unwrap();
    make_home(tmp.path()).require_valid().unwrap();
}

#[test]
fn directory_check_comes_before_database_check() {
    // 目录整个不在时若先报"缺少数据库"，用户会去查一个根本不存在的路径。
    let tmp = tempfile::tempdir().unwrap();
    let home = make_home(&tmp.path().join("nope"));
    assert!(home.require_valid().unwrap_err().to_string().contains("数据目录不存在"));
}

// --------------------------------------------------------------------------
// 账号快照
// --------------------------------------------------------------------------

fn with_snapshot(dir: &Path, body: &str) -> Home {
    let snap = dir.join("storage/skeleton/account-snapshot.json");
    fs::create_dir_all(snap.parent().unwrap()).unwrap();
    fs::write(&snap, body).unwrap();
    make_home(dir)
}

#[test]
fn current_uid_reads_primary_uid() {
    let tmp = tempfile::tempdir().unwrap();
    let home = with_snapshot(
        tmp.path(),
        r#"{"primary": {"uid": "0f1e2d3c", "nickname": "测试账号"}}"#,
    );
    assert_eq!(home.current_uid().unwrap(), "0f1e2d3c");
    assert_eq!(home.nickname(), "测试账号");
}

#[test]
fn missing_snapshot_message_tells_the_user_what_to_do() {
    let tmp = tempfile::tempdir().unwrap();
    let home = make_home(tmp.path());

    let err = home.current_uid().unwrap_err().to_string();
    assert!(err.starts_with(&format!("[WorkBuddy] 缺少账号快照：{}", home.snapshot_path().display())));
    // 光说"缺少"没用，得告诉用户下一步干什么。
    assert!(err.contains("请先启动该客户端并完成登录，再运行本工具。"));
}

#[test]
fn snapshot_without_primary_uid_is_an_error() {
    let tmp = tempfile::tempdir().unwrap();
    let home = with_snapshot(tmp.path(), r#"{"primary": {}}"#);
    assert_eq!(
        home.current_uid().unwrap_err().to_string(),
        "[WorkBuddy] 账号快照中没有 primary.uid"
    );
}

#[test]
fn nil_primary_is_treated_as_absent() {
    // Python 侧是 `(doc.get("primary") or {})`，null 与缺失同义。
    let tmp = tempfile::tempdir().unwrap();
    let home = with_snapshot(tmp.path(), r#"{"primary": null}"#);
    assert_eq!(
        home.current_uid().unwrap_err().to_string(),
        "[WorkBuddy] 账号快照中没有 primary.uid"
    );
}

#[test]
fn nickname_is_empty_string_when_unavailable_never_an_error() {
    // 昵称只用于展示，取不到不该让整条流程失败 —— current_uid 才是不许失败的那个。
    let tmp = tempfile::tempdir().unwrap();

    let no_snapshot = make_home(tmp.path());
    assert_eq!(no_snapshot.nickname(), "");

    let empty = with_snapshot(&tmp.path().join("a"), r#"{"primary": {}}"#);
    assert_eq!(empty.nickname(), "");

    let broken = with_snapshot(&tmp.path().join("b"), "{not json");
    assert_eq!(broken.nickname(), "");
}

#[test]
fn broken_snapshot_is_reported_as_unparsable() {
    let tmp = tempfile::tempdir().unwrap();
    let home = with_snapshot(tmp.path(), "{not json");
    let err = home.current_uid().unwrap_err().to_string();
    assert!(err.starts_with("[WorkBuddy] 无法解析账号快照："), "实得：{err}");
}

// --------------------------------------------------------------------------
// dir_size：符号链接口径 —— plan_id 的组成部分
// --------------------------------------------------------------------------

#[test]
fn dir_size_sums_files_recursively() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("tree");
    write(&root.join("a.bin"), 100);
    write(&root.join("sub/b.bin"), 250);
    write(&root.join("sub/deep/c.bin"), 7);

    assert_eq!(dir_size(&root), 357);
}

#[test]
fn dir_size_is_zero_for_missing_or_non_directory() {
    let tmp = tempfile::tempdir().unwrap();
    assert_eq!(dir_size(&tmp.path().join("nope")), 0);

    let file = tmp.path().join("plain.txt");
    write(&file, 42);
    // 传进来的是文件而不是目录 → 0，不是 42。
    assert_eq!(dir_size(&file), 0);
}

#[cfg(unix)]
#[test]
fn dir_size_does_not_descend_into_symlinked_directories() {
    // Python 的 os.walk(followlinks=False) 既不递归进链接目录、也不计入它。
    // 若这里改成跟随，approx_bytes 就会比 Python 大，plan_id 直接对不上。
    let tmp = tempfile::tempdir().unwrap();
    let outside = tmp.path().join("outside");
    write(&outside.join("big.bin"), 9999);

    let tree = tmp.path().join("tree");
    write(&tree.join("own.bin"), 10);
    std::os::unix::fs::symlink(&outside, tree.join("link")).unwrap();

    assert_eq!(dir_size(&tree), 10, "链接目录不该被跟随");
}

#[cfg(unix)]
#[test]
fn dir_size_counts_symlinked_files_by_target_size() {
    // Python 用 os.stat（跟随链接）取大小，所以指向文件的链接要按目标算。
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target.bin");
    write(&target, 321);

    let tree = tmp.path().join("tree");
    fs::create_dir_all(&tree).unwrap();
    std::os::unix::fs::symlink(&target, tree.join("link.bin")).unwrap();

    assert_eq!(dir_size(&tree), 321);
}

#[cfg(unix)]
#[test]
fn dir_size_skips_broken_symlinks() {
    let tmp = tempfile::tempdir().unwrap();
    let tree = tmp.path().join("tree");
    fs::create_dir_all(&tree).unwrap();
    write(&tree.join("real.bin"), 5);
    std::os::unix::fs::symlink(tmp.path().join("gone"), tree.join("broken")).unwrap();

    assert_eq!(dir_size(&tree), 5);
}

// --------------------------------------------------------------------------
// tree_manifest
// --------------------------------------------------------------------------

#[test]
fn tree_manifest_keys_are_relative_and_sorted() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("tree");
    write(&root.join("z.bin"), 1);
    write(&root.join("a.bin"), 2);
    write(&root.join("sub/m.bin"), 3);

    let got = tree_manifest(&root);
    let want: BTreeMap<String, u64> = [
        ("a.bin".to_string(), 2),
        ("sub/m.bin".to_string(), 3),
        ("z.bin".to_string(), 1),
    ]
    .into_iter()
    .collect();

    assert_eq!(got, want);
    // 键里不能出现根目录的绝对路径。
    assert!(got.keys().all(|k| !k.contains("tree/") && !k.starts_with('/')));
}

#[test]
fn tree_manifest_is_empty_for_missing_directory() {
    let tmp = tempfile::tempdir().unwrap();
    assert!(tree_manifest(&tmp.path().join("nope")).is_empty());
}

#[test]
fn tree_manifest_is_stable_across_runs() {
    // 遍历序不稳定的实现会让"同一份树两次扫描得到同一个清单"不成立，
    // 核验就失去意义。
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("tree");
    for i in 0..30 {
        write(&root.join(format!("d{}/f{i}.bin", i % 5)), i + 1);
    }
    assert_eq!(tree_manifest(&root), tree_manifest(&root));
}

// --------------------------------------------------------------------------
// project_slug_for
// --------------------------------------------------------------------------

#[test]
fn project_slug_finds_every_bucket_containing_the_conversation() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    write(&root.join("projects/slug-b/conv1.jsonl"), 1);
    write(&root.join("projects/slug-a/conv1.jsonl"), 1);
    write(&root.join("projects/slug-c/conv2.jsonl"), 1);
    // 同名但不是 .jsonl 的文件不算。
    write(&root.join("projects/slug-d/conv1.txt"), 1);

    let home = make_home(root);
    // 同一个会话可能被多个桶收录，所以返回列表；且要排序。
    assert_eq!(project_slug_for(&home, "conv1"), vec!["slug-a", "slug-b"]);
}

#[test]
fn project_slug_is_empty_when_projects_dir_absent() {
    let tmp = tempfile::tempdir().unwrap();
    assert!(project_slug_for(&make_home(tmp.path()), "conv1").is_empty());
}

// --------------------------------------------------------------------------
// human：只用于展示，不进哈希
// --------------------------------------------------------------------------

#[test]
fn human_uses_1024_units_and_truncates_bytes() {
    assert_eq!(human(0.0), "0B");
    assert_eq!(human(999.0), "999B");
    // B 不带小数，且向零截断
    assert_eq!(human(1023.9), "1023B");
    // 1024 是边界：进到 KB
    assert_eq!(human(1024.0), "1.0KB");
    assert_eq!(human(1536.0), "1.5KB");
    assert_eq!(human(1048576.0), "1.0MB");
    assert_eq!(human(1073741824.0), "1.0GB");
}

#[test]
fn human_falls_back_to_gb_beyond_gb() {
    // 超出 GB 的量级不再向上进位，仍以 GB 表示 —— 与 Python 的循环结构一致。
    assert_eq!(human(1099511627776.0), "1024.0GB");
}

// 说明：这里只断言"算法结构"（单位、边界、截断方式），**没有**断言从
// CPython 抄来的精确字符串。精确对账放在 CLI/调查阶段 —— 那时会有
// survey 的跨实现比对兜住它。手抄 CPython 的期望值是本仓库明令禁止的做法
// （抄错一次会让两边一起错，测试反而是绿的）。
