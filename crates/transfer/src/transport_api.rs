use anyhow::Result;
use serde::Serialize;

/// The selected path without exposing candidate addresses.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PathObservation {
    pub path: &'static str,
    pub local_candidate_type: Option<String>,
    pub remote_candidate_type: Option<String>,
}

impl PathObservation {
    #[must_use]
    pub fn unknown() -> Self {
        Self {
            path: "unknown",
            local_candidate_type: None,
            remote_candidate_type: None,
        }
    }
}

/// The message and shutdown operations required by the transfer protocol.
#[async_trait::async_trait]
pub trait TransferTransport: Send {
    async fn send_message(&mut self, data: Vec<u8>) -> Result<()>;
    async fn receive_message(&mut self) -> Result<Vec<u8>>;
    async fn close_send_half(&mut self) -> Result<()>;
    async fn finish_sending(&mut self) -> Result<()>;
    async fn finish_receiving(&mut self) -> Result<()>;
    async fn wait_for_peer_close(&mut self) -> Result<()>;
    async fn close_transport(&mut self) -> Result<()>;

    /// Request an explicit connection close after protocol completion when the
    /// concrete transport has an opt-in benchmark close mode. Returns true only
    /// when the close request was actually issued.
    fn close_connection_if_enabled(&mut self) -> bool {
        false
    }

    /// Return the elapsed endpoint-close span when the concrete transport records it.
    fn take_endpoint_close_seconds(&mut self) -> Option<f64> {
        None
    }

    /// Mark the exact beginning of application payload traffic for optional transport diagnostics.
    fn begin_payload_observation(&mut self) {}

    /// Mark the exact end of application payload traffic for optional transport diagnostics.
    fn end_payload_observation(&mut self) {}

    async fn abort(&mut self) -> Result<()> {
        self.close_transport().await
    }

    async fn observe_path(&mut self) -> Result<Option<PathObservation>> {
        Ok(None)
    }
}
