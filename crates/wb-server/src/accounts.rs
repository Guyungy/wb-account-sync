//! Official auth JSON stays intact, including WorkBuddy 5.6 encrypted envelopes.
use crate::files::{self, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Account {
    pub id: String,
    pub variant: String,
    pub uid: String,
    pub nickname: String,
    pub enabled: bool,
    pub saved_at: i64,
    pub session: Value,
}
impl Account {
    pub fn from_session(variant: &str, session: Value) -> Result<Self> {
        validate_variant(variant)?;
        let uid = session["account"]["uid"]
            .as_str()
            .or_else(|| session["account"]["id"].as_str())
            .ok_or("Auth file has no account UID")?;
        if uid.is_empty() {
            return Err("Account UID is empty".into());
        }
        let token = &session["auth"]["accessToken"];
        if !(token.as_str().is_some_and(|s| !s.trim().is_empty())
            || token.get("$wbEncrypted").is_some())
        {
            return Err("Auth file has no access token".into());
        }
        let domain = session["auth"]["domain"].as_str().unwrap_or("");
        if !domain.is_empty() && !matches_domain(variant, domain) {
            return Err("Account domain and client variant do not match".into());
        }
        Ok(Self {
            id: files::hash(format!("{variant}:{uid}").as_bytes())[..24].into(),
            variant: variant.into(),
            uid: uid.into(),
            nickname: session["account"]["nickname"]
                .as_str()
                .unwrap_or(uid)
                .into(),
            enabled: true,
            saved_at: files::now_ms(),
            session,
        })
    }
    pub fn token(&self) -> Result<&str> {
        self.session["auth"]["accessToken"].as_str().filter(|s| !s.is_empty()).ok_or_else(|| "Encrypted desktop credentials cannot be used by the gateway. Add this account using OAuth.".into())
    }
    pub fn domain(&self) -> &str {
        self.session["auth"]["domain"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or(if self.variant == "ai" {
                "www.workbuddy.ai"
            } else {
                "www.codebuddy.cn"
            })
    }
    pub fn public(&self) -> Value {
        json!({"id":self.id,"uid":self.uid,"nickname":self.nickname,"variant":self.variant,
            "enabled":self.enabled,"saved_at":self.saved_at,"gateway_ready":self.token().is_ok(),
            "expires_at":self.session["auth"]["expiresAt"]})
    }
}
pub fn validate_variant(v: &str) -> Result<()> {
    if matches!(v, "cn" | "ai") {
        Ok(())
    } else {
        Err("variant must be cn or ai".into())
    }
}
pub fn matches_domain(variant: &str, d: &str) -> bool {
    let d = d.trim().to_ascii_lowercase();
    if variant == "ai" {
        matches!(d.as_str(), "www.workbuddy.ai" | "www.codebuddy.ai")
    } else {
        matches!(d.as_str(), "www.codebuddy.cn" | "www.workbuddy.cn")
    }
}
pub fn auth_path(v: &str) -> Result<PathBuf> {
    validate_variant(v)?;
    let home = files::user_home();
    let dir = if cfg!(target_os = "macos") {
        home.join("Library/Application Support")
    } else if cfg!(windows) {
        PathBuf::from(
            std::env::var_os("LOCALAPPDATA")
                .unwrap_or_else(|| home.join("AppData/Local").into_os_string()),
        )
    } else {
        home.join(".local/share")
    };
    Ok(dir
        .join("CodeBuddyExtension/Data/Public/auth")
        .join(if v == "ai" {
            "workbuddy-desktop-ai.info"
        } else {
            "workbuddy-desktop.info"
        }))
}
#[derive(Default, Serialize, Deserialize)]
pub struct Store {
    pub accounts: BTreeMap<String, Account>,
}
impl Store {
    pub fn load(dir: &Path) -> Result<Self> {
        let p = dir.join("accounts.json");
        if p.exists() {
            files::read_json(&p)
        } else {
            Ok(Self::default())
        }
    }
    pub fn save(&self, dir: &Path) -> Result<()> {
        files::atomic_json(&dir.join("accounts.json"), self)
    }
    pub fn import(&mut self, account: Account, dir: &Path) -> Result<Value> {
        let public = account.public();
        let id = account.id.clone();
        let prev = self.accounts.insert(id.clone(), account);
        if let Err(e) = self.save(dir) {
            if let Some(a) = prev {
                self.accounts.insert(id, a);
            } else {
                self.accounts.remove(&id);
            }
            return Err(e);
        }
        Ok(public)
    }
}

/// Writes only login state. The account snapshot is a client-generated cache.
pub fn write_login(path: &Path, target: &Account) -> Result<()> {
    files::plain(path)?;
    let existing: Value = if path.exists() {
        files::read_json(path)?
    } else {
        json!({})
    };
    let mut doc = existing.clone();
    if !doc.is_object() {
        return Err("Unsupported auth file structure".into());
    }
    let mut all = existing
        .get("allAccounts")
        .or_else(|| existing.get("accounts"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    all.retain(|a| a["uid"].as_str().or_else(|| a["id"].as_str()) != Some(&target.uid));
    let mut profile = target.session["account"]
        .as_object()
        .cloned()
        .ok_or("Invalid account profile")?;
    profile.insert("uid".into(), json!(target.uid));
    for (key, value) in [
        ("nickname", json!(target.nickname)),
        (
            "type",
            json!(if profile
                .get("enterpriseId")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.is_empty())
            {
                "enterprise"
            } else {
                "personal"
            }),
        ),
        ("accountType", json!("")),
        ("idp", json!("")),
        ("oneidAccountId", json!("")),
        ("areaInfoComplete", json!(false)),
        ("isCurrentOneIdEnterprise", json!(false)),
        ("isCurrentOneIdPersonal", json!(false)),
        ("isFirstLogin", json!(false)),
        ("isCreator", json!(false)),
        ("isAdmin", json!(false)),
        ("uin", json!("")),
        ("phoneNumber", json!("")),
        ("lastLogin", json!(true)),
        ("pluginEnabled", json!(true)),
        (
            "deployStatus",
            json!({"statusCode":0,"statusMsg":"","detailMsg":""}),
        ),
        ("sso", json!({"domain":"","domainModifiedTimes":0})),
    ] {
        profile.entry(key.to_string()).or_insert(value);
    }
    let mut auth = target.session["auth"]
        .as_object()
        .cloned()
        .ok_or("Invalid account auth")?;
    for (key, value) in [
        ("tokenType", json!("Bearer")),
        ("domain", json!(target.domain())),
        ("scope", json!("openid profile offline_access email")),
        ("notBeforePolicy", json!(0)),
        ("sessionState", json!("")),
        ("lastRefreshTime", json!(files::now_ms())),
    ] {
        auth.entry(key.to_string()).or_insert(value);
    }
    for (at, seconds) in [
        ("expiresAt", "expiresIn"),
        ("refreshExpiresAt", "refreshExpiresIn"),
    ] {
        if let Some(expiry) = auth.get(at).and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_str().and_then(|s| s.parse::<i64>().ok()))
        }) {
            let ms = if expiry < 100_000_000_000 {
                expiry.saturating_mul(1000)
            } else {
                expiry
            };
            auth.insert(at.into(), json!(ms));
            auth.insert(
                seconds.into(),
                json!(((ms - files::now_ms()) / 1000).max(0)),
            );
        } else {
            auth.entry(seconds.to_string()).or_insert(json!(0));
        }
    }
    let profile = Value::Object(profile);
    all.push(profile.clone());
    doc["account"] = profile;
    doc["auth"] = Value::Object(auth);
    doc["accounts"] = json!(all);
    doc["allAccounts"] = doc["accounts"].clone();
    files::atomic_json(path, &doc)?;
    let read: Value = files::read_json(path)?;
    if read["auth"] != doc["auth"] || read["account"] != doc["account"] {
        return Err("Login verification failed".into());
    }
    let marker = PathBuf::from(format!("{}.logged-out", path.display()));
    if marker.exists() {
        files::plain(&marker)?;
        std::fs::remove_file(marker).map_err(|e| e.to_string())?;
    }
    Ok(())
}
