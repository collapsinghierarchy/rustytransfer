pub mod errors;
pub use crate::signaling::{frames, websocket};
mod adapter;
#[cfg(feature = "iroh")]
pub mod iroh;
#[cfg(feature = "webrtc")]
pub mod webrtc;

use anyhow::Result;
pub use rustytransfer_transfer::PathObservation;

#[derive(Debug, Clone, serde::Serialize, PartialEq, Eq)]
pub struct PathEvidence {
    pub classification: &'static str,
    pub verified: bool,
    pub direct_stream_tx: u64,
    pub direct_stream_rx: u64,
    pub relay_stream_tx: u64,
    pub relay_stream_rx: u64,
    pub lagged: bool,
    pub missing_path_stats: bool,
    pub relay_selected: bool,
    /// Iroh connection and send-stall diagnostics, collected only in profile mode.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub connection_stats: Option<IrohConnectionEvidence>,
}

#[derive(Debug, Clone, serde::Serialize, PartialEq, Eq)]
pub struct IrohConnectionEvidence {
    pub sample_interval_ms: u64,
    pub sampling_mode: &'static str,
    pub samples: u64,
    pub selected_path_start: Option<String>,
    pub selected_path_end: Option<String>,
    pub selected_path_differs_at_end: bool,
    pub rtt_start_us: Option<u64>,
    pub rtt_end_us: Option<u64>,
    pub rtt_min_us: Option<u64>,
    pub rtt_max_us: Option<u64>,
    pub congestion_window_start_bytes: Option<u64>,
    pub congestion_window_end_bytes: Option<u64>,
    pub congestion_window_min_bytes: Option<u64>,
    pub congestion_window_max_bytes: Option<u64>,
    pub congestion_events_start: Option<u64>,
    pub congestion_events_end: Option<u64>,
    pub congestion_events_delta: Option<u64>,
    pub lost_packets_start: u64,
    pub lost_packets_end: u64,
    pub lost_packets_delta: u64,
    pub lost_bytes_start: u64,
    pub lost_bytes_end: u64,
    pub lost_bytes_delta: u64,
    pub udp_tx_datagrams_delta: u64,
    pub udp_tx_bytes_delta: u64,
    pub udp_rx_datagrams_delta: u64,
    pub udp_rx_bytes_delta: u64,
    pub data_blocked_frames_delta: u64,
    pub stream_data_blocked_frames_delta: u64,
    pub data_blocked_frames_rx_delta: u64,
    pub stream_data_blocked_frames_rx_delta: u64,
    pub send_wait_calls: u64,
    pub send_stalls_over_1ms: u64,
    pub send_stalls_over_10ms: u64,
    pub send_wait_max_us: u64,
    /// Iroh 1.2.0 does not expose these as public connection counters.
    pub unavailable_counters: Vec<&'static str>,
    pub counter_limitations: Vec<&'static str>,
}

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
    pub fn begin_payload_observation(&mut self) {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(_) => {}
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.begin_payload_observation(),
        }
    }

    pub fn end_payload_observation(&mut self) {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(_) => {}
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.end_payload_observation(),
        }
    }

    pub async fn take_path_evidence(&mut self) -> Option<PathEvidence> {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(_) => None,
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.take_path_evidence().await,
        }
    }

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

    pub fn close_connection_if_enabled(&mut self) -> bool {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(_) => false,
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.close_connection_if_enabled(),
        }
    }

    pub fn take_endpoint_close_seconds(&mut self) -> Option<f64> {
        match self {
            #[cfg(feature = "webrtc")]
            Self::WebRtc(_) => None,
            #[cfg(feature = "iroh")]
            Self::Iroh(state) => state.take_endpoint_close_seconds(),
        }
    }
}
