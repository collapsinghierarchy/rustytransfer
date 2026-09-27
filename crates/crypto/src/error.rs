use std::{error::Error, fmt};

/// Errors returned by cryptographic operations.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CryptoError {
    PakeAlreadyFinished,
    InvalidPakeMessage,
    MissingEncapsulationKey,
    MissingDecapsulationKey,
    EncapsulationFailed,
    DecapsulationFailed,
    DemFailed,
    NonceExhausted,
}

impl fmt::Display for CryptoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            Self::PakeAlreadyFinished => "PAKE state already finished",
            Self::InvalidPakeMessage => "invalid PAKE message",
            Self::MissingEncapsulationKey => "KEM encapsulation key unavailable",
            Self::MissingDecapsulationKey => "KEM decapsulation key unavailable",
            Self::EncapsulationFailed => "KEM encapsulation failed",
            Self::DecapsulationFailed => "KEM decapsulation failed",
            Self::DemFailed => "DEM authentication or encryption failed",
            Self::NonceExhausted => "DEM nonce counter exhausted",
        };
        f.write_str(message)
    }
}

impl Error for CryptoError {}

impl From<aes_gcm::Error> for CryptoError {
    fn from(_: aes_gcm::Error) -> Self {
        Self::DemFailed
    }
}
