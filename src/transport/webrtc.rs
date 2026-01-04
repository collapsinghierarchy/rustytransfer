use anyhow::Result;
use bytes::Bytes;
use just_webrtc::{DataChannelExt};
use just_webrtc::platform::{Channel, PeerConnection};
use just_webrtc::types::{ICECandidate, SDPType};

use crate::transport::frames::Frame;
use crate::transport::websocket::{WsRead, WsRoomTransport};

pub struct WebRtcState {
    pub pc: PeerConnection,
    pub ch: Channel,
}

impl WebRtcState {
    pub async fn send_vec(&self, data: Vec<u8>) -> Result<()> {
        let b = Bytes::from(data);
        self.ch.send(&b).await?;
        Ok(())
    }

    pub async fn recv_vec(&self) -> Result<Vec<u8>> {
        Ok(self.ch.receive().await?.to_vec())
    }
}

pub async fn wait_for_sdp_frame(
    read: &mut WsRead,
    expected: SDPType,
) -> Result<(String, Vec<ICECandidate>)> {
    loop {
        match WsRoomTransport::recv_frame(read).await? {
            Frame::Room_Full => continue,

            Frame::Offer { sdp, candidates } if expected == SDPType::Offer => {
                return Ok((sdp, candidates));
            }

            Frame::Answer { sdp, candidates } if expected == SDPType::Answer => {
                return Ok((sdp, candidates));
            }

            // ignore anything else and keep waiting
            _ => continue,
        }
    }
}