use serde_json::{json, Value};
use wb_server::{
    accounts::{write_login, Account, Store},
    files,
};

#[test]
fn encrypted_envelopes_roundtrip_and_logout_marker_is_removed() {
    let tmp = tempfile::tempdir_in(std::fs::canonicalize(std::env::temp_dir()).unwrap()).unwrap();
    let path = tmp.path().join("workbuddy-desktop.info");
    let marker = tmp.path().join("workbuddy-desktop.info.logged-out");
    files::atomic_json(&path,&json!({"unknown":"preserved","account":{"uid":"old"},"auth":{},"allAccounts":[{"uid":"old"}]})).unwrap();
    std::fs::write(&marker, b"logged out").unwrap();
    let envelope = json!({"$wbEncrypted":1,"envelope":{"wrapped":"opaque"}});
    let a=Account::from_session("cn",json!({"account":{"uid":"new","nickname":{"$wbEncrypted":1,"envelope":"name"}},"auth":{"accessToken":envelope,"refreshToken":envelope}})).unwrap();
    assert!(a.token().is_err());
    write_login(&path, &a).unwrap();
    let doc: Value = files::read_json(&path).unwrap();
    assert_eq!(doc["auth"]["accessToken"], envelope);
    assert_eq!(doc["unknown"], "preserved");
    assert!(!marker.exists());
    assert_eq!(doc["allAccounts"].as_array().unwrap().len(), 2);
}
#[test]
fn public_accounts_never_include_tokens() {
    let a=Account::from_session("ai",json!({"account":{"uid":"u1","nickname":"a"},"auth":{"accessToken":"TOP-SECRET","refreshToken":"MORE-SECRET","domain":"www.workbuddy.ai"}})).unwrap();
    assert!(!a.public().to_string().contains("SECRET"));
}
#[test]
fn invalid_or_cross_realm_auth_is_refused() {
    for session in [
        json!({"account":{"uid":"u1"},"auth":{"accessToken":" "}}),
        json!({"account":{"uid":"u1"},"auth":{"accessToken":"a","domain":"www.workbuddy.ai"}}),
    ] {
        assert!(Account::from_session("cn", session).is_err());
    }
    assert!(Account::from_session("unknown", json!({})).is_err());
}
#[test]
fn saved_credentials_are_private_and_reloadable() {
    let tmp = tempfile::tempdir_in(std::fs::canonicalize(std::env::temp_dir()).unwrap()).unwrap();
    let mut store = Store::default();
    let a = Account::from_session(
        "cn",
        json!({"account":{"uid":"u1"},"auth":{"accessToken":"a"}}),
    )
    .unwrap();
    store.import(a.clone(), tmp.path()).unwrap();
    assert_eq!(Store::load(tmp.path()).unwrap().accounts[&a.id].uid, "u1");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(tmp.path().join("accounts.json"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}
#[cfg(unix)]
#[test]
fn auth_symlink_is_rejected_without_touching_its_target() {
    let tmp = tempfile::tempdir_in(std::fs::canonicalize(std::env::temp_dir()).unwrap()).unwrap();
    let real = tmp.path().join("real");
    std::fs::write(&real, b"original").unwrap();
    let path = tmp.path().join("auth");
    std::os::unix::fs::symlink(&real, &path).unwrap();
    let a = Account::from_session(
        "cn",
        json!({"account":{"uid":"u1"},"auth":{"accessToken":"a"}}),
    )
    .unwrap();
    assert!(write_login(&path, &a).is_err());
    assert_eq!(std::fs::read(real).unwrap(), b"original");
}
#[test]
fn flat_credentials_gain_client_fields_without_inventing_privileges() {
    let tmp = tempfile::tempdir_in(std::fs::canonicalize(std::env::temp_dir()).unwrap()).unwrap();
    let a=Account::from_session("cn",json!({"account":{"id":"u1","nickname":"User","enterpriseId":"company-1"},"auth":{"accessToken":"a","expiresAt":2_000_000_000_i64}})).unwrap();
    let path = tmp.path().join("auth.info");
    write_login(&path, &a).unwrap();
    let doc: Value = files::read_json(&path).unwrap();
    assert_eq!(doc["account"]["uid"], "u1");
    assert_eq!(doc["account"]["enterpriseId"], "company-1");
    assert_eq!(doc["account"]["isAdmin"], false);
    assert_eq!(doc["auth"]["expiresAt"], 2_000_000_000_000_i64);
    assert_eq!(doc["auth"]["accessToken"], "a");
    assert_eq!(doc["account"]["sso"]["domain"], "");
}
