use crate::{
    accounts,
    files::{self, Result},
};
use serde_json::{json, Value};
use std::{path::PathBuf, process::Command};

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct Client {
    pub variant: String,
    pub home: PathBuf,
    pub auth: PathBuf,
    pub app: String,
}
impl Client {
    pub fn default_for(v: &str) -> Result<Self> {
        accounts::validate_variant(v)?;
        Ok(Self {
            variant: v.into(),
            home: files::user_home().join(if v == "ai" {
                ".workbuddy-ai"
            } else {
                ".workbuddy"
            }),
            auth: accounts::auth_path(v)?,
            app: if cfg!(target_os = "macos") {
                if v == "ai" {
                    "/Applications/WorkBuddy AI.app"
                } else {
                    "/Applications/WorkBuddy.app"
                }
            } else if cfg!(windows) {
                if v == "ai" {
                    "WorkBuddyAI.exe"
                } else {
                    "WorkBuddy.exe"
                }
            } else if v == "ai" {
                "/usr/bin/workbuddy-ai"
            } else {
                "/usr/bin/workbuddy"
            }
            .into(),
        })
    }
    pub fn processes(&self) -> Result<Vec<u32>> {
        let output = if cfg!(windows) {
            Command::new("tasklist")
                .args(["/FO", "CSV", "/NH"])
                .output()
        } else {
            Command::new("ps").args(["-Ao", "pid=,command="]).output()
        }
        .map_err(|_| "Cannot inspect client processes")?;
        if !output.status.success() {
            return Err("Client process inspection failed".into());
        }
        let text = String::from_utf8_lossy(&output.stdout);
        let mut ids = Vec::new();
        for line in text.lines() {
            if cfg!(windows) {
                let fields: Vec<_> = line.trim_matches('"').split("\",\"").collect();
                let expected = if self.variant == "ai" {
                    "WorkBuddyAI.exe"
                } else {
                    "WorkBuddy.exe"
                };
                if fields.first().is_some_and(|s| {
                    s.eq_ignore_ascii_case(expected)
                        || (self.variant == "ai" && s.eq_ignore_ascii_case("WorkBuddy AI.exe"))
                }) {
                    if let Some(pid) = fields.get(1).and_then(|s| s.parse().ok()) {
                        ids.push(pid);
                    }
                }
            } else {
                let line = line.trim();
                let Some((pid, cmd)) = line.split_once(char::is_whitespace) else {
                    continue;
                };
                // Match executable/bundle, never arbitrary words in a shell command or script.
                let executable = cmd.trim_start();
                let matches = if cfg!(target_os = "macos") {
                    let suffix = if self.variant == "ai" {
                        "WorkBuddy AI.app/Contents/MacOS/"
                    } else {
                        "WorkBuddy.app/Contents/MacOS/"
                    };
                    let configured = if self.app.starts_with("/Applications/") {
                        executable.contains(suffix)
                    } else {
                        executable.starts_with(&format!("{}/Contents/MacOS/", self.app))
                    };
                    executable.starts_with('/')
                        && configured
                        && !executable.contains("/Frameworks/")
                        && (executable.contains("/Contents/MacOS/Electron")
                            || executable.contains("/Contents/MacOS/WorkBuddy"))
                } else {
                    let first = executable.split_whitespace().next().unwrap_or("");
                    let name = std::path::Path::new(first)
                        .file_name()
                        .and_then(|s| s.to_str())
                        .unwrap_or("");
                    first == self.app
                        || (self.app.starts_with("/usr/bin/")
                            && name
                                == if self.variant == "ai" {
                                    "workbuddy-ai"
                                } else {
                                    "workbuddy"
                                })
                };
                if matches {
                    if let Ok(pid) = pid.parse() {
                        ids.push(pid);
                    }
                }
            }
        }
        Ok(ids)
    }
    pub fn require_stopped(&self) -> Result<()> {
        if self.processes()?.is_empty() {
            Ok(())
        } else {
            Err(format!(
                "{} is running; completely exit it first",
                self.variant
            ))
        }
    }
    pub fn quit(&self) -> Result<()> {
        let ids = self.processes()?;
        if ids.is_empty() {
            return Ok(());
        }
        if cfg!(target_os = "macos") {
            let bundle = if self.variant == "ai" {
                "com.workbuddy.workbuddy-ai"
            } else {
                "com.tencent.workbuddy.mac"
            };
            let status = Command::new("osascript")
                .args(["-e", &format!("tell application id \"{bundle}\" to quit")])
                .status()
                .map_err(|_| "Cannot request client exit")?;
            if !status.success() {
                return Err("Client refused the exit request".into());
            }
        } else if cfg!(windows) {
            for pid in ids {
                let status = Command::new("taskkill")
                    .args(["/PID", &pid.to_string(), "/T"])
                    .status()
                    .map_err(|_| "Cannot request client exit")?;
                if !status.success() {
                    return Err("Client exit failed; close it manually".into());
                }
            }
        } else {
            for pid in ids {
                Command::new("kill")
                    .args(["-TERM", &pid.to_string()])
                    .status()
                    .map_err(|_| "Cannot request client exit")?;
            }
        }
        for _ in 0..100 {
            if self.processes()?.is_empty() {
                return Ok(());
            }
            std::thread::sleep(std::time::Duration::from_millis(200));
        }
        Err("Client did not exit within 20 seconds; nothing was written".into())
    }
    pub fn launch(&self) -> Result<()> {
        let status = if cfg!(target_os = "macos") {
            Command::new("open").args(["-a", &self.app]).status()
        } else {
            Command::new(&self.app)
                .spawn()
                .map(|_| std::process::ExitStatus::default())
        }
        .map_err(|_| "Login was written but client launch failed; open WorkBuddy manually")?;
        if !status.success() {
            return Err(
                "Login was written but client launch failed; open WorkBuddy manually".into(),
            );
        }
        Ok(())
    }
    pub fn public(&self) -> Result<Value> {
        let ids = self.processes()?;
        let uid = if self.auth.is_file() {
            files::read_json::<Value>(&self.auth)
                .ok()
                .and_then(|v| v["account"]["uid"].as_str().map(str::to_string))
        } else {
            None
        };
        Ok(
            json!({"variant":self.variant,"display":if self.variant=="ai" {"WorkBuddy AI"} else {"WorkBuddy"},
            "running":!ids.is_empty(),"pids":ids,"home":self.home,"db_exists":self.home.join("workbuddy.db").is_file(),"current_uid":uid}),
        )
    }
}
