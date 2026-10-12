use crate::{
    accounts::{self, Account, Store},
    clients::Client,
    files::{self, Result},
    gateway::{self, Gateway},
    sessions,
};
use axum::{
    extract::{ConnectInfo, DefaultBodyLimit, State},
    http::{header, Request},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};
use std::{collections::HashMap, net::SocketAddr, path::PathBuf, sync::Arc};
use tokio::sync::{Mutex, RwLock};

pub type DesktopRouter = Router;
pub async fn serve_gateway(
    listener: tokio::net::TcpListener,
    router: Router,
    shutdown: tokio::sync::oneshot::Receiver<()>,
) -> Result<()> {
    axum::serve(listener, router)
        .with_graceful_shutdown(async {
            let _ = shutdown.await;
        })
        .await
        .map_err(|e| e.to_string())
}
pub fn allowed_login_url(url: &str) -> bool {
    reqwest::Url::parse(url).is_ok_and(|u| {
        u.scheme() == "https"
            && u.host_str().is_some_and(|h| {
                [
                    "codebuddy.cn",
                    "workbuddy.cn",
                    "workbuddy.ai",
                    "tencent.com",
                    "qq.com",
                ]
                .iter()
                .any(|d| h == *d || h.ends_with(&format!(".{d}")))
            })
    })
}

