use anyhow::{Context, Result};
use just_webrtc::{
    DataChannelExt, PeerConnectionExt, SimpleLocalPeerConnection,
    platform::{Channel, PeerConnection},
    types::{PeerConnectionState, SessionDescription, SDPType},
};
use tokio::time::timeout;
use std::time::Duration;

use crate::transport::frames::Frame;
use crate::transport::websocket::WsRoomTransport;
use crate::transport::websocket::wait_for_room_full;
use crate::transport::webrtc::WebRtcState;
use crate::transport::webrtc::wait_for_sdp_frame;

/// Side A: connect once, return an open DataChannel in WebRtcState.
pub async fn connect_offerer(app_id: &str) -> Result<WebRtcState> {
    let ws = WsRoomTransport::new(app_id.to_string(), "A".to_string());
    let (mut write, mut read) = ws.connect_room().await?;

    let pc: PeerConnection = SimpleLocalPeerConnection::build(false).await?;

    // Create offer + ICE candidates
    let offer = pc
        .get_local_description()
        .await
        .context("missing local offer")?;
    let offer_candidates = pc.collect_ice_candidates().await?;
    // wait until the other peer is present
    timeout(Duration::from_secs(60), wait_for_room_full(&mut read)).await??;

    // Send offer
    WsRoomTransport::send_frame(
        &mut write,
        &Frame::Offer {
            sdp: offer.sdp.clone(),
            candidates: offer_candidates,
        },
    )
    .await?;

    // Wait for answer
    let (answer_sdp, answer_candidates) = wait_for_sdp_frame(&mut read, SDPType::Answer).await?;
    let answer = SessionDescription {
        sdp_type: SDPType::Answer,
        sdp: answer_sdp,
    };

    pc.set_remote_description(answer).await?;
    pc.add_ice_candidates(answer_candidates).await?;

    // Wait for connected
    while pc.state_change().await != PeerConnectionState::Connected {}

    // Data channel
    let ch: Channel = pc.receive_channel().await.context("no data channel")?;
    ch.wait_ready().await;

    Ok(WebRtcState { pc, ch })
}
