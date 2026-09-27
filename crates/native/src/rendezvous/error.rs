use std::fmt;

/// Errors returned by the rendezvous service.
#[derive(Debug)]
pub enum RendezvousError {
    CodeUnavailable,
}

impl fmt::Display for RendezvousError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CodeUnavailable => {
                formatter.write_str("rendezvous code is used/expired/unknown (HTTP 410 Gone)")
            }
        }
    }
}

impl std::error::Error for RendezvousError {}
