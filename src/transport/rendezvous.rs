use anyhow::{bail, Context, Result};
use rand::Rng;
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};

use super::constants;

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

/// POST /rendezvous/code → { code, appID, expiresAt? }
pub async fn request_code() -> Result<RendezvousCodeResponse> {
    let client = reqwest::Client::new();
    let res = client
        .post(constants::API_URL_CODE_REQ)
        .send()
        .await
        .context("POST /rendezvous/code failed")?
        .error_for_status()
        .context("POST /rendezvous/code returned error")?;

    Ok(res
        .json::<RendezvousCodeResponse>()
        .await
        .context("failed to parse /rendezvous/code json")?)
}

/// POST /rendezvous/redeem {code:"NNNN"} → { appID, expiresAt? }
///
/// Returns a nice error on 410 Gone (used/expired/unknown).
pub async fn redeem(code4: &str) -> Result<RendezvousRedeemResponse> {
    let code4 = normalize_code4(code4)?;

    let client = reqwest::Client::new();
    let res = client
        .post(constants::API_URL_CODE_REDEEM)
        .json(&RedeemRequest { code: &code4 })
        .send()
        .await
        .context("POST /rendezvous/redeem failed")?;

    if res.status() == StatusCode::GONE {
        bail!("rendezvous code is used/expired/unknown (HTTP 410 Gone)");
    }

    let res = res
        .error_for_status()
        .context("POST /rendezvous/redeem returned error")?;

    Ok(res
        .json::<RendezvousRedeemResponse>()
        .await
        .context("failed to parse /rendezvous/redeem json")?)
}

/// 5 uppercase letters (PAKE password)
pub fn gen_password_5() -> String {
    let mut rng = rand::thread_rng();
    (0..5)
        .map(|_| (b'A' + rng.gen_range(0..26) as u8) as char)
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
    let cleaned = s.trim().replace(' ', "").replace('-', "");
    if cleaned.len() != 9 {
        bail!("share code must be 9 chars total (NNNN-ABCDE)");
    }
    let (digits, letters) = cleaned.split_at(4);
    Ok((normalize_code4(digits)?, normalize_pw5(letters)?))
}

fn normalize_code4(code4: &str) -> Result<String> {
    let c = code4.trim();
    if c.len() != 4 || !c.chars().all(|x| x.is_ascii_digit()) {
        bail!("rendezvous code must be exactly 4 digits");
    }
    Ok(c.to_string())
}

fn normalize_pw5(pw5: &str) -> Result<String> {
    let p = pw5.trim();
    if p.len() != 5 || !p.chars().all(|x| x.is_ascii_uppercase()) {
        bail!("password must be exactly 5 uppercase letters");
    }
    Ok(p.to_string())
}

#[cfg(test)]
mod rendezvous_tests {
    use super::*;

    #[tokio::test]
    #[ignore]
    async fn request_rendezvous_code_from_whitenoise() -> Result<()> {
        let r = request_code().await?;
        println!("Got rendezvous code: {}", r.code);
        assert_eq!(r.code.len(), 4);
        assert!(r.code.chars().all(|c| c.is_ascii_digit()));
        Ok(())
    }
}
