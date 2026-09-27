use crate::PathObservation;

/// Phase timings and byte counts returned after a complete transfer.
#[derive(Debug, Clone, PartialEq)]
pub struct TransferMetrics {
    /// Bytes sent or received in this session, excluding a reused prefix.
    pub bytes_transferred: u64,
    /// Full file size, including any prefix reused from an earlier session.
    pub file_size: u64,
    pub chunk_size: u32,
    pub handshake_seconds: f64,
    pub payload_seconds: f64,
    pub shutdown_seconds: f64,
    pub path_end: Option<PathObservation>,
    pub cleanup_issues: Vec<&'static str>,
}
