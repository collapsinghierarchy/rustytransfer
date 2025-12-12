use std::error::Error;
use serde::Deserialize;
use anyhow::Result;

use super::constants;

#[derive(Debug, serde::Deserialize)]
struct RendezvousCodeResponse {
        code: String,
        #[serde(rename = "appID")]
        app_id: String,
        #[serde(default)]
        expiresAt: Option<String>, // optional, just in case
}

pub fn code_req() -> Result<String, anyhow::Error> {
    let client = reqwest::blocking::Client::new();
    let res = client.post(constants::API_URL_CODE_REQ).send()?;
    let body_text = res.text()?;
    let parsed: RendezvousCodeResponse = serde_json::from_str(&body_text)?;
    Ok(parsed.code)
}

#[cfg(test)]
mod rendezvous_tests {
    #[test]
    #[ignore] // so it doesn't run on every `cargo test` unless you ask for it
    fn request_rendezvous_code_from_whitenoise() -> Result<(), anyhow::Error> {
        let code = super::code_req()?;
        println!("Got rendezvous code: {}", code);

        // basic sanity checks
        assert_eq!(code.len(), 4, "code should be 4 digits");
        // check it's all digits:
        assert!(code.chars().all(|c| c.is_ascii_digit()));

        Ok(())
    }
}

