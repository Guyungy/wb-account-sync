use crate::{
    accounts::{Account, Store},
    files::{self, Result},
};
use axum::{
    body::{Body, Bytes},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use futures_util::StreamExt;
use reqwest::RequestBuilder;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::RwLock;

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub listen: String,
    pub api_key: String,
    pub cn_base: String,
    pub ai_base: String,
    pub user_agent: String,
    pub max_in_flight: usize,
    pub cooldown_seconds: i64,
    pub timeout_seconds: u64,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            listen: "127.0.0.1:8789".into(),
            api_key: format!(
                "wb-{}{}",
                uuid::Uuid::new_v4().simple(),
                uuid::Uuid::new_v4().simple()
            ),
            cn_base: "https://copilot.tencent.com".into(),
            ai_base: "https://www.workbuddy.ai".into(),
            user_agent: "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1".into(),
            max_in_flight: 2,
            cooldown_seconds: 60,
            timeout_seconds: 120,
        }
    }
}
#[derive(Default)]
pub struct Pool {
    pub running: HashMap<String, usize>,
    pub cooldown: HashMap<String, i64>,
    pub failures: HashMap<String, usize>,
    pub sticky: HashMap<String, (String, i64)>,
    pub cursor: usize,
    pub completed: u64,
    pub errors: u64,
}
pub struct Gateway {
    pub config: Config,
    pub store: Arc<RwLock<Store>>,
    pub dir: PathBuf,
    pub http: reqwest::Client,
    pub pool: Arc<Mutex<Pool>>,
    refresh: tokio::sync::Mutex<()>,
}
impl Gateway {
    pub fn new(config: Config, store: Arc<RwLock<Store>>, dir: PathBuf) -> Result<Self> {
        if config.api_key.len() < 24
            || config.max_in_flight == 0
            || config.timeout_seconds == 0
            || config.cooldown_seconds <= 0
        {
            return Err("Gateway requires a key of at least 24 characters, positive concurrency and timeout".into());
        }
        for base in [&config.cn_base, &config.ai_base] {
            let url = reqwest::Url::parse(base).map_err(|_| "Invalid upstream URL")?;
            if url.scheme() != "https"
                && !(url.scheme() == "http"
                    && matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1")))
            {
                return Err("Upstreams require HTTPS (except loopback test servers)".into());
            }
            if !url.username().is_empty() || url.password().is_some() {
                return Err("URL credentials are not supported".into());
            }
        }
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(15))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|e| e.to_string())?;
        Ok(Self {
            config,
            store,
            dir,
            http,
            pool: Arc::new(Mutex::new(Pool::default())),
            refresh: tokio::sync::Mutex::new(()),
        })
    }
    pub fn authorized(&self, headers: &HeaderMap) -> bool {
        use subtle::ConstantTimeEq;
        let token = headers
            .get("authorization")
            .and_then(|s| s.to_str().ok())
            .and_then(|s| s.strip_prefix("Bearer "))
            .unwrap_or("");
        token
            .as_bytes()
            .ct_eq(self.config.api_key.as_bytes())
            .into()
    }
    pub fn base<'a>(&'a self, account: &Account) -> &'a str {
        if account.variant == "ai" {
            &self.config.ai_base
        } else {
            &self.config.cn_base
        }
    }
    fn headers(&self, req: RequestBuilder, account: &Account) -> Result<RequestBuilder> {
        let req = req
            .bearer_auth(account.token()?)
            .header("X-User-Id", &account.uid)
            .header("X-Domain", account.domain())
            .header("Origin", format!("https://{}", account.domain()))
            .header("Referer", format!("https://{}/", account.domain()))
            .header("X-CodeBuddy-Request", "1")
            .header("X-Requested-With", "XMLHttpRequest")
            .header("User-Agent", &self.config.user_agent)
            .header(
                "Accept-Language",
                if account.variant == "ai" {
                    "en-US"
                } else {
                    "zh-CN"
                },
            );
        let req = if let Some(id) = account.session["account"]["enterpriseId"]
            .as_str()
            .filter(|s| !s.is_empty())
        {
            req.header("X-Enterprise-Id", id)
        } else {
            req.header("X-No-Enterprise-Id", "1")
        };
        Ok(req)
    }
    pub async fn fresh(&self, account: &Account, force: bool) -> Result<Account> {
        let _lock = self.refresh.lock().await;
        let current = self
            .store
            .read()
            .await
            .accounts
            .get(&account.id)
            .cloned()
            .ok_or("Account was removed")?;
        let expiry_value = &current.session["auth"]["expiresAt"];
        let expiry = expiry_value
            .as_i64()
            .or_else(|| expiry_value.as_str().and_then(|s| s.parse().ok()))
            .unwrap_or(i64::MAX);
        let expiry = if expiry < 100_000_000_000 {
            expiry.saturating_mul(1000)
        } else {
            expiry
        };
        if current.session["auth"]["accessToken"] != account.session["auth"]["accessToken"]
            || (!force && expiry > files::now_ms() + 60_000)
        {
            current.token()?;
            return Ok(current);
        }
        let rt = current.session["auth"]["refreshToken"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or("Account expired; sign in again using OAuth")?;
        let req = self
            .headers(
                self.http.post(format!(
                    "{}/v2/plugin/auth/token/refresh",
                    self.base(&current)
                )),
                &current,
            )?
            .header("X-Refresh-Token", rt)
            .header("X-Auth-Refresh-Source", "plugin")
            .timeout(Duration::from_secs(30));
        let resp = req
            .send()
            .await
            .map_err(|_| "Token refresh transport failed")?;
        if !resp.status().is_success() {
            return Err("Token refresh rejected; sign in again".into());
        }
        let data: Value = resp
            .json()
            .await
            .map_err(|_| "Invalid token refresh response")?;
        let data = data.get("data").unwrap_or(&data);
        let token = data["accessToken"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or("Token refresh returned no access token")?;
        let mut updated = current.clone();
        updated.session["auth"]["accessToken"] = json!(token);
        if let Some(token) = data["refreshToken"].as_str().filter(|s| !s.is_empty()) {
            updated.session["auth"]["refreshToken"] = json!(token);
        }
        if let Some(seconds) = data["expiresIn"]
            .as_i64()
            .filter(|s| *s > 0 && *s < 10 * 365 * 24 * 3600)
        {
            updated.session["auth"]["expiresAt"] = json!(files::now_ms() + seconds * 1000);
        }
        if let Some(domain) = data["domain"].as_str().filter(|s| !s.is_empty()) {
            if !crate::accounts::matches_domain(&updated.variant, domain) {
                return Err("Refresh returned a different account realm".into());
            }
            updated.session["auth"]["domain"] = json!(domain);
        }
        self.store
            .write()
            .await
            .import(updated.clone(), &self.dir)?;
        Ok(updated)
    }
    async fn reserve(&self, key: &str, tried: &BTreeSet<String>) -> Result<(Account, Lease)> {
        let accounts: Vec<_> = self
            .store
            .read()
            .await
            .accounts
            .values()
            .filter(|a| a.enabled && a.token().is_ok() && !tried.contains(&a.id))
            .cloned()
            .collect();
        let mut pool = self.pool.lock().unwrap();
        let now = files::now_ms();
        pool.sticky.retain(|_, (_, at)| *at > now);
        let eligible: Vec<_> = accounts
            .into_iter()
            .filter(|a| {
                pool.cooldown.get(&a.id).copied().unwrap_or(0) <= now
                    && pool.running.get(&a.id).copied().unwrap_or(0) < self.config.max_in_flight
            })
            .collect();
        if eligible.is_empty() {
            return Err("No available accounts; add an OAuth account or wait for cooldown".into());
        }
        let sticky = pool
            .sticky
            .get(key)
            .and_then(|(id, _)| eligible.iter().position(|a| &a.id == id));
        let index = sticky.unwrap_or(pool.cursor % eligible.len());
        pool.cursor = pool.cursor.wrapping_add(1);
        let account = eligible[index].clone();
        *pool.running.entry(account.id.clone()).or_default() += 1;
        pool.sticky
            .insert(key.into(), (account.id.clone(), now + 30 * 60 * 1000));
        Ok((
            account.clone(),
            Lease {
                id: account.id,
                pool: self.pool.clone(),
            },
        ))
    }
    fn failure(&self, id: &str, status: u16) {
        let mut p = self.pool.lock().unwrap();
        p.errors += 1;
        let failures = p.failures.entry(id.into()).or_default();
        *failures += 1;
        let seconds = match status {
            401 | 403 => 300,
            402 => 3600,
            429 => self
                .config
                .cooldown_seconds
                .saturating_mul(1_i64 << (*failures).min(6)),
            _ => {
                if *failures >= 3 {
                    self.config.cooldown_seconds
                } else {
                    5
                }
            }
        };
        p.cooldown
            .insert(id.into(), files::now_ms() + seconds * 1000);
        p.sticky.retain(|_, (member, _)| member != id);
    }
    fn success(&self, id: &str) {
        let mut p = self.pool.lock().unwrap();
        p.completed += 1;
        p.failures.remove(id);
    }
    pub fn status(&self) -> Value {
        let p = self.pool.lock().unwrap();
        json!({"completed":p.completed,"errors":p.errors,"in_flight":p.running,"cooldown_until":p.cooldown})
    }
    pub async fn models(&self, headers: &HeaderMap) -> Response {
        if !self.authorized(headers) {
            return error(401, "Invalid API key");
        }
        let accounts: Vec<_> = self
            .store
            .read()
            .await
            .accounts
            .values()
            .filter(|a| a.enabled && a.token().is_ok())
            .cloned()
            .collect();
        let mut models = BTreeMap::new();
        for a in accounts {
            let Ok(a) = self.fresh(&a, false).await else {
                continue;
            };
            let req = match self.headers(
                self.http.get(format!(
                    "{}/console/enterprises/personal/models",
                    self.base(&a)
                )),
                &a,
            ) {
                Ok(r) => r,
                Err(_) => continue,
            };
            let Ok(resp) = req.timeout(Duration::from_secs(30)).send().await else {
                continue;
            };
            if !resp.status().is_success() {
                continue;
            }
            let Ok(value) = resp.json::<Value>().await else {
                continue;
            };
            if value["code"].as_i64().is_some_and(|n| n != 0 && n != 200) {
                continue;
            }
            let data = value.get("data").unwrap_or(&value);
            let cli: BTreeSet<_> = data["agents"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|a| a["name"] == "cli")
                .flat_map(|a| a["models"].as_array().into_iter().flatten())
                .filter_map(Value::as_str)
                .collect();
            for m in data["models"].as_array().into_iter().flatten() {
                let Some(id) = m["id"].as_str() else {
                    continue;
                };
                if m["disabled"] == true || (!cli.is_empty() && !cli.contains(id)) {
                    continue;
                }
                models.insert(
                    id.to_string(),
                    json!({"id":id,"object":"model","created":0,"owned_by":"workbuddy"}),
                );
            }
        }
        if models.is_empty() {
            return error(
                502,
                "Upstream model discovery failed; check account authorization",
            );
        }
        Json(json!({"object":"list","data":models.values().collect::<Vec<_>>()})).into_response()
    }
    pub async fn chat(self: Arc<Self>, headers: &HeaderMap, mut body: Value) -> Response {
        if !self.authorized(headers) {
            return error(401, "Invalid API key");
        }
        if !body.is_object()
            || body["model"].as_str().is_none_or(|s| s.is_empty())
            || body["messages"].as_array().is_none_or(|m| m.is_empty())
        {
            return error(400, "model and a non-empty messages array are required");
        }
        if body["n"].as_u64().is_some_and(|n| n != 1) {
            return error(400, "This gateway currently supports n=1");
        }
        let streaming = body["stream"] == true;
        let key = body["conversation_id"]
            .as_str()
            .or_else(|| body["conversationId"].as_str())
            .map(str::to_string)
            .unwrap_or_else(|| {
                let messages = body["messages"].as_array().unwrap();
                let first: Vec<_> = messages
                    .iter()
                    .filter(|m| m["role"] == "system" || m["role"] == "user")
                    .take(2)
                    .collect();
                files::hash(&serde_json::to_vec(&first).unwrap())
            });
        body["stream"] = json!(true);
        if let Some(alias) = body
            .as_object_mut()
            .unwrap()
            .remove("max_completion_tokens")
        {
            if body.get("max_tokens").is_none() {
                body["max_tokens"] = alias;
            }
        }
        if body["tool_choice"].is_object() {
            let name = body["tool_choice"]["function"]["name"]
                .as_str()
                .unwrap_or("")
                .to_string();
            body["tool_choice"] = json!(name);
        }
        let request_id = uuid::Uuid::new_v4().simple().to_string();
        let mut tried = BTreeSet::new();
        let mut exhausted_status = 503;
        loop {
            let (a, lease) = match self.reserve(&key, &tried).await {
                Ok(v) => v,
                Err(e) => return error(exhausted_status, &e),
            };
            tried.insert(a.id.clone());
            let a = match self.fresh(&a, false).await {
                Ok(a) => a,
                Err(_) => {
                    self.failure(&a.id, 401);
                    continue;
                }
            };
            let mut response = None;
            let mut account = a.clone();
            for attempt in 0..2 {
                let endpoint = format!("{}/v2/chat/completions", self.base(&account));
                let req = match self.headers(self.http.post(endpoint), &account) {
                    Ok(r) => r,
                    Err(e) => return error(400, &e),
                };
                let req = req
                    .header("Accept", "application/json, text/event-stream")
                    .header("X-Conversation-ID", &key)
                    .header("X-Conversation-Request-ID", &request_id)
                    .header("X-Request-ID", uuid::Uuid::new_v4().simple().to_string())
                    .json(&body);
                let sent = tokio::time::timeout(
                    Duration::from_secs(self.config.timeout_seconds),
                    req.send(),
                )
                .await;
                let resp = match sent {
                    Ok(Ok(r)) => r,
                    _ => {
                        self.failure(&account.id, 502);
                        break;
                    }
                };
                if resp.status() == StatusCode::UNAUTHORIZED && attempt == 0 {
                    if let Ok(fresh) = self.fresh(&account, true).await {
                        account = fresh;
                        continue;
                    }
                }
                response = Some(resp);
                break;
            }
            let Some(resp) = response else {
                continue;
            };
            let status = resp.status();
            if !status.is_success() {
                if matches!(
                    status.as_u16(),
                    401 | 402 | 403 | 429 | 500 | 502 | 503 | 504
                ) {
                    self.failure(&account.id, status.as_u16());
                    continue;
                }
                return error(status.as_u16(), "Upstream rejected the request parameters");
            }
            if !resp
                .headers()
                .get("content-type")
                .is_some_and(|s| s.to_str().unwrap_or("").contains("text/event-stream"))
            {
                self.failure(&account.id, 502);
                return error(502, "Upstream did not return an SSE stream");
            }
            // The upstream can report quota/auth errors inside HTTP 200 SSE.
            // Peek before sending any client headers, so those failures can change account.
            let mut raw_stream = resp.bytes_stream();
            let mut peek = Decoder::default();
            let mut prefix = Vec::new();
            let mut native_failure = None;
            loop {
                let next = tokio::time::timeout(
                    Duration::from_secs(self.config.timeout_seconds),
                    raw_stream.next(),
                )
                .await;
                let bytes = match next {
                    Ok(Some(Ok(b))) => b,
                    Ok(None) => break,
                    _ => {
                        self.failure(&account.id, 502);
                        return error(502, "Upstream stream failed before first event");
                    }
                };
                prefix.extend_from_slice(&bytes);
                if prefix.len() > 4 * 1024 * 1024 {
                    return error(502, "Upstream first event exceeded 4 MB");
                }
                let frames = match peek.push(&bytes) {
                    Ok(f) => f,
                    Err(_) => return error(502, "Invalid upstream SSE"),
                };
                let mut meaningful = false;
                for frame in frames {
                    if frame == "[DONE]" {
                        meaningful = true;
                        break;
                    }
                    let Ok(v) = serde_json::from_str::<Value>(&frame) else {
                        return error(502, "Invalid upstream SSE JSON");
                    };
                    let code = v["code"]
                        .as_i64()
                        .or_else(|| v["error"]["code"].as_i64())
                        .unwrap_or(0);
                    if v["error"].is_object() || (code != 0 && code != 200) {
                        native_failure = Some(match code {
                            6004 | 429 => 429,
                            402 => 402,
                            12153 | 401 => 401,
                            11101 | 400 => 400,
                            _ => 502,
                        });
                        break;
                    }
                    meaningful |= v["choices"].as_array().is_some_and(|c| !c.is_empty())
                        || v["data"]["choices"]
                            .as_array()
                            .is_some_and(|c| !c.is_empty());
                }
                if meaningful || native_failure.is_some() {
                    break;
                }
            }
            if let Some(status) = native_failure {
                if status == 400 {
                    return error(400, "Upstream rejected the request parameters");
                }
                self.failure(&account.id, status);
                exhausted_status = 502;
                continue;
            }
            let mut upstream = Box::pin(
                futures_util::stream::once(async move {
                    Ok::<Bytes, reqwest::Error>(Bytes::from(prefix))
                })
                .chain(raw_stream),
            );
            if streaming {
                let gateway = self.clone();
                let model = body["model"].as_str().unwrap().to_string();
                let events = async_stream::stream! {
                    let _lease=lease;
                    let mut decoder=Decoder::default();let mut saw=false;let mut done=false;let mut finished=false;
                    let mut failure=false;
                    'read: loop {
                        let next=tokio::time::timeout(Duration::from_secs(gateway.config.timeout_seconds),upstream.next()).await;
                        let bytes=match next {Ok(Some(Ok(b)))=>b,Ok(None)=>break,_=>{failure=true;break;}};
                        let frames=match decoder.push(&bytes) {Ok(f)=>f,Err(_)=>{failure=true;break;}};
                        for frame in frames {
                            if frame=="[DONE]" {done=true;break 'read;}
                            let chunk=match normalize(&frame,&model) {Ok(Some(c))=>c,Ok(None)=>continue,Err(_)=>{failure=true;break 'read;}};
                            saw |= chunk["choices"].as_array().is_some_and(|c|!c.is_empty());
                            finished |= chunk["choices"].as_array().is_some_and(|cs|cs.iter().any(|c|c["finish_reason"].as_str().is_some()));
                            yield Ok::<Bytes,std::io::Error>(Bytes::from(format!("data: {}\n\n",chunk)));
                        }
                    }
                    if failure || !saw || !(done || finished) {
                        gateway.failure(&account.id,502);
                        yield Ok(Bytes::from(format!("data: {}\n\n",json!({"error":{"message":"Upstream stream failed or was truncated","type":"upstream_error","code":"stream_incomplete"}}))));
                    } else {gateway.success(&account.id);}
                    yield Ok(Bytes::from_static(b"data: [DONE]\n\n"));
                };
                let mut res = Body::from_stream(events).into_response();
                res.headers_mut()
                    .insert("content-type", "text/event-stream".parse().unwrap());
                res.headers_mut()
                    .insert("cache-control", "no-cache".parse().unwrap());
                res.headers_mut()
                    .insert("x-accel-buffering", "no".parse().unwrap());
                res.headers_mut()
                    .insert("x-request-id", request_id.parse().unwrap());
                return res;
            }
            let mut decoder = Decoder::default();
            let mut aggregate = Aggregate::new(body["model"].as_str().unwrap());
            let mut done = false;
            let mut total = 0usize;
            'read: loop {
                let next = tokio::time::timeout(
                    Duration::from_secs(self.config.timeout_seconds),
                    upstream.next(),
                )
                .await;
                let bytes = match next {
                    Ok(Some(Ok(b))) => b,
                    Ok(None) => break,
                    _ => {
                        self.failure(&account.id, 502);
                        return error(502, "Upstream stream failed");
                    }
                };
                total += bytes.len();
                if total > 64 * 1024 * 1024 {
                    return error(502, "Upstream response exceeded 64 MB");
                }
                let frames = match decoder.push(&bytes) {
                    Ok(f) => f,
                    Err(e) => return error(502, &e),
                };
                for frame in frames {
                    if frame == "[DONE]" {
                        done = true;
                        break 'read;
                    }
                    match normalize(&frame, body["model"].as_str().unwrap()) {
                        Ok(Some(c)) => aggregate.add(&c),
                        Ok(None) => {}
                        Err(_) => {
                            self.failure(&account.id, 502);
                            return error(502, "Upstream returned an error event");
                        }
                    }
                }
            }
            if aggregate.events == 0 || !(done || aggregate.finished) {
                self.failure(&account.id, 502);
                return error(502, "Upstream stream was empty or truncated");
            }
            self.success(&account.id);
            drop(lease);
            let mut res = Json(aggregate.output()).into_response();
            res.headers_mut()
                .insert("x-request-id", request_id.parse().unwrap());
            return res;
        }
    }
}
struct Lease {
    id: String,
    pool: Arc<Mutex<Pool>>,
}
impl Drop for Lease {
    fn drop(&mut self) {
        let mut p = self.pool.lock().unwrap();
        if let Some(n) = p.running.get_mut(&self.id) {
            *n = n.saturating_sub(1);
        }
    }
}
pub fn error(status: u16, message: &str) -> Response {
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY),
        Json(json!({"error":{"message":message,"type":"gateway_error","code":status}})),
    )
        .into_response()
}

