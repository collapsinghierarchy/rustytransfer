use anyhow::{Context, Result, ensure};
use futures_util::TryStreamExt;
use rand::Rng;
use reqwest::StatusCode;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use std::time::Duration;

use crate::constants;
use crate::transport::errors::TransportError;

#[derive(Debug, Deserialize)]
pub struct RendezvousCodeResponse {
    pub code: String,
    #[serde(rename = "appID")]
    pub app_id: String,
    #[serde(default, rename = "expiresAt")]
    pub expires_at: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct RendezvousRedeemResponse {
    #[serde(rename = "appID")]
    pub app_id: String,
    #[serde(default, rename = "expiresAt")]
    pub expires_at: Option<String>,
}

#[derive(Debug, Serialize)]
struct RedeemRequest<'a> {
    code: &'a str,
}

const MAX_RESPONSE_BYTES: usize = 64 * 1024;
const MAX_RESPONSE_BYTES_U64: u64 = 64 * 1024;

fn rendezvous_client() -> Result<reqwest::Client> {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(15))
        .build()
        .context("failed to configure rendezvous client")
}

async fn decode_response<T: DeserializeOwned>(response: reqwest::Response) -> Result<T> {
    if let Some(length) = response.content_length() {
        ensure!(
            length <= MAX_RESPONSE_BYTES_U64,
            "rendezvous response too large"
        );
    }
    let mut body = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream
        .try_next()
        .await
        .context("failed to read rendezvous response")?
    {
        ensure!(
            chunk.len() <= MAX_RESPONSE_BYTES.saturating_sub(body.len()),
            "rendezvous response too large"
        );
        body.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&body).context("invalid rendezvous response")
}

/// POST /rendezvous/code → { code, appID, expiresAt? }
pub async fn request_code() -> Result<RendezvousCodeResponse> {
    let client = rendezvous_client()?;
    let res = client
        .post(constants::api_url("/rendezvous/code"))
        .send()
        .await
        .context("POST /rendezvous/code failed")?
        .error_for_status()
        .context("POST /rendezvous/code returned error")?;

    decode_response(res).await
}

/// POST /rendezvous/redeem {code:"NNNN"} → { appID, expiresAt? }
///
/// Returns a nice error on 410 Gone (used/expired/unknown).
pub async fn redeem(code4: &str) -> Result<RendezvousRedeemResponse> {
    let code4 = normalize_code4(code4)?;

    let client = rendezvous_client()?;
    let res = client
        .post(constants::api_url("/rendezvous/redeem"))
        .json(&RedeemRequest { code: &code4 })
        .send()
        .await
        .context("POST /rendezvous/redeem failed")?;

    if res.status() == StatusCode::GONE {
        return Err(TransportError::RendezvousCodeUnavailable.into());
    }

    let res = res
        .error_for_status()
        .context("POST /rendezvous/redeem returned error")?;

    decode_response(res).await
}

/// 5 uppercase letters (PAKE password)
pub fn gen_password_5() -> String {
    let mut rng = rand::thread_rng();
    (0..5)
        .map(|_| char::from(rng.gen_range(b'A'..=b'Z')))
        .collect()
}

/// Formats `NNNN-ABCDE`
pub fn format_share_code(code4: &str, pw5: &str) -> Result<String> {
    let code4 = normalize_code4(code4)?;
    let pw5 = normalize_pw5(pw5)?;
    Ok(format!("{code4}-{pw5}"))
}

/// Parses `NNNN-ABCDE` (dash optional, spaces ignored)
pub fn parse_share_code(s: &str) -> Result<(String, String)> {
    let cleaned = s.trim().replace([' ', '-'], "");
    if cleaned.len() != 9 {
        return Err(TransportError::InvalidRendezvousInput(
            "share code must be 9 chars total (NNNN-ABCDE)",
        )
        .into());
    }
    let (Some(digits), Some(letters)) = (cleaned.get(..4), cleaned.get(4..)) else {
        return Err(TransportError::InvalidRendezvousInput(
            "share code must be 9 chars total (NNNN-ABCDE)",
        )
        .into());
    };
    Ok((normalize_code4(digits)?, normalize_pw5(letters)?))
}

fn normalize_code4(code4: &str) -> Result<String> {
    let c = code4.trim();
    if c.len() != 4 || !c.chars().all(|x| x.is_ascii_digit()) {
        return Err(TransportError::InvalidRendezvousInput(
            "rendezvous code must be exactly 4 digits",
        )
        .into());
    }
    Ok(c.to_string())
}

fn normalize_pw5(pw5: &str) -> Result<String> {
    let p = pw5.trim();
    if p.len() != 5 || !p.chars().all(|x| x.is_ascii_uppercase()) {
        return Err(TransportError::InvalidRendezvousInput(
            "password must be exactly 5 uppercase letters",
        )
        .into());
    }
    Ok(p.to_string())
}

#[cfg(test)]
mod rendezvous_tests {
    use super::*;
    use anyhow::ensure;

    #[tokio::test]
    #[ignore = "requires a live rendezvous backend"]
    async fn request_rendezvous_code_from_whitenoise() -> Result<()> {
        let r = request_code().await?;
        println!("Got rendezvous code: {}", r.code);
        ensure!(
            r.code.len() == 4,
            "rendezvous code must contain four digits"
        );
        ensure!(
            r.code.chars().all(|c| c.is_ascii_digit()),
            "rendezvous code contains a non-digit"
        );
        Ok(())
    }
}
