use crate::crypto::pake::PakeState;
use crate::crypto::kem::KemState;
use crate::crypto::mac::MacState;
use std::{error::Error, fmt};

#[derive(Debug)]
pub enum State {
    Init {role: Role, pw: Option<Vec<u8>>},
    Pake {role: Role, pake_state: PakeState, shared_key: Option<Vec<u8>>},
    KemAuth {role: Role, mac: Option<MacState>, kem: Option<KemState>},
    Smt {role: Role},

    Success(String),
    Failed(String)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Sender,
    Receiver
}

#[derive(Debug)]
pub struct PakeKey;         // later: actual key representation
#[derive(Debug)]
pub struct Password;        // later: actual password representation
#[derive(Debug)]
pub struct KemPublicKey;
#[derive(Debug)]
pub struct MacKey;
#[derive(Debug)]
pub struct DemKey;
#[derive(Debug)]
pub struct FileData;

// For sender/receiver messages:
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
    // later: CryptoError, MacError, etc.
}

impl From<&str> for StepError {
    fn from(s: &str) -> Self {
        StepError::InvalidTransition(s.to_string())
    }
}

impl From<String> for StepError {
    fn from(s: String) -> Self {
        StepError::InvalidTransition(s)
    }
}

impl From<aes_gcm::Error> for StepError {
    fn from(_: aes_gcm::Error) -> Self {
        StepError::InvalidTransition("DEM decryption failed".into())
    }
}

impl fmt::Display for StepError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StepError::InvalidTransition(msg) => write!(f, "invalid transition: {msg}"),
        }
    }
}

impl Error for StepError {}