#[derive(Default)]
pub struct Decoder {
    buffer: Vec<u8>,
    data: Vec<String>,
}
impl Decoder {
    pub fn push(&mut self, bytes: &[u8]) -> Result<Vec<String>> {
        self.buffer.extend(bytes);
        if self.buffer.len() > 4 * 1024 * 1024 {
            return Err("SSE frame too large".into());
        }
        let mut result = Vec::new();
        while let Some(i) = self.buffer.iter().position(|b| *b == b'\n') {
            let raw: Vec<_> = self.buffer.drain(..=i).collect();
            let line = std::str::from_utf8(&raw[..raw.len() - 1])
                .map_err(|_| "Invalid SSE UTF-8")?
                .trim_end_matches('\r');
            if line.is_empty() {
                if !self.data.is_empty() {
                    result.push(self.data.join("\n"));
                    self.data.clear();
                }
            } else if let Some(data) = line.strip_prefix("data:") {
                self.data
                    .push(data.strip_prefix(' ').unwrap_or(data).into());
            }
            if self.data.iter().map(String::len).sum::<usize>() > 4 * 1024 * 1024 {
                return Err("SSE event too large".into());
            }
        }
        Ok(result)
    }
}
fn only(value: &Value, keys: &[&str]) -> Value {
    let mut map = serde_json::Map::new();
    for k in keys {
        if let Some(v) = value.get(*k) {
            map.insert((*k).into(), v.clone());
        }
    }
    Value::Object(map)
}
pub fn normalize(frame: &str, model: &str) -> Result<Option<Value>> {
    let mut value: Value = serde_json::from_str(frame).map_err(|_| "Invalid SSE JSON")?;
    if value["error"].is_object() || value["code"].as_i64().is_some_and(|n| n != 0 && n != 200) {
        return Err("Upstream error event".into());
    }
    if value["data"]["choices"].is_array() {
        value = value["data"].clone();
    }
    let Some(choices) = value["choices"].as_array() else {
        return Ok(None);
    };
    let choices:Vec<_>=choices.iter().map(|c| {
        let mut delta=only(c.get("delta").or_else(||c.get("message")).unwrap_or(&Value::Null),&["role","content","reasoning_content","refusal","tool_calls","function_call"]);
        if let Some(tools)=delta["tool_calls"].as_array() {
            delta["tool_calls"]=json!(tools.iter().map(|t|{let mut t=only(t,&["index","id","type","function"]);if t["function"].is_object() {t["function"]=only(&t["function"],&["name","arguments"]);}t}).collect::<Vec<_>>());
        }
        json!({"index":c["index"].as_u64().unwrap_or(0),"delta":delta,"finish_reason":c["finish_reason"]})
    }).collect();
    let mut out = json!({"id":value["id"].as_str().unwrap_or("chatcmpl-workbuddy"),"object":"chat.completion.chunk",
        "created":value["created"].as_i64().unwrap_or(files::now_ms()/1000),"model":value["model"].as_str().unwrap_or(model),"choices":choices});
    if value["usage"].is_object() {
        out["usage"] = only(
            &value["usage"],
            &[
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_tokens_details",
                "completion_tokens_details",
            ],
        );
    }
    Ok(Some(out))
}
pub struct Aggregate {
    id: String,
    model: String,
    created: i64,
    content: String,
    reasoning: String,
    tools: BTreeMap<u64, Value>,
    finish: Value,
    usage: Value,
    pub events: usize,
    pub finished: bool,
}
impl Aggregate {
    pub fn new(model: &str) -> Self {
        Self {
            id: "chatcmpl-workbuddy".into(),
            model: model.into(),
            created: files::now_ms() / 1000,
            content: String::new(),
            reasoning: String::new(),
            tools: BTreeMap::new(),
            finish: json!("stop"),
            usage: Value::Null,
            events: 0,
            finished: false,
        }
    }
    pub fn add(&mut self, c: &Value) {
        if c["choices"].as_array().is_some_and(|cs| !cs.is_empty()) {
            self.events += 1;
        }
        self.id = c["id"].as_str().unwrap_or(&self.id).into();
        self.created = c["created"].as_i64().unwrap_or(self.created);
        if c["usage"].is_object() {
            self.usage = c["usage"].clone();
        }
        for choice in c["choices"].as_array().into_iter().flatten() {
            let d = &choice["delta"];
            self.content.push_str(d["content"].as_str().unwrap_or(""));
            self.reasoning
                .push_str(d["reasoning_content"].as_str().unwrap_or(""));
            if choice["finish_reason"].is_string() {
                self.finish = choice["finish_reason"].clone();
                self.finished = true;
            }
            for (i, t) in d["tool_calls"].as_array().into_iter().flatten().enumerate() {
                let entry = self
                    .tools
                    .entry(t["index"].as_u64().unwrap_or(i as u64))
                    .or_insert_with(
                        || json!({"id":"","type":"function","function":{"name":"","arguments":""}}),
                    );
                for k in ["id", "type"] {
                    if t[k].is_string() {
                        entry[k] = t[k].clone();
                    }
                }
                for k in ["name", "arguments"] {
                    if let Some(s) = t["function"][k].as_str() {
                        let old = entry["function"][k].as_str().unwrap_or("");
                        entry["function"][k] = json!(format!("{old}{s}"));
                    }
                }
            }
        }
    }
    pub fn output(self) -> Value {
        let mut message = json!({"role":"assistant","content":if self.content.is_empty() && !self.tools.is_empty() {Value::Null} else {json!(self.content)}});
        if !self.reasoning.is_empty() {
            message["reasoning_content"] = json!(self.reasoning);
        }
        if !self.tools.is_empty() {
            message["tool_calls"] = json!(self.tools.into_values().collect::<Vec<_>>());
        }
        json!({"id":self.id,"object":"chat.completion","created":self.created,"model":self.model,"choices":[{"index":0,"message":message,"finish_reason":self.finish}],"usage":self.usage})
    }
}
