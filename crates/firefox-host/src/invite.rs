use anyhow::{Result, bail};
use rand::RngCore;

/// Direct Iroh address plus the per-transfer secret used by the CLI format.
#[derive(Clone)]
pub(crate) struct DirectInvite {
    pub(crate) sender_id: String,
    pub(crate) token: [u8; 16],
}

impl DirectInvite {
    pub(crate) fn generate(sender_id: String) -> Self {
        let mut token = [0_u8; 16];
        rand::rngs::OsRng.fill_bytes(&mut token);
        Self { sender_id, token }
    }

    pub(crate) fn parse(text: &str) -> Result<Self> {
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

    pub(crate) fn format(&self) -> String {
        format!(
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
    fn invite_format_matches_cli_and_round_trips() {
        let invite = DirectInvite {
            sender_id: "example-id".to_owned(),
            token: [0xabu8; 16],
        };

        assert_eq!(
            invite.format(),
            "rt1:example-id:abababababababababababababababab"
        );
        let parsed = DirectInvite::parse(&invite.format()).expect("valid invite should parse");
        assert_eq!(parsed.sender_id, invite.sender_id);
        assert_eq!(parsed.token, invite.token);
        assert_eq!(
            DirectInvite::parse("rt1:example-id:ABABABABABABABABABABABABABABABAB")
                .expect("uppercase hex should parse")
                .format(),
            invite.format()
        );
    }

    #[test]
    fn invite_parser_rejects_invalid_format_and_token() {
        assert!(DirectInvite::parse("rt1:example-id:short").is_err());
        assert!(DirectInvite::parse("rt1:example-id:0000000000000000000000000000000g").is_err());
        assert!(DirectInvite::parse("rt1::00000000000000000000000000000000").is_err());
        assert!(
            DirectInvite::parse("rt1:example-id:00000000000000000000000000000000:extra").is_err()
        );
    }
}
