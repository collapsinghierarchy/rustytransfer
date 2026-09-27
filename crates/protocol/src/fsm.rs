use rustytransfer_crypto::error::CryptoError;
use rustytransfer_crypto::kem::KemState;
use rustytransfer_crypto::mac::MacState;
use rustytransfer_crypto::pake::PakeState;
use std::{error::Error, fmt};

pub enum State {
    Init {
        role: Role,
        pw: Option<Vec<u8>>,
    },
    Pake {
        role: Role,
        pake_state: PakeState,
        shared_key: Option<Vec<u8>>,
    },
    DirectAuth {
        role: Role,
        shared_key: Vec<u8>,
    },
    KemAuth {
        role: Role,
        mac: Option<MacState>,
        kem: Option<Box<KemState>>,
    },
    Smt {
        role: Role,
    },
    Success(String),
    Failed(String),
}

impl fmt::Debug for State {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Init { role, .. } => f.debug_struct("Init").field("role", role).finish(),
            Self::Pake { role, .. } => f.debug_struct("Pake").field("role", role).finish(),
            Self::DirectAuth { role, .. } => {
                f.debug_struct("DirectAuth").field("role", role).finish()
            }
            Self::KemAuth { role, .. } => f.debug_struct("KemAuth").field("role", role).finish(),
            Self::Smt { role } => f.debug_struct("Smt").field("role", role).finish(),
            Self::Success(_) => f.write_str("Success"),
            Self::Failed(_) => f.write_str("Failed"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Sender,
    Receiver,
}

#[derive(Debug)]
pub struct PakeKey;
#[derive(Debug)]
pub struct Password;
#[derive(Debug)]
pub struct KemPublicKey;
#[derive(Debug)]
pub struct MacKey;
#[derive(Debug)]
pub struct DemKey;
#[derive(Debug)]
pub struct FileData;

#[derive(Debug)]
pub struct RendezvousInfo;
#[derive(Debug)]
pub struct MacTag;
#[derive(Debug)]
pub struct KemCiphertext;
#[derive(Debug)]
pub struct DemData;

#[derive(Debug)]
pub enum StepError {
    InvalidTransition(String),
    Authentication,
    Crypto(CryptoError),
}

impl From<&str> for StepError {
    fn from(message: &str) -> Self {
        Self::InvalidTransition(message.to_string())
    }
}

impl From<String> for StepError {
    fn from(message: String) -> Self {
        Self::InvalidTransition(message)
    }
}

impl From<CryptoError> for StepError {
    fn from(error: CryptoError) -> Self {
        Self::Crypto(error)
    }
}

impl fmt::Display for StepError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidTransition(message) => write!(f, "invalid transition: {message}"),
            Self::Authentication => f.write_str("authentication failed"),
            Self::Crypto(error) => write!(f, "cryptographic operation failed: {error}"),
        }
    }
}

impl Error for StepError {}

impl StepError {
    #[must_use]
    pub fn is_authentication(&self) -> bool {
        matches!(
            self,
            Self::Authentication
                | Self::Crypto(CryptoError::InvalidPakeMessage | CryptoError::DemFailed)
        )
    }
}
