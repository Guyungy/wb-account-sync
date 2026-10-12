#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::Value;
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
};
use tauri::{Emitter, Manager};
use wb_server::{
    clients::Client,
    files::{self, Result},
    gateway::Config,
    server::{self, App},
};

struct Desktop {
    app: Option<Arc<App>>,
    error: Option<String>,
    base: String,
    _lock: Option<files::Lock>,
    shutdown: Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
}
fn initialize(dir: PathBuf) -> Result<Desktop> {
    files::private_dir(&dir)?;
    let dir = std::fs::canonicalize(dir).map_err(|e| e.to_string())?;
    let lock = files::Lock::acquire(&dir.join("server.lock"))?;
    let path = dir.join("gateway.json");
    let config: Config = if path.is_file() {
        files::read_json(&path)?
    } else {
        let config = Config::default();
        files::atomic_json(&path, &config)?;
        config
    };
    let clients_path = dir.join("clients.json");
    let clients: Vec<Client> = if clients_path.is_file() {
        files::read_json(&clients_path)?
    } else {
        vec![Client::default_for("cn")?, Client::default_for("ai")?]
    };
    if clients.len() != 2
        || clients[0].variant == clients[1].variant
        || clients[0].home == clients[1].home
    {
        return Err("Two different client configurations are required".into());
    }
    let listener = std::net::TcpListener::bind(&config.listen)
        .map_err(|e| format!("Cannot start local API on {}: {e}", config.listen))?;
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let addr = listener.local_addr().map_err(|e| e.to_string())?;
    let base = format!(
        "http://{}:{}/v1",
        if addr.is_ipv6() { "[::1]" } else { "127.0.0.1" },
        addr.port()
    );
    let app = App::new(dir, clients, config)?;
    let (tx, rx) = tokio::sync::oneshot::channel();
    let router = server::gateway_router(app.clone());
    tauri::async_runtime::spawn(async move {
        let listener = tokio::net::TcpListener::from_std(listener).expect("local listener");
        let _ = axum_serve(listener, router, rx).await;
    });
    Ok(Desktop {
        app: Some(app),
        error: None,
        base,
        _lock: Some(lock),
        shutdown: Mutex::new(Some(tx)),
    })
}
async fn axum_serve(
    listener: tokio::net::TcpListener,
    router: wb_server::server::DesktopRouter,
    rx: tokio::sync::oneshot::Receiver<()>,
) -> Result<()> {
    wb_server::server::serve_gateway(listener, router, rx).await
}
#[tauri::command]
async fn request(
    window: tauri::Window,
    state: tauri::State<'_, Desktop>,
    path: String,
    body: Option<Value>,
) -> Result<Value> {
    if window.label() != "main" {
        return Err("Unknown desktop window".into());
    }
    let app = state.app.clone().ok_or_else(|| {
        state
            .error
            .clone()
            .unwrap_or_else(|| "Desktop service is unavailable".into())
    })?;
    let mut value = server::dispatch(app, &path, body).await?;
    if path == "/api/gateway" {
        value["base_url"] = Value::String(state.base.clone());
    }
    Ok(value)
}
#[tauri::command]
fn open_login(window: tauri::Window, url: String) -> Result<()> {
    if window.label() != "main" || !server::allowed_login_url(&url) {
        return Err("Unexpected login URL".into());
    }
    let status = if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(&url).status()
    } else if cfg!(windows) {
        std::process::Command::new("rundll32")
            .args(["url.dll,FileProtocolHandler", &url])
            .status()
    } else {
        std::process::Command::new("xdg-open").arg(&url).status()
    }
    .map_err(|e| e.to_string())?;
    if !status.success() {
        return Err("Cannot open official login page".into());
    }
    Ok(())
}
fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let state = app
                .path()
                .app_data_dir()
                .map_err(|e| e.to_string())
                .and_then(initialize)
                .unwrap_or_else(|e| Desktop {
                    app: None,
                    error: Some(e),
                    base: String::new(),
                    _lock: None,
                    shutdown: Mutex::new(None),
                });
            app.manage(state);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![request, open_login])
        .on_window_event(|window, event| {
            let Some(state) = window.try_state::<Desktop>() else {
                return;
            };
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if state.app.as_ref().is_some_and(|a| a.is_busy()) {
                    api.prevent_close();
                    let _ = window.emit(
                        "operation-close-blocked",
                        "正在保存数据，请等待操作完成后退出。",
                    );
                } else if let Some(sender) = state.shutdown.lock().unwrap().take() {
                    let _ = sender.send(());
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("Desktop application initialization failed")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { api, .. } = event {
                let state = app.state::<Desktop>();
                if state.app.as_ref().is_some_and(|a| a.is_busy()) {
                    api.prevent_exit();
                    let _ = app.emit(
                        "operation-close-blocked",
                        "正在保存数据，请等待操作完成后退出。",
                    );
                } else if let Some(sender) = state.shutdown.lock().unwrap().take() {
                    let _ = sender.send(());
                }
            }
        });
}
