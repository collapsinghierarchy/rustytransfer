use anyhow::{Result, bail};
use rand::{RngCore, rngs::OsRng};
use rustytransfer_transfer::error::{CompletionState, ErrorCode, RetryAdvice, TransferError};
use std::{fmt, time::Duration};

/// A copyable direct-transfer address and per-transfer authorization secret.
#[derive(Clone)]
pub struct DirectInvite {
    pub sender_id: String,
    pub token: [u8; 16],
}

impl DirectInvite {
    #[must_use]
    pub fn generate(sender_id: String) -> Self {
        let mut token = [0_u8; 16];
        let mut rng = OsRng;
        rng.fill_bytes(&mut token);
        Self { sender_id, token }
    }

    /// Parse an invite while preserving the original `rt1:` validation rules.
    ///
    /// # Errors
    ///
    /// Returns an error if the prefix, sender ID, or 128-bit hexadecimal token is invalid.
    pub fn parse(text: &str) -> Result<Self> {
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

    #[must_use]
    pub fn format(&self) -> String {
        self.to_string()
    }
}

impl fmt::Display for DirectInvite {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "rt1:{}:{:032x}",
            self.sender_id,
            u128::from_be_bytes(self.token)
        )
    }
}

/// Maximum number of sessions an interrupted transfer may use.
pub const MAX_TRANSFER_ATTEMPTS: usize = 4;

const RETRY_BACKOFFS: [Duration; 3] = [
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(4),
];

/// Return the delay before retrying the given one-based attempt number.
#[must_use]
pub fn retry_backoff(attempt: usize) -> Duration {
    RETRY_BACKOFFS
        .get(attempt.saturating_sub(2))
        .copied()
        .unwrap_or(Duration::from_secs(4))
}

/// Whether a classified transfer failure can safely be retried in a new session.
#[must_use]
pub fn can_retry_transfer(error: &TransferError) -> bool {
    error.retry_advice() == RetryAdvice::NewSession
        && error.code() != ErrorCode::Cancelled
        && matches!(
            error.completion,
            CompletionState::NotStarted
                | CompletionState::InProgress
                | CompletionState::DataCompleteUnconfirmed
        )
}

#[cfg(test)]
mod tests {
    use super::{
        CompletionState, DirectInvite, MAX_TRANSFER_ATTEMPTS, TransferError, can_retry_transfer,
        retry_backoff,
    };
    use anyhow::Result;
    use rustytransfer_protocol::fsm::StepError;
    use rustytransfer_transfer::error::{Phase, TransferErrorKind};
    use std::{io, time::Duration};

    #[test]
    fn invite_roundtrips_with_the_existing_wire_format() -> Result<()> {
        let invite = DirectInvite {
            sender_id: "example-id".to_owned(),
            token: [0xab; 16],
        };
        let wire = "rt1:example-id:abababababababababababababababab";
        assert_eq!(invite.to_string(), wire);
        assert_eq!(invite.format(), wire);

        let parsed = DirectInvite::parse(wire)?;
        assert_eq!(parsed.sender_id, invite.sender_id);
        assert_eq!(parsed.token, invite.token);

        let uppercase = "rt1:example-id:ABABABABABABABABABABABABABABABAB";
        assert_eq!(DirectInvite::parse(uppercase)?.to_string(), wire);

        let generated = DirectInvite::generate("another-id".to_owned());
        let parsed_generated = DirectInvite::parse(&generated.to_string())?;
        assert_eq!(parsed_generated.token, generated.token);
        Ok(())
    }

    #[test]
    fn invite_parser_rejects_malformed_inputs() {
        for invalid in [
            "",
            "rt2:example-id:abababababababababababababababab",
            "rt1::abababababababababababababababab",
            "rt1:example-id:short",
            "rt1:example-id:abababababababababababababababg0",
            "rt1:example-id:abababababababababababababababab:extra",
        ] {
            assert!(
                DirectInvite::parse(invalid).is_err(),
                "accepted {invalid:?}"
            );
        }
    }

    #[test]
    fn retry_policy_respects_completion_and_error_classification() {
        for completion in [
            CompletionState::NotStarted,
            CompletionState::InProgress,
            CompletionState::DataCompleteUnconfirmed,
        ] {
            let interrupted =
                TransferError::new(TransferErrorKind::Timeout, Phase::Payload, 10, completion);
            assert!(can_retry_transfer(&interrupted));
        }

        for completion in [
            CompletionState::ProtocolConfirmed,
            CompletionState::Committed,
        ] {
            let confirmed =
                TransferError::new(TransferErrorKind::Timeout, Phase::Shutdown, 10, completion);
            assert!(!can_retry_transfer(&confirmed));
        }

        let cancelled = TransferError::new(
            TransferErrorKind::Cancelled,
            Phase::Payload,
            10,
            CompletionState::InProgress,
        );
        let file_error = TransferError::new(
            TransferErrorKind::SourceIo(io::Error::other("read failed")),
            Phase::Payload,
            10,
            CompletionState::InProgress,
        );
        let configuration_error = TransferError::new(
            TransferErrorKind::Config("invalid test configuration"),
            Phase::Input,
            0,
            CompletionState::NotStarted,
        );
        let protocol_error = TransferError::new(
            TransferErrorKind::Protocol(StepError::InvalidTransition(
                "malformed protocol step".to_owned(),
            )),
            Phase::Payload,
            10,
            CompletionState::InProgress,
        );
        assert!(!can_retry_transfer(&cancelled));
        assert!(!can_retry_transfer(&file_error));
        assert!(!can_retry_transfer(&configuration_error));
        assert!(!can_retry_transfer(&protocol_error));
    }

    #[test]
    fn retry_backoff_is_bounded_by_the_four_attempt_policy() {
        assert_eq!(MAX_TRANSFER_ATTEMPTS, 4);
        assert_eq!(retry_backoff(2), Duration::from_secs(1));
        assert_eq!(retry_backoff(3), Duration::from_secs(2));
        assert_eq!(retry_backoff(4), Duration::from_secs(4));
        assert_eq!(retry_backoff(5), Duration::from_secs(4));
    }
}
