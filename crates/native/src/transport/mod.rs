pub mod errors;
pub use crate::signaling::{frames, websocket};
mod adapter;
#[cfg(feature = "iroh")]
pub mod iroh;
#[cfg(feature = "webrtc")]
pub mod webrtc;

use anyhow::Result;
pub use rustytransfer_transfer::PathObservation;

#[cfg(feature = "iroh")]
use self::iroh::IrohState;
#[cfg(feature = "webrtc")]
use self::webrtc::WebRtcState;

/// The selected byte-message transport used by the CLI.
// Clippy baseline: the public type name makes the transport abstraction explicit.
pub enum DataTransport {
    #[cfg(feature = "webrtc")]
    WebRtc(WebRtcState),
    #[cfg(feature = "iroh")]
    Iroh(IrohState),
}

impl DataTransport {
    /// Return the selected network path without exposing candidate addresses.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying transport cannot inspect its current path.
    pub async fn path_observation(&self) -> Result<PathObservation> {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(state) => state.path_observation().await,
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => Ok(state.path_observation()),
        }
    }

    /// Sends one byte message over the selected transport.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying transport rejects the message.
    pub async fn send_vec(&mut self, data: Vec<u8>) -> Result<()> {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(state) => state.send_vec(data).await,
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.send_vec(data).await,
        }
    }

    /// Receives one byte message from the selected transport.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying transport cannot receive the message.
    pub async fn recv_vec(&mut self) -> Result<Vec<u8>> {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(state) => state.recv_vec().await,
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.recv_vec().await,
        }
    }

    /// Close this peer's sending half without waiting for a transport receipt.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying transport cannot finish its sending half.
    pub fn close_send(&mut self) -> Result<()> {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(_) => Ok(()),
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.close_send(),
        }
    }

    /// Finish the sending half and wait until the peer has consumed it.
    ///
    /// # Errors
    ///
    /// Returns an error if delivery fails or the underlying transport reports an early stop.
    pub async fn finish_send(&mut self) -> Result<()> {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(_) => Ok(()),
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.finish_send().await,
        }
    }

    /// Wait for the peer to finish sending on transports with stream half-closes.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying transport cannot observe the peer's stream finish.
    pub async fn finish_recv(&mut self) -> Result<()> {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(_) => Ok(()),
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.finish_recv().await,
        }
    }

    /// Wait until the peer has closed the transport.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying transport's close signal ends unexpectedly.
    pub async fn wait_for_peer_close(&mut self) -> Result<()> {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(state) => state.wait_for_peer_close().await,
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.wait_for_peer_close().await,
        }
    }

    /// Gracefully close this transport after the receiver has drained its final messages.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying transport cannot close cleanly.
    pub async fn close_transport(&self) -> Result<()> {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(state) => state.close_transport().await,
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.close_transport().await,
        }
    }
}
