pub const BASE_API_URL: &str = "http://141.147.1.21";

/// Backend base URL can be overridden for temporary deployments and local testing.
pub fn api_base_url() -> String {
    std::env::var("RUSTYTRANSFER_BACKEND_URL")
        .unwrap_or_else(|_| BASE_API_URL.to_owned())
        .trim_end_matches('/')
        .to_owned()
}

pub fn api_url(path: &str) -> String {
    format!("{}{}", api_base_url(), path)
}

pub fn websocket_base_url() -> String {
    let base = api_base_url();
    if let Some(host) = base.strip_prefix("https://") {
        format!("wss://{host}")
    } else if let Some(host) = base.strip_prefix("http://") {
        format!("ws://{host}")
    } else {
        base
    }
}
