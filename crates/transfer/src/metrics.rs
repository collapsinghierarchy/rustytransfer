use crate::PathObservation;

/// Phase timings and byte counts returned after a complete transfer.
#[derive(Debug, Clone, PartialEq)]
pub struct TransferMetrics {
    pub bytes_transferred: u64,
    pub chunk_size: u32,
    pub handshake_seconds: f64,
    pub payload_seconds: f64,
    pub shutdown_seconds: f64,
    pub path_end: Option<PathObservation>,
    pub cleanup_issues: Vec<&'static str>,
}
