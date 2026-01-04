use anyhow::{Context, Result};
use just_webrtc::{
    DataChannelExt, PeerConnectionExt, SimpleRemotePeerConnection,
    platform::{Channel, PeerConnection},
    types::{PeerConnectionState, SessionDescription, SDPType},
};

use crate::transport::frames::Frame;
use crate::transport::websocket::WsRoomTransport;
use crate::transport::webrtc::WebRtcState;
use crate::transport::webrtc::wait_for_sdp_frame;

/// Side B: connect once, return an open DataChannel in WebRtcState.
pub async fn connect_answerer(app_id: &str) -> Result<WebRtcState> {
    let ws = WsRoomTransport::new(app_id.to_string(), "B".to_string());
    let (mut write, mut read) = ws.connect_room().await?;

    // Wait for offer
    let (offer_sdp, offer_candidates) = wait_for_sdp_frame(&mut read, SDPType::Offer).await?;
    let offer = SessionDescription {
        sdp_type: SDPType::Offer,
        sdp: offer_sdp,
    };
    
    let pc: PeerConnection = SimpleRemotePeerConnection::build(offer).await?;
    pc.add_ice_candidates(offer_candidates).await?;

    // Create answer + ICE candidates
    let answer = pc
        .get_local_description()
        .await
        .context("missing local answer")?;
    let answer_candidates = pc.collect_ice_candidates().await?;

    // Send answer
    WsRoomTransport::send_frame(
        &mut write,
        &Frame::Answer {
            sdp: answer.sdp.clone(),
            candidates: answer_candidates,
        },
    )
    .await?;

    // Wait for connected
    while pc.state_change().await != PeerConnectionState::Connected {}

    // Data channel
    let ch: Channel = pc.receive_channel().await.context("no data channel")?;
    ch.wait_ready().await;

    Ok(WebRtcState { pc, ch })
}
