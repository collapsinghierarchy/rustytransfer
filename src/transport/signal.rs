use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum SignalFrame {
    Room_Full,
    Offer { sdp: String },
    //Answer { sdp: String },
    //Ice { candidate: String }, 
    // Also possible -> Hello { .. }, Send { .. }, Delivered { .. }, Telemetry { .. }
}

