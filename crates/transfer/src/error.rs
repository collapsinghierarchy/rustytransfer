//! Stable transfer error classification. Diagnostic sources stay local to the
//! process; only redacted codes and messages belong in user output or metrics.

use std::{error::Error, fmt, io};

use rustytransfer_protocol::fsm::StepError;

/// A stable public code for one category of transfer failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum ErrorCode {
    InvalidInput,
    AuthenticationFailed,
    ProtocolViolation,
    TransportFailure,
    TimedOut,
    SourceIo,
    DestinationIo,
    CommitFailed,
    Cancelled,
    Internal,
}

impl ErrorCode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::InvalidInput => "RTY-INPUT-001",
            Self::AuthenticationFailed => "RTY-AUTH-001",
            Self::ProtocolViolation => "RTY-PROTOCOL-001",
            Self::TransportFailure => "RTY-CONNECT-001",
            Self::TimedOut => "RTY-TIMEOUT-001",
            Self::SourceIo => "RTY-IO-001",
            Self::DestinationIo => "RTY-IO-002",
            Self::CommitFailed => "RTY-IO-003",
            Self::Cancelled => "RTY-CANCELLED-001",
            Self::Internal => "RTY-INTERNAL-001",
        }
    }

    #[must_use]
    pub const fn user_message(self) -> &'static str {
        match self {
            Self::InvalidInput => "Die Transfereinstellungen sind ungültig.",
            Self::AuthenticationFailed => {
                "Die Authentifizierung ist fehlgeschlagen. Prüfe den Share-Code."
            }
            Self::ProtocolViolation => {
                "Die Gegenstelle hat das Transferprotokoll nicht eingehalten."
            }
            Self::TransportFailure => "Die Verbindung zur Gegenstelle ist abgebrochen.",
            Self::TimedOut => "Während der Übertragung ist eine Zeitüberschreitung aufgetreten.",
            Self::SourceIo => "Die Quelldatei konnte nicht vollständig gelesen werden.",
            Self::DestinationIo => "Die Zieldatei konnte nicht vollständig geschrieben werden.",
            Self::CommitFailed => {
                "Die empfangene Datei konnte nicht als Zieldatei übernommen werden."
            }
            Self::Cancelled => "Die Übertragung wurde abgebrochen.",
            Self::Internal => "Rustytransfer ist auf einen internen Fehler gestoßen.",
        }
    }

    #[must_use]
    pub const fn exit_code(self) -> u8 {
        match self {
            Self::InvalidInput => 2,
            Self::AuthenticationFailed => 5,
            Self::ProtocolViolation => 6,
            Self::TransportFailure => 4,
            Self::TimedOut => 8,
            Self::SourceIo | Self::DestinationIo | Self::CommitFailed => 7,
            Self::Cancelled => 130,
            Self::Internal => 70,
        }
    }
}

impl fmt::Display for ErrorCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The operation during which a failure occurred.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum Phase {
    Input,
    Rendezvous,
    Signaling,
    Connect,
    Pake,
    KemAuth,
    Metadata,
    Payload,
    Finalize,
    Shutdown,
    Metrics,
}

impl Phase {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Input => "input",
            Self::Rendezvous => "rendezvous",
            Self::Signaling => "signaling",
            Self::Connect => "connect",
            Self::Pake => "pake",
            Self::KemAuth => "kem_auth",
            Self::Metadata => "metadata",
            Self::Payload => "payload",
            Self::Finalize => "finalize",
            Self::Shutdown => "shutdown",
            Self::Metrics => "metrics",
        }
    }
}

impl fmt::Display for Phase {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum CompletionState {
    NotStarted,
    InProgress,
    DataCompleteUnconfirmed,
    ProtocolConfirmed,
    Committed,
}

impl CompletionState {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::NotStarted => "not_started",
            Self::InProgress => "in_progress",
            Self::DataCompleteUnconfirmed => "data_complete_unconfirmed",
            Self::ProtocolConfirmed => "protocol_confirmed",
            Self::Committed => "committed",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum RetryAdvice {
    Never,
    AfterUserAction,
    AfterBackoff,
    NewSession,
}

#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum TransferErrorKind {
    #[error("invalid transfer configuration: {0}")]
    Config(&'static str),
    #[error("transfer protocol failed: {0}")]
    Protocol(#[source] StepError),
    #[error("transport operation failed: {0}")]
    Transport(#[source] anyhow::Error),
    #[error("transfer operation timed out")]
    Timeout,
    #[error("source read failed: {0}")]
    SourceIo(#[source] io::Error),
    #[error("destination write failed: {0}")]
    DestinationIo(#[source] io::Error),
    #[error("destination commit failed: {0}")]
    CommitIo(#[source] io::Error),
    #[error("transfer was cancelled")]
    Cancelled,
    #[error("internal transfer invariant failed: {0}")]
    Internal(&'static str),
}

/// A classified failure with enough safe context for the CLI and metrics.
#[derive(Debug)]
pub struct TransferError {
    pub kind: TransferErrorKind,
    pub phase: Phase,
    pub bytes_transferred: u64,
    pub completion: CompletionState,
    pub cleanup_issues: Vec<String>,
}

impl TransferError {
    #[must_use]
    pub fn new(
        kind: TransferErrorKind,
        phase: Phase,
        bytes_transferred: u64,
        completion: CompletionState,
    ) -> Self {
        Self {
            kind,
            phase,
            bytes_transferred,
            completion,
            cleanup_issues: Vec::new(),
        }
    }

    #[must_use]
    pub fn code(&self) -> ErrorCode {
        match &self.kind {
            TransferErrorKind::Config(_) => ErrorCode::InvalidInput,
            TransferErrorKind::Protocol(error) if error.is_authentication() => {
                ErrorCode::AuthenticationFailed
            }
            TransferErrorKind::Protocol(_) => ErrorCode::ProtocolViolation,
            TransferErrorKind::Transport(_) => ErrorCode::TransportFailure,
            TransferErrorKind::Timeout => ErrorCode::TimedOut,
            TransferErrorKind::SourceIo(_) => ErrorCode::SourceIo,
            TransferErrorKind::DestinationIo(_) => ErrorCode::DestinationIo,
            TransferErrorKind::CommitIo(_) => ErrorCode::CommitFailed,
            TransferErrorKind::Cancelled => ErrorCode::Cancelled,
            TransferErrorKind::Internal(_) => ErrorCode::Internal,
        }
    }

    #[must_use]
    pub const fn phase(&self) -> Phase {
        self.phase
    }

    #[must_use]
    pub fn retry_advice(&self) -> RetryAdvice {
        match self.code() {
            ErrorCode::InvalidInput
            | ErrorCode::AuthenticationFailed
            | ErrorCode::ProtocolViolation
            | ErrorCode::Internal => RetryAdvice::Never,
            ErrorCode::SourceIo | ErrorCode::DestinationIo | ErrorCode::CommitFailed => {
                RetryAdvice::AfterUserAction
            }
            ErrorCode::TransportFailure | ErrorCode::TimedOut | ErrorCode::Cancelled => {
                RetryAdvice::NewSession
            }
        }
    }

    #[must_use]
    pub fn exit_code(&self) -> u8 {
        self.code().exit_code()
    }

    pub fn add_cleanup_issue(&mut self, issue: impl Into<String>) {
        self.cleanup_issues.push(issue.into());
    }
}

impl fmt::Display for TransferError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} during {}: {}", self.code(), self.phase, self.kind)
    }
}

impl Error for TransferError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        self.kind.source()
    }
}
