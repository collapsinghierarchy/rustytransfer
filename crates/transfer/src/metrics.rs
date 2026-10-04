use crate::PathObservation;
use std::time::Instant;

pub(crate) fn payload_profile_enabled_from_env() -> bool {
    std::env::var("RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE").is_ok_and(|value| value == "1")
}

pub(crate) fn completion_profile_enabled_from_env() -> bool {
    std::env::var("RUSTYTRANSFER_BENCH_COMPLETION_PROFILE").is_ok_and(|value| value == "1")
}

pub(crate) fn record_seconds(slot: &mut Option<f64>, started: Instant) {
    *slot = Some(started.elapsed().as_secs_f64());
}

/// Optional lifecycle spans around transfer confirmation and transport shutdown.
/// Individual sequential spans can be compared with shutdown time; setup,
/// application wall, and transfer lifetime are enclosing spans and overlap them.
#[derive(Debug, Clone, Default, PartialEq, serde::Serialize)]
pub struct CompletionProfile {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub setup_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub application_wall_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub transfer_lifetime_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub protocol_confirmation_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub receiver_commit_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub close_send_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finish_receiving_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finish_sending_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub wait_peer_close_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub explicit_connection_close_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub explicit_connection_close_applied: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub endpoint_close_seconds: Option<f64>,
}

impl CompletionProfile {
    pub(crate) fn for_enabled_transfer(enabled: bool) -> Option<Self> {
        enabled.then(Self::default)
    }
}

/// Optional payload-stage wall-time profile. Timings include async waiting and
/// backpressure; they are elapsed spans, not CPU time. Sender reads include the
/// EOF probe and receiver writes include the payload flush. Stage spans do not
/// overlap, so their sum should stay within `TransferMetrics::payload_seconds`
/// except for timer precision and measurement overhead.
#[derive(Debug, Clone, Default, PartialEq, serde::Serialize)]
pub struct PayloadProfile {
    pub source_read_seconds: f64,
    pub allocation_copy_encrypt_seconds: f64,
    /// Sender-side substage; overlaps `allocation_copy_encrypt_seconds`.
    pub sender_allocation_copy_seconds: f64,
    /// Sender-side substage; overlaps `allocation_copy_encrypt_seconds`.
    pub sender_encrypt_seconds: f64,
    pub send_wait_seconds: f64,
    pub receive_wait_seconds: f64,
    pub decrypt_seconds: f64,
    pub destination_write_seconds: f64,
    pub chunk_count: u64,
    pub payload_bytes: u64,
}

#[derive(Clone, Copy)]
pub(crate) enum PayloadStage {
    SourceRead,
    AllocationCopyEncrypt,
    SendWait,
    ReceiveWait,
    Decrypt,
    DestinationWrite,
}

impl PayloadProfile {
    pub(crate) fn for_enabled_transfer(enabled: bool) -> Option<Self> {
        enabled.then(Self::default)
    }

    pub(crate) fn start_stage(&self) -> Instant {
        Instant::now()
    }

    pub(crate) fn record_stage(&mut self, stage: PayloadStage, started: Instant) {
        let seconds = started.elapsed().as_secs_f64();
        let total = match stage {
            PayloadStage::SourceRead => &mut self.source_read_seconds,
            PayloadStage::AllocationCopyEncrypt => &mut self.allocation_copy_encrypt_seconds,
            PayloadStage::SendWait => &mut self.send_wait_seconds,
            PayloadStage::ReceiveWait => &mut self.receive_wait_seconds,
            PayloadStage::Decrypt => &mut self.decrypt_seconds,
            PayloadStage::DestinationWrite => &mut self.destination_write_seconds,
        };
        *total += seconds;
    }

    pub(crate) fn record_sender_allocation_copy(&mut self, started: Instant) {
        self.sender_allocation_copy_seconds += started.elapsed().as_secs_f64();
    }

    pub(crate) fn record_sender_encrypt(&mut self, started: Instant) {
        self.sender_encrypt_seconds += started.elapsed().as_secs_f64();
    }
}

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
    pub payload_profile: Option<PayloadProfile>,
    /// Coarse completion timings, absent unless the benchmark profile is enabled.
    pub completion_profile: Option<CompletionProfile>,
}
