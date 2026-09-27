use anyhow::{Result, bail};
use rand::RngCore;

/// A copyable direct-transfer address and per-transfer authorization secret.
pub(super) struct DirectInvite {
    pub(super) sender_id: String,
    pub(super) token: [u8; 16],
}

impl DirectInvite {
    pub(super) fn generate(sender_id: String) -> Self {
        let mut token = [0u8; 16];
        rand::thread_rng().fill_bytes(&mut token);
        Self { sender_id, token }
    }

    pub(super) fn parse(text: &str) -> Result<Self> {
        let mut parts = text.split(':');
        let (Some("rt1"), Some(sender_id), Some(hex), None) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        else {
            bail!("invalid direct invite format");
        };
        if sender_id.is_empty()
            || hex.len() != 32
            || !hex.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            bail!("invalid direct invite ID or token");
        }
        let token = u128::from_str_radix(hex, 16)?.to_be_bytes();
        Ok(Self {
            sender_id: sender_id.to_owned(),
            token,
        })
    }
}

impl std::fmt::Display for DirectInvite {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "rt1:{}:{:032x}",
            self.sender_id,
            u128::from_be_bytes(self.token)
        )
    }
}

#[cfg(test)]
mod tests {
    use super::DirectInvite;

    #[test]
    fn invite_round_trip_and_invalid_token() {
        let invite = DirectInvite::generate("example-id".to_owned());
        let parsed = DirectInvite::parse(&invite.to_string()).unwrap();
        assert_eq!(parsed.sender_id, invite.sender_id);
        assert_eq!(parsed.token, invite.token);
        assert!(DirectInvite::parse("rt1:example-id:short").is_err());
        assert!(DirectInvite::parse("rt1:example-id:0000000000000000000000000000000g").is_err());
    }
}
