use axum::{
    body::{to_bytes, Body},
    extract::{ConnectInfo, State},
    http::{HeaderMap, Request, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};
use std::{
    net::SocketAddr,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
};
use tower::ServiceExt;
use wb_server::{
    accounts::Account,
    clients::Client,
    files,
    gateway::{normalize, Aggregate, Config, Decoder},
    server::{self, App},
};

#[derive(Default)]
struct Mock {
    requests: Mutex<Vec<(String, Value)>>,
    calls: AtomicUsize,
    refreshes: AtomicUsize,
}
async fn mock_chat(
    State(mock): State<Arc<Mock>>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    assert!(headers.get("x-refresh-token").is_none());
    assert_eq!(body["stream"], true);
    let uid = headers["x-user-id"].to_str().unwrap().to_string();
    mock.requests.lock().unwrap().push((uid, body.clone()));
    let call = mock.calls.fetch_add(1, Ordering::SeqCst);
    match body["model"].as_str().unwrap() {
        "failover" if call == 0 => {
            return (StatusCode::TOO_MANY_REQUESTS, "rate limited").into_response()
        }
        "refresh"
            if !headers["authorization"]
                .to_str()
                .unwrap()
                .contains("rotated") =>
        {
            return StatusCode::UNAUTHORIZED.into_response()
        }
        "invalid" => return (StatusCode::BAD_REQUEST, "invalid parameters").into_response(),
        "empty" => {
            return ([("content-type", "text/event-stream")], "data: [DONE]\n\n").into_response()
        }
        "truncated" => {
            return (
                [("content-type", "text/event-stream")],
                "data: {\"choices\":[{\"delta\":{\"content\":\"unfinished\"}}]}\n\n",
            )
                .into_response()
        }
        "native-error" | "native-failover" if body["model"] == "native-error" || call == 0 => {
            return (
                [("content-type", "text/event-stream")],
                "data: {\"code\":6004,\"msg\":\"secret upstream error\"}\n\n",
            )
                .into_response()
        }
        _ => {}
    }
    let chunks = [
        json!({"id":"chatcmpl-test","model":body["model"],"choices":[{"index":0,"delta":{"role":"assistant","content":"你","reasoning_content":"think"}}],"secret":"not-for-clients"}),
        json!({"choices":[{"index":0,"delta":{"content":"好","tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\"q\":"}}]}}]}),
        json!({"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}),
    ];
    let data = format!(
        ": keepalive\r\n\r\n{}data: [DONE]\r\n\r\n",
        chunks
            .iter()
            .map(|c| format!("data: {c}\r\n\r\n"))
            .collect::<String>()
    );
    // Split in the middle of multibyte UTF-8 and SSE delimiters.
    let bytes: Vec<_> = data
        .as_bytes()
        .chunks(7)
        .map(|b| Ok::<_, std::io::Error>(axum::body::Bytes::copy_from_slice(b)))
        .collect();
    (
        [("content-type", "text/event-stream")],
        Body::from_stream(futures_util::stream::iter(bytes)),
    )
        .into_response()
}
async fn mock_refresh(State(mock): State<Arc<Mock>>, headers: HeaderMap) -> Json<Value> {
    assert!(headers.get("x-refresh-token").is_some());
    mock.refreshes.fetch_add(1, Ordering::SeqCst);
    Json(
        json!({"code":0,"data":{"accessToken":"rotated-token","refreshToken":"rotated-refresh","expiresIn":3600}}),
    )
}
struct Harness {
    _tmp: tempfile::TempDir,
    app: Arc<App>,
    base: String,
    _tasks: Vec<tokio::task::JoinHandle<()>>,
    mock: Arc<Mock>,
}
impl Drop for Harness {
    fn drop(&mut self) {
        for t in &self._tasks {
            t.abort();
        }
    }
}
impl Harness {
    async fn new(two: bool) -> Self {
        let tmp =
            tempfile::tempdir_in(std::fs::canonicalize(std::env::temp_dir()).unwrap()).unwrap();
        let mock = Arc::new(Mock::default());
        let upstream=Router::new().route("/v2/chat/completions",post(mock_chat)).route("/v2/plugin/auth/token/refresh",post(mock_refresh))
            .route("/console/enterprises/personal/models",get(||async {Json(json!({"code":0,"data":{"models":[{"id":"test-model","disabled":false},{"id":"disabled","disabled":true}],"agents":[{"name":"cli","models":["test-model","disabled"]}]}}))})).with_state(mock.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let up = format!("http://{}", listener.local_addr().unwrap());
        let t1 = tokio::spawn(async move {
            axum::serve(listener, upstream).await.unwrap();
        });
        let config = Config {
            api_key: "test-gateway-key-with-enough-entropy".into(),
            cn_base: up.clone(),
            ai_base: up,
            ..Config::default()
        };
        let clients = ["cn", "ai"]
            .iter()
            .map(|v| Client {
                variant: (*v).into(),
                home: tmp.path().join(v),
                auth: tmp.path().join(format!("{v}.info")),
                app: "/nonexistent-test-app".into(),
            })
            .collect();
        let app = App::new(tmp.path().to_path_buf(), clients, config).unwrap();
        for uid in if two {
            vec!["alice", "bob"]
        } else {
            vec!["alice"]
        } {
            let a=Account::from_session("cn",json!({"account":{"uid":uid,"nickname":uid},"auth":{"accessToken":"original-token","refreshToken":"original-refresh","expiresAt":files::now_ms()+3600*1000}})).unwrap();
            app.gateway
                .store
                .write()
                .await
                .import(a, tmp.path())
                .unwrap();
        }
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let router = server::router(app.clone());
        let t2 = tokio::spawn(async move {
            axum::serve(
                listener,
                router.into_make_service_with_connect_info::<SocketAddr>(),
            )
            .await
            .unwrap();
        });
        Self {
            _tmp: tmp,
            app,
            base,
            _tasks: vec![t1, t2],
            mock,
        }
    }
    fn http(&self) -> reqwest::Client {
        reqwest::Client::new()
    }
    async fn chat(&self, model: &str, stream: bool) -> reqwest::Response {
        self.http().post(format!("{}/v1/chat/completions",self.base)).bearer_auth(&self.app.gateway.config.api_key).json(&json!({"model":model,"messages":[{"role":"user","content":"hi"}],"stream":stream,"conversation_id":"sticky-session"})).send().await.unwrap()
    }
}
#[tokio::test]
async fn auth_and_dynamic_models_are_openai_compatible() {
    let h = Harness::new(false).await;
    assert_eq!(
        h.http()
            .get(format!("{}/v1/models", h.base))
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    let value: Value = h
        .http()
        .get(format!("{}/v1/models", h.base))
        .bearer_auth(&h.app.gateway.config.api_key)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(value["object"], "list");
    assert_eq!(value["data"].as_array().unwrap().len(), 1);
    assert_eq!(value["data"][0]["id"], "test-model");
}
#[tokio::test]
async fn nonstream_aggregates_unicode_reasoning_tools_and_usage() {
    let h = Harness::new(false).await;
    let response = h.chat("test", false).await;
    assert_eq!(response.status(), 200);
    assert!(response.headers().get("x-request-id").is_some());
    let value: Value = response.json().await.unwrap();
    assert_eq!(value["object"], "chat.completion");
    let m = &value["choices"][0]["message"];
    assert_eq!(m["content"], "你好");
    assert_eq!(m["reasoning_content"], "think");
    assert_eq!(m["tool_calls"][0]["function"]["arguments"], "{\"q\":1}");
    assert_eq!(value["usage"]["total_tokens"], 8);
    assert!(!value.to_string().contains("not-for-clients"));
    assert_eq!(
        h.app.gateway.status()["in_flight"]
            .as_object()
            .unwrap()
            .values()
            .map(|v| v.as_u64().unwrap())
            .sum::<u64>(),
        0
    );
}
#[tokio::test]
async fn streaming_returns_chunks_and_exactly_one_done() {
    let h = Harness::new(false).await;
    let response = h.chat("test", true).await;
    assert_eq!(response.status(), 200);
    let text = response.text().await.unwrap();
    assert_eq!(text.matches("data: [DONE]").count(), 1);
    assert!(text.contains("tool_calls"));
    assert!(!text.contains("not-for-clients"));
    assert_eq!(h.app.gateway.status()["completed"], 1);
}
#[tokio::test]
async fn rate_limit_switches_account_and_sticky_session_stays_on_replacement() {
    let h = Harness::new(true).await;
    assert_eq!(h.chat("failover", false).await.status(), 200);
    // Consume body so the stream finishes and the lease is released.
    h.chat("test", false).await.bytes().await.unwrap();
    let requests = h.mock.requests.lock().unwrap();
    assert_eq!(requests.len(), 3);
    assert_ne!(requests[0].0, requests[1].0);
    assert_eq!(requests[1].0, requests[2].0);
}
#[tokio::test]
async fn rate_limit_inside_http_200_sse_changes_account_before_client_stream_begins() {
    for stream in [false, true] {
        let h = Harness::new(true).await;
        let res = h.chat("native-failover", stream).await;
        assert_eq!(res.status(), 200);
        let text = res.text().await.unwrap();
        assert!(!text.contains("secret upstream error"));
        let requests = h.mock.requests.lock().unwrap();
        assert_eq!(requests.len(), 2);
        assert_ne!(requests[0].0, requests[1].0);
    }
}
#[tokio::test]
async fn unauthorized_upstream_rotates_and_persists_both_tokens() {
    let h = Harness::new(false).await;
    let value: Value = h.chat("refresh", false).await.json().await.unwrap();
    assert_eq!(value["object"], "chat.completion");
    assert_eq!(h.mock.refreshes.load(Ordering::SeqCst), 1);
    let store = h.app.gateway.store.read().await;
    let a = store.accounts.values().next().unwrap();
    assert_eq!(a.session["auth"]["accessToken"], "rotated-token");
    assert_eq!(a.session["auth"]["refreshToken"], "rotated-refresh");
    let persisted: Value = files::read_json(&h._tmp.path().join("accounts.json")).unwrap();
    assert!(persisted.to_string().contains("rotated-refresh"));
}
#[tokio::test]
async fn empty_truncated_native_error_and_invalid_parameters_are_not_successes() {
    for model in ["empty", "truncated", "native-error"] {
        let h = Harness::new(false).await;
        let res = h.chat(model, false).await;
        assert_eq!(res.status(), 502, "{model}");
        let text = res.text().await.unwrap();
        assert!(!text.contains("secret upstream error"));
    }
    let h = Harness::new(false).await;
    assert_eq!(h.chat("invalid", false).await.status(), 400);
    let h = Harness::new(false).await;
    let res = h.chat("truncated", true).await;
    let text = res.text().await.unwrap();
    assert!(text.contains("stream_incomplete"));
    assert_eq!(text.matches("[DONE]").count(), 1);
}
#[tokio::test]
async fn management_is_separate_from_api_key_and_rejects_remote_origin_and_host() {
    let h = Harness::new(false).await;
    let url = format!("{}/api/accounts", h.base);
    assert_eq!(
        h.http()
            .get(&url)
            .bearer_auth(&h.app.gateway.config.api_key)
            .send()
            .await
            .unwrap()
            .status(),
        403
    );
    let value: Value = h
        .http()
        .get(&url)
        .header("x-wb-token", &h.app.admin_token)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(!value.to_string().contains("original-token"));
    assert_eq!(
        h.http()
            .get(&url)
            .header("x-wb-token", &h.app.admin_token)
            .header("Origin", "https://evil.example")
            .send()
            .await
            .unwrap()
            .status(),
        403
    );
    assert_eq!(
        h.http()
            .get(&url)
            .header("x-wb-token", &h.app.admin_token)
            .header("Host", "evil.example")
            .send()
            .await
            .unwrap()
            .status(),
        403
    );
    let mut request = Request::builder()
        .uri("/api/accounts")
        .header("host", "127.0.0.1")
        .header("x-wb-token", &h.app.admin_token)
        .body(Body::empty())
        .unwrap();
    request
        .extensions_mut()
        .insert(ConnectInfo("10.0.0.1:1234".parse::<SocketAddr>().unwrap()));
    assert_eq!(
        server::router(h.app.clone())
            .oneshot(request)
            .await
            .unwrap()
            .status(),
        403
    );
}
#[tokio::test]
async fn reference_export_formats_import_and_switch_without_touching_real_apps() {
    let h = Harness::new(false).await;
    let url = format!("{}/api/accounts/import", h.base);
    let response=h.http().post(&url).header("x-wb-token",&h.app.admin_token).json(&json!({"data":[{"uid":"bob","nickname":"Bob","variant":"cn","access_token":"import-token","refresh_token":"import-refresh","domain":"www.codebuddy.cn"}]})).send().await.unwrap();
    assert_eq!(response.status(), 200);
    let value: Value = response.json().await.unwrap();
    let id = value["accounts"][0]["id"].as_str().unwrap();
    let result: Value = h
        .http()
        .post(format!("{}/api/accounts/switch", h.base))
        .header("x-wb-token", &h.app.admin_token)
        .json(&json!({"id":id,"sync_all":false}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(result["status"], "switched");
    assert_eq!(result["uid"], "bob");
    assert_eq!(result["launched"], false);
    let auth: Value = files::read_json(&h.app.clients[0].auth).unwrap();
    assert_eq!(auth["auth"]["accessToken"], "import-token");
    assert!(!result.to_string().contains("import-token"));
}
#[tokio::test]
async fn gateway_only_listener_has_no_web_panel_or_management_routes() {
    let h = Harness::new(false).await;
    let value: Value = h
        .http()
        .get(format!("{}/healthz", h.base))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(value["service"], "wb-account-sync-gateway");
    let response = h.http().get(&h.base).send().await.unwrap();
    assert_eq!(response.status(), 404);
    assert!(response.headers().get("content-security-policy").is_some());
    for path in ["/", "/api/accounts", "/api/sessions/apply"] {
        let res = server::gateway_router(h.app.clone())
            .oneshot(
                Request::builder()
                    .uri(path)
                    .header("x-wb-token", &h.app.admin_token)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), 404);
    }
    assert_eq!(
        h.http()
            .get(format!("{}/accounts.json", h.base))
            .send()
            .await
            .unwrap()
            .status(),
        404
    );
}
#[tokio::test]
async fn native_dispatch_uses_the_same_engine_without_any_http_management_port() {
    let h = Harness::new(false).await;
    let accounts = server::dispatch(h.app.clone(), "/api/accounts", None)
        .await
        .unwrap();
    assert_eq!(accounts["accounts"].as_array().unwrap().len(), 1);
    let models = server::dispatch(h.app.clone(), "/api/gateway/models", None)
        .await
        .unwrap();
    assert_eq!(models["data"][0]["id"], "test-model");
    assert!(server::dispatch(h.app.clone(), "/v1/models", None)
        .await
        .is_err());
}
#[test]
fn decoder_handles_multiline_crlf_and_split_utf8() {
    let text = "data: {\"choices\":\r\ndata: [{\"delta\":{\"content\":\"你\"}}]}\r\n\r\n";
    let mut decoder = Decoder::default();
    let mut frames = Vec::new();
    for byte in text.as_bytes() {
        frames.extend(decoder.push(&[*byte]).unwrap());
    }
    assert_eq!(frames.len(), 1);
    assert!(normalize(&frames[0], "test").unwrap().is_some());
}
#[test]
fn aggregate_accepts_tools_only_and_does_not_invent_usage() {
    let mut aggregate = Aggregate::new("test");
    aggregate.add(&normalize("{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"tool-1\",\"function\":{\"name\":\"test\",\"arguments\":\"{}\"}}]},\"finish_reason\":\"tool_calls\"}]}","test").unwrap().unwrap());
    let value = aggregate.output();
    assert!(value["choices"][0]["message"]["content"].is_null());
    assert!(value["usage"].is_null());
}
#[tokio::test]
async fn local_header_auth_cannot_be_replaced_by_a_query_parameter() {
    let h = Harness::new(false).await;
    let response = h
        .http()
        .get(format!("{}/api/gateway?t={}", h.base, h.app.admin_token))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 403);
    let res = server::router(h.app.clone())
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert!(to_bytes(res.into_body(), 1024)
        .await
        .unwrap()
        .starts_with(b"{"));
}
