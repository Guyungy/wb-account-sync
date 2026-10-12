use std::{net::SocketAddr, path::PathBuf};
use wb_server::{clients::Client, files, gateway::Config, server};

#[tokio::main]
async fn main() {
    if let Err(e) = run().await {
        eprintln!("Error: {e}");
        std::process::exit(1);
    }
}
async fn run() -> files::Result<()> {
    let mut listen: Option<String> = None;
    let mut dir = files::user_home().join(".wb-account-sync-server");
    let mut clients = vec![Client::default_for("cn")?, Client::default_for("ai")?];
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--help" || arg == "-h" {
            println!("wb-server [--listen 127.0.0.1:8789] [--state-dir PATH]\n  [--home-cn PATH] [--home-ai PATH] [--auth-cn PATH] [--auth-ai PATH]\n  [--app-cn PATH] [--app-ai PATH]\nInference only: /v1/chat/completions and /v1/models\nAccount management is available in the native desktop app. Keep state-dir persistent.");
            return Ok(());
        }
        let value = args
            .next()
            .ok_or_else(|| format!("Missing value for {arg}"))?;
        match arg.as_str() {
            "--listen" => listen = Some(value),
            "--state-dir" => dir = PathBuf::from(value),
            "--home-cn" => clients[0].home = value.into(),
            "--home-ai" => clients[1].home = value.into(),
            "--auth-cn" => clients[0].auth = value.into(),
            "--auth-ai" => clients[1].auth = value.into(),
            "--app-cn" => clients[0].app = value,
            "--app-ai" => clients[1].app = value,
            _ => return Err(format!("Unknown option {arg}")),
        }
    }
    files::private_dir(&dir)?;
    dir = std::fs::canonicalize(dir).map_err(|e| e.to_string())?;
    let _lock = files::Lock::acquire(&dir.join("server.lock"))?;
    for client in &mut clients {
        if client.home.is_relative() {
            client.home = std::env::current_dir()
                .map_err(|e| e.to_string())?
                .join(&client.home);
        }
        if client.auth.is_relative() {
            client.auth = std::env::current_dir()
                .map_err(|e| e.to_string())?
                .join(&client.auth);
        }
    }
    if clients[0].home == clients[1].home
        || (clients[0].home.exists()
            && clients[1].home.exists()
            && std::fs::canonicalize(&clients[0].home).ok()
                == std::fs::canonicalize(&clients[1].home).ok())
    {
        return Err("Client homes must be different".into());
    }
    let config_path = dir.join("gateway.json");
    let config: Config = if config_path.exists() {
        files::read_json(&config_path)?
    } else {
        let config = Config::default();
        files::atomic_json(&config_path, &config)?;
        config
    };
    let listen = listen.unwrap_or_else(|| config.listen.clone());
    let app = server::App::new(dir, clients, config)?;
    let addr: SocketAddr = listen.parse().map_err(|_| "Invalid listen address")?;
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| e.to_string())?;
    let actual = listener.local_addr().map_err(|e| e.to_string())?;
    let local_ip = if actual.is_ipv6() {
        "[::1]"
    } else {
        "127.0.0.1"
    };
    println!("Inference API: http://{local_ip}:{}/v1", actual.port());
    println!("Gateway: http://{actual}/v1 (API key available in local management)");
    axum::serve(
        listener,
        server::gateway_router(app).into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(async {
        let _ = tokio::signal::ctrl_c().await;
    })
    .await
    .map_err(|e| e.to_string())
}