#[derive(Clone)]
struct Login {
    variant: String,
    state: String,
    expires: i64,
    result: Option<Value>,
}
pub struct App {
    pub dir: PathBuf,
    pub admin_token: String,
    pub clients: Vec<Client>,
    pub gateway: Arc<Gateway>,
    operation: Arc<Mutex<()>>,
    plans: Mutex<HashMap<String, sessions::Plan>>,
    logins: Mutex<HashMap<String, Login>>,
}
impl App {
    pub fn is_busy(&self) -> bool {
        self.operation.try_lock().is_err()
    }
    pub fn new(dir: PathBuf, clients: Vec<Client>, config: gateway::Config) -> Result<Arc<Self>> {
        if clients.len() != 2
            || !clients.iter().all(|c| {
                accounts::validate_variant(&c.variant).is_ok()
                    && c.home.is_absolute()
                    && c.auth.is_absolute()
            })
            || clients[0].variant == clients[1].variant
        {
            return Err(
                "Configure two clients (cn and ai) with absolute home and auth paths".into(),
            );
        }
        for c in &clients {
            files::plain(&c.home)?;
            if dir.starts_with(&c.home) {
                return Err("State directory must be outside client homes".into());
            }
        }
        if clients[0].home == clients[1].home {
            return Err("Client homes must be different".into());
        }
        let store = Arc::new(RwLock::new(Store::load(&dir)?));
        Ok(Arc::new(Self {
            admin_token: format!(
                "{}{}",
                uuid::Uuid::new_v4().simple(),
                uuid::Uuid::new_v4().simple()
            ),
            gateway: Arc::new(Gateway::new(config, store, dir.clone())?),
            dir,
            clients,
            operation: Arc::new(Mutex::new(())),
            plans: Mutex::new(HashMap::new()),
            logins: Mutex::new(HashMap::new()),
        }))
    }
}
/// Only inference endpoints listen on a socket. Management is invoked by the desktop host.
pub fn gateway_router(app: Arc<App>) -> Router {
    Router::new()
        .route(
            "/healthz",
            get(|| async { Json(json!({"service":"wb-account-sync-gateway","status":"ok"})) }),
        )
        .route("/v1/models", get(models))
        .route("/v1/chat/completions", post(chat))
        .layer(DefaultBodyLimit::max(16 * 1024 * 1024))
        .layer(middleware::from_fn(security_headers))
        .with_state(app)
}
pub async fn dispatch(app: Arc<App>, path: &str, body: Option<Value>) -> Result<Value> {
    use tower::ServiceExt;
    if !path.starts_with("/api/") || path.contains('?') || path.contains('#') {
        return Err("Unsupported desktop request".into());
    }
    let bytes = if let Some(v) = &body {
        serde_json::to_vec(v).map_err(|e| e.to_string())?
    } else {
        Vec::new()
    };
    let mut req = Request::builder()
        .method(if body.is_some() { "POST" } else { "GET" })
        .uri(path)
        .header("host", "127.0.0.1")
        .header("x-wb-token", &app.admin_token)
        .header("content-type", "application/json")
        .body(axum::body::Body::from(bytes))
        .map_err(|e| e.to_string())?;
    req.extensions_mut()
        .insert(ConnectInfo("127.0.0.1:1".parse::<SocketAddr>().unwrap()));
    let resp = router(app).oneshot(req).await.map_err(|e| e.to_string())?;
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 16 * 1024 * 1024)
        .await
        .map_err(|e| e.to_string())?;
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| "Invalid desktop response")?;
    if !status.is_success() {
        return Err(value["error"]["message"]
            .as_str()
            .unwrap_or("Desktop request failed")
            .into());
    }
    Ok(value)
}
pub fn router(app: Arc<App>) -> Router {
    let management = Router::new()
        .route("/api/state", get(state))
        .route("/api/accounts", get(account_list))
        .route("/api/accounts/import", post(import_accounts))
        .route("/api/accounts/import-local", post(import_local))
        .route("/api/accounts/enabled", post(enable_account))
        .route("/api/accounts/oauth/start", post(oauth_start))
        .route("/api/accounts/oauth/poll", post(oauth_poll))
        .route("/api/accounts/switch", post(switch))
        .route("/api/sessions/plan", post(plan))
        .route("/api/sessions/apply", post(apply))
        .route("/api/sessions/restore", post(restore))
        .route("/api/gateway", get(gateway_settings))
        .route("/api/gateway/models", get(desktop_models))
        .route_layer(middleware::from_fn_with_state(app.clone(), admin_auth));
    Router::new()
        .merge(management)
        .route(
            "/healthz",
            get(|| async { Json(json!({"service":"wb-account-sync-gateway","status":"ok"})) }),
        )
        .route("/v1/models", get(models))
        .route("/v1/chat/completions", post(chat))
        .layer(DefaultBodyLimit::max(16 * 1024 * 1024))
        .layer(middleware::from_fn(security_headers))
        .with_state(app)
}
async fn admin_auth(
    State(app): State<Arc<App>>,
    request: Request<axum::body::Body>,
    next: Next,
) -> Response {
    use subtle::ConstantTimeEq;
    let local = request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .is_some_and(|c| c.0.ip().is_loopback());
    let host = request
        .headers()
        .get("host")
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    let host_url = reqwest::Url::parse(&format!("http://{host}"));
    let host_ok = host_url.as_ref().is_ok_and(|u| {
        matches!(
            u.host_str(),
            Some("localhost" | "127.0.0.1" | "[::1]" | "::1")
        )
    });
    let origin_ok = request
        .headers()
        .get("origin")
        .is_none_or(|o| o.to_str().is_ok_and(|o| o == format!("http://{host}")));
    let given = request
        .headers()
        .get("x-wb-token")
        .and_then(|s| s.to_str().ok())
        .unwrap_or("");
    if !local
        || !host_ok
        || !origin_ok
        || !bool::from(given.as_bytes().ct_eq(app.admin_token.as_bytes()))
    {
        return gateway::error(
            403,
            "Local management requires the current admin token and a loopback connection",
        );
    }
    next.run(request).await
}
async fn security_headers(request: Request<axum::body::Body>, next: Next) -> Response {
    let mut res = next.run(request).await;
    for (key,value) in [("x-content-type-options","nosniff"),("referrer-policy","no-referrer"),("x-frame-options","DENY"),("cache-control","no-store"),("content-security-policy","default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")] {
        res.headers_mut().insert(key,value.parse().unwrap());
    }
    res
}
fn response(result: Result<Value>) -> Response {
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => gateway::error(400, &e),
    }
}
async fn state(State(app): State<Arc<App>>) -> Response {
    let clients = app.clients.clone();
    let dir = app.dir.clone();
    response(tokio::task::spawn_blocking(move || {
        let clients=clients.iter().map(Client::public).collect::<Result<Vec<_>>>()?;
        Ok(json!({"clients":clients,"all_stopped":clients.iter().all(|c|c["running"]==false),"pending_runs":sessions::pending(&dir)?.iter().map(|j|json!({"run_id":j.run_id,"status":j.status})).collect::<Vec<_>>()}))
    }).await.unwrap_or_else(|_|Err("Client status worker failed".into())))
}
async fn account_list(State(app): State<Arc<App>>) -> Json<Value> {
    Json(
        json!({"accounts":app.gateway.store.read().await.accounts.values().map(Account::public).collect::<Vec<_>>(),"pool":app.gateway.status()}),
    )
}
async fn import_local(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Response {
    let variant = body["variant"].as_str().unwrap_or("cn");
    let Some(client) = app.clients.iter().find(|c| c.variant == variant) else {
        return gateway::error(400, "Unknown client");
    };
    let result =
        files::read_json(&client.auth).and_then(|session| Account::from_session(variant, session));
    match result {
        Ok(account) => response(app.gateway.store.write().await.import(account, &app.dir)),
        Err(e) => gateway::error(400, &e),
    }
}
fn imported(item: &Value, default_variant: &str) -> Result<Account> {
    let variant = item["variant"].as_str().unwrap_or(default_variant);
    let session = if item["session"].is_object() {
        item["session"].clone()
    } else if item["auth"]["accessToken"].is_string() || item["auth"]["accessToken"].is_object() {
        item.clone()
    } else {
        let mut profile = item["profile_raw"].as_object().cloned().unwrap_or_default();
        for key in ["uid", "nickname", "enterpriseId", "enterpriseName", "email"] {
            if !item[key].is_null() {
                profile.insert(key.into(), item[key].clone());
            }
        }
        let mut auth = item["auth_raw"]["auth"]
            .as_object()
            .or_else(|| item["auth_raw"].as_object())
            .cloned()
            .unwrap_or_default();
        for (dest, src) in [
            ("accessToken", "access_token"),
            ("refreshToken", "refresh_token"),
            ("domain", "domain"),
            ("expiresAt", "expiresAt"),
        ] {
            if !item[src].is_null() {
                auth.insert(dest.into(), item[src].clone());
            }
        }
        if !auth.contains_key("accessToken") {
            auth.insert("accessToken".into(), item["accessToken"].clone());
        }
        if !auth.contains_key("refreshToken") {
            auth.insert("refreshToken".into(), item["refreshToken"].clone());
        }
        json!({"account":profile,"auth":auth})
    };
    Account::from_session(variant, session)
}
async fn import_accounts(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Response {
    let default = body["variant"].as_str().unwrap_or("cn");
    let payload = body.get("data").unwrap_or(&body);
    let items = payload
        .as_array()
        .or_else(|| payload["accounts"].as_array())
        .cloned()
        .unwrap_or_else(|| vec![payload.clone()]);
    let accounts = match items
        .iter()
        .map(|v| imported(v, default))
        .collect::<Result<Vec<_>>>()
    {
        Ok(v) => v,
        Err(e) => return gateway::error(400, &e),
    };
    let mut store = app.gateway.store.write().await;
    let before = store.accounts.clone();
    let mut public = Vec::new();
    for a in accounts {
        public.push(a.public());
        store.accounts.insert(a.id.clone(), a);
    }
    if let Err(e) = store.save(&app.dir) {
        store.accounts = before;
        return gateway::error(400, &e);
    }
    Json(json!({"accounts":public})).into_response()
}
async fn enable_account(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Response {
    let Some(id) = body["id"].as_str() else {
        return gateway::error(400, "Account ID required");
    };
    let Some(enabled) = body["enabled"].as_bool() else {
        return gateway::error(400, "enabled must be boolean");
    };
    let mut store = app.gateway.store.write().await;
    let Some(account) = store.accounts.get(id).cloned() else {
        return gateway::error(404, "Account not found");
    };
    let mut account = account;
    account.enabled = enabled;
    response(store.import(account, &app.dir))
}
fn oauth_base(v: &str) -> &'static str {
    if v == "ai" {
        "https://www.workbuddy.ai"
    } else {
        "https://www.codebuddy.cn"
    }
}
async fn oauth_start(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Response {
    let variant = body["variant"].as_str().unwrap_or("cn");
    if let Err(e) = accounts::validate_variant(variant) {
        return gateway::error(400, &e);
    }
    let platform = if variant == "ai" {
        "workbuddy-ai"
    } else {
        "workbuddy"
    };
    let req = app
        .gateway
        .http
        .post(format!(
            "{}/v2/plugin/auth/state?platform={platform}",
            oauth_base(variant)
        ))
        .json(&json!({}))
        .timeout(std::time::Duration::from_secs(30));
    let Ok(resp) = req.send().await else {
        return gateway::error(502, "OAuth state request failed");
    };
    if !resp.status().is_success() {
        return gateway::error(502, "OAuth state request rejected");
    }
    let Ok(value) = resp.json::<Value>().await else {
        return gateway::error(502, "Invalid OAuth response");
    };
    let Some(state) = value["data"]["state"].as_str().filter(|s| !s.is_empty()) else {
        return gateway::error(502, "OAuth state missing");
    };
    let uri = value["data"]["authUrl"]
        .as_str()
        .or_else(|| value["data"]["auth_url"].as_str())
        .or_else(|| value["data"]["url"].as_str())
        .map(str::to_string)
        .unwrap_or_else(|| format!("{}/login?state={state}", oauth_base(variant)));
    let allowed = allowed_login_url(&uri);
    if !allowed {
        return gateway::error(502, "OAuth returned an unexpected login URL");
    }
    let id = uuid::Uuid::new_v4().to_string();
    let mut logins = app.logins.lock().await;
    let now = files::now_ms();
    logins.retain(|_, l| l.expires > now);
    if logins.len() >= 16 {
        return gateway::error(429, "Too many pending sign-ins");
    }
    logins.insert(
        id.clone(),
        Login {
            variant: variant.into(),
            state: state.into(),
            expires: now + 10 * 60 * 1000,
            result: None,
        },
    );
    Json(json!({"login_id":id,"verification_uri":uri,"expires_in":600})).into_response()
}
async fn oauth_poll(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Response {
    let id = body["login_id"].as_str().unwrap_or("");
    let mut logins = app.logins.lock().await;
    let Some(login) = logins.get_mut(id) else {
        return gateway::error(404, "Sign-in request not found");
    };
    if let Some(v) = &login.result {
        return Json(json!({"done":true,"account":v})).into_response();
    }
    if login.expires < files::now_ms() {
        return gateway::error(400, "Sign-in expired");
    }
    let url = format!("{}/v2/plugin/auth/token", oauth_base(&login.variant));
    let Ok(resp) = app
        .gateway
        .http
        .get(url)
        .query(&[("state", &login.state)])
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
    else {
        return gateway::error(502, "OAuth polling failed");
    };
    let Ok(value) = resp.json::<Value>().await else {
        return gateway::error(502, "Invalid OAuth token response");
    };
    if value["data"]["accessToken"]
        .as_str()
        .is_none_or(|s| s.is_empty())
    {
        return Json(json!({"done":false})).into_response();
    }
    let mut auth = value["data"].clone();
    if let Some(expiry) = auth["expiresIn"].as_i64() {
        if auth["expiresAt"].is_null() {
            auth["expiresAt"] = json!(files::now_ms() + expiry.saturating_mul(1000));
        }
    }
    let url = format!("{}/v2/plugin/login/account", oauth_base(&login.variant));
    let Ok(resp) = app
        .gateway
        .http
        .get(url)
        .query(&[("state", &login.state)])
        .bearer_auth(auth["accessToken"].as_str().unwrap())
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
    else {
        return gateway::error(502, "OAuth profile request failed");
    };
    let Ok(profile) = resp.json::<Value>().await else {
        return gateway::error(502, "Invalid OAuth profile response");
    };
    let account = match Account::from_session(
        &login.variant,
        json!({"account":profile["data"],"auth":auth}),
    ) {
        Ok(a) => a,
        Err(e) => return gateway::error(502, &e),
    };
    match app.gateway.store.write().await.import(account, &app.dir) {
        Ok(account) => {
            login.result = Some(account.clone());
            Json(json!({"done":true,"account":account})).into_response()
        }
        Err(e) => gateway::error(400, &e),
    }
}
async fn targets(app: &App, body: &Value) -> Result<Vec<Account>> {
    let ids = body["account_ids"]
        .as_array()
        .ok_or("account_ids array required")?;
    let store = app.gateway.store.read().await;
    ids.iter()
        .map(|id| {
            store
                .accounts
                .get(id.as_str().unwrap_or(""))
                .cloned()
                .ok_or("Target account not found".into())
        })
        .collect()
}
async fn plan(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Response {
    let targets = match targets(&app, &body).await {
        Ok(v) => v,
        Err(e) => return gateway::error(400, &e),
    };
    let Ok(_guard) = app.operation.try_lock() else {
        return gateway::error(409, "Another operation is running");
    };
    let clients = app.clients.clone();
    let dir = app.dir.clone();
    match tokio::task::spawn_blocking(move || sessions::build(&clients, &targets, &dir)).await {
        Ok(Ok(plan)) => {
            let view = plan.public();
            let mut plans = app.plans.lock().await;
            plans.clear();
            plans.insert(plan.plan_id.clone(), plan);
            Json(view).into_response()
        }
        Ok(Err(e)) => gateway::error(400, &e),
        Err(_) => gateway::error(500, "Plan worker failed"),
    }
}
async fn apply(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Response {
    let id = body["plan_id"].as_str().unwrap_or("").to_string();
    let Some(plan) = app.plans.lock().await.get(&id).cloned() else {
        return gateway::error(400, "Plan missing or stale; generate a new preview");
    };
    let Ok(guard) = app.operation.clone().try_lock_owned() else {
        return gateway::error(409, "Another operation is running");
    };
    let clients = app.clients.clone();
    let dir = app.dir.clone();
    response(
        tokio::task::spawn_blocking(move || {
            let _guard = guard;
            sessions::apply(&plan, &id, &clients, &dir)
        })
        .await
        .unwrap_or_else(|_| Err("Sync worker failed; inspect recovery runs".into())),
    )
}
async fn restore(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Response {
    let id = body["run_id"].as_str().unwrap_or("").to_string();
    let Ok(guard) = app.operation.clone().try_lock_owned() else {
        return gateway::error(409, "Another operation is running");
    };
    let clients = app.clients.clone();
    let dir = app.dir.clone();
    response(
        tokio::task::spawn_blocking(move || {
            let _guard = guard;
            sessions::restore(&id, &clients, &dir)
        })
        .await
        .unwrap_or_else(|_| Err("Recovery worker failed".into())),
    )
}
async fn switch(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Response {
    let id = body["id"].as_str().unwrap_or("");
    let Some(account) = app.gateway.store.read().await.accounts.get(id).cloned() else {
        return gateway::error(404, "Account not found");
    };
    let account = if account.token().is_ok() {
        match app.gateway.fresh(&account, false).await {
            Ok(a) => a,
            Err(e) => return gateway::error(400, &e),
        }
    } else {
        account
    };
    let Some(client) = app
        .clients
        .iter()
        .find(|c| c.variant == account.variant)
        .cloned()
    else {
        return gateway::error(400, "Client not configured");
    };
    let Ok(guard) = app.operation.clone().try_lock_owned() else {
        return gateway::error(409, "Another operation is running");
    };
    let clients = app.clients.clone();
    let dir = app.dir.clone();
    let sync_all = body["sync_all"] == true;
    let store = app.gateway.store.clone();
    response(tokio::task::spawn_blocking(move || {
        let _guard=guard;
        if !sessions::pending(&dir)?.is_empty() {return Err("An interrupted sync requires recovery before switching".into());}
        client.quit()?;
        let sync=if sync_all {
            // Closing both clients is part of the explicitly selected "sync all" action.
            for c in &clients {c.quit()?;}
            let plan=sessions::build(&clients,&[account.clone()],&dir)?;
            Some(sessions::apply(&plan,&plan.plan_id,&clients,&dir)?)
        } else {None};
        let _home_lock=if client.home.is_dir() {Some(files::Lock::acquire(&client.home.join(".wb-operations.lock"))?)} else {None};
        client.require_stopped()?;
        files::plain(&client.auth)?;
        let backup_dir=dir.join("login-backups").join(uuid::Uuid::new_v4().to_string());files::private_dir(&backup_dir)?;
        let before=if client.auth.is_file() {Some(std::fs::read(&client.auth).map_err(|e|e.to_string())?)} else {None};
        if let Some(bytes)=&before {
            files::atomic_bytes(&backup_dir.join("auth.json"),bytes)?;
            if let Ok(session)=serde_json::from_slice(bytes) {
                if let Ok(current)=Account::from_session(&client.variant,session) {
                    let mut store=store.blocking_write();
                    if !store.accounts.contains_key(&current.id) {store.import(current,&dir)?;}
                }
            }
        }
        let marker=PathBuf::from(format!("{}.logged-out",client.auth.display()));
        let marker_bytes=if marker.exists() {files::plain(&marker)?;Some(std::fs::read(&marker).map_err(|e|e.to_string())?)} else {None};
        if let Err(e)=accounts::write_login(&client.auth,&account) {
            let rollback=(|| {
                if let Some(before)=before {files::atomic_bytes(&client.auth,&before)?;}
                else if client.auth.exists() {std::fs::remove_file(&client.auth).map_err(|e|e.to_string())?;}
                if let Some(bytes)=marker_bytes {files::atomic_bytes(&marker,&bytes)?;}
                Ok::<_,String>(())
            })();
            return Err(format!("Login switch failed: {e}; rollback: {}",if rollback.is_ok() {"complete"} else {"failed; inspect login-backups"}));
        }
        let launch=client.launch();
        Ok(json!({"status":"switched","uid":account.uid,"variant":account.variant,"launched":launch.is_ok(),"launch_error":launch.err(),"backup":backup_dir,"sync":sync}))
    }).await.unwrap_or_else(|_|Err("Switch worker failed".into())))
}
async fn gateway_settings(State(app): State<Arc<App>>) -> Json<Value> {
    Json(
        json!({"api_key":app.gateway.config.api_key,"max_in_flight":app.gateway.config.max_in_flight,"pool":app.gateway.status()}),
    )
}
async fn models(State(app): State<Arc<App>>, headers: header::HeaderMap) -> Response {
    app.gateway.models(&headers).await
}
async fn desktop_models(State(app): State<Arc<App>>) -> Response {
    let mut headers = header::HeaderMap::new();
    let Ok(key) = format!("Bearer {}", app.gateway.config.api_key).parse() else {
        return gateway::error(500, "Invalid gateway key");
    };
    headers.insert("authorization", key);
    app.gateway.models(&headers).await
}
async fn chat(
    State(app): State<Arc<App>>,
    headers: header::HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    app.gateway.clone().chat(&headers, body).await
}
