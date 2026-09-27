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

    async fn abort(&mut self) -> Result<()> {
        self.close_transport().await
    }

    async fn observe_path(&mut self) -> Result<Option<PathObservation>> {
        Ok(None)
    }
}
