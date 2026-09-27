#[cfg(feature = "iroh")]
use iroh::EndpointAddr;
use serde::{Deserialize, Serialize};
#[cfg(feature = "webrtc")]
use webrtc::peer_connection::RTCIceCandidateInit;

/// ICE candidate fields in the signaling format used by the existing clients.
/// Keep `username_fragment` snake-cased for compatibility with existing signaling payloads.
#[cfg(feature = "webrtc")]
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SignaledIceCandidate {
    pub candidate: String,
    pub sdp_mid: Option<String>,
    #[serde(rename = "sdpMLineIndex")]
    pub sdp_mline_index: Option<u16>,
    pub username_fragment: Option<String>,
}

#[cfg(feature = "webrtc")]
impl From<RTCIceCandidateInit> for SignaledIceCandidate {
    fn from(candidate: RTCIceCandidateInit) -> Self {
        Self {
            candidate: candidate.candidate,
            sdp_mid: candidate.sdp_mid,
            sdp_mline_index: candidate.sdp_mline_index,
            username_fragment: candidate.username_fragment,
        }
    }
}

#[cfg(feature = "webrtc")]
impl From<SignaledIceCandidate> for RTCIceCandidateInit {
    fn from(candidate: SignaledIceCandidate) -> Self {
        Self {
            candidate: candidate.candidate,
            sdp_mid: candidate.sdp_mid,
            sdp_mline_index: candidate.sdp_mline_index,
            username_fragment: candidate.username_fragment,
            url: None,
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum Frame {
    // Keep the signaling wire value even though the Rust variant uses standard casing.
    #[serde(rename = "room_full")]
    RoomFull,
    #[cfg(feature = "webrtc")]
    Offer {
        sdp: String,
        #[serde(default)]
        candidates: Vec<SignaledIceCandidate>,
    },

    #[cfg(feature = "webrtc")]
    Answer {
        sdp: String,
        #[serde(default)]
        candidates: Vec<SignaledIceCandidate>,
    },

    #[cfg(feature = "iroh")]
    #[serde(rename = "iroh_offer")]
    IrohOffer { addr: EndpointAddr },
}
