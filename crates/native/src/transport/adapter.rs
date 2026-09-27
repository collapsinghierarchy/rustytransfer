use anyhow::Result;
use rustytransfer_transfer::{PathObservation, TransferTransport};

use super::DataTransport;

#[async_trait::async_trait]
impl TransferTransport for DataTransport {
    async fn send_message(&mut self, data: Vec<u8>) -> Result<()> {
        self.send_vec(data).await
    }

    async fn receive_message(&mut self) -> Result<Vec<u8>> {
        self.recv_vec().await
    }

    async fn close_send_half(&mut self) -> Result<()> {
        self.close_send()
    }

    async fn finish_sending(&mut self) -> Result<()> {
        self.finish_send().await
    }

    async fn finish_receiving(&mut self) -> Result<()> {
        self.finish_recv().await
    }

    async fn wait_for_peer_close(&mut self) -> Result<()> {
        self.wait_for_peer_close().await
    }

    async fn close_transport(&mut self) -> Result<()> {
        DataTransport::close_transport(self).await
    }

    async fn observe_path(&mut self) -> Result<Option<PathObservation>> {
        self.path_observation().await.map(Some)
    }
}
