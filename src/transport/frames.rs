use serde::{Deserialize, Serialize};
use just_webrtc::types::ICECandidate;

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum Frame {
    Room_Full,
    Offer {
        sdp: String,
        #[serde(default)]
        candidates: Vec<ICECandidate>,
    },

    Answer {
        sdp: String,
        #[serde(default)]
        candidates: Vec<ICECandidate>,
    },
}

