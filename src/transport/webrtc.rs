use anyhow::Result;
use just_webrtc::{
    DataChannelExt,
    PeerConnectionExt,
    SimpleLocalPeerConnection,
    types::{SessionDescription, ICECandidate, PeerConnectionState}
};

pub async fn run_remote_peer() -> anyhow::Result<()> {
    // create remote peer with one unordered data channel
    let remote = SimpleLocalPeerConnection::build(true).await?;
    
    // ... receive (offer, candidates) via your signalling (your WS backend) ...
    /*
    remote.set_remote_description(offer).await?;
    remote.add_ice_candidates(candidates).await?;

    let answer = remote.get_local_description().await.unwrap();
    let answer_candidates = remote.collect_ice_candidates().await?;

    // ... send (answer, answer_candidates) back via your signalling ...

    while remote.state_change().await != PeerConnectionState::Connected {}

    let channel = remote.receive_channel().await.unwrap();
    channel.wait_ready().await;
    let msg = channel.receive().await?;
    println!("Got message from remote: {}", String::from_utf8_lossy(&msg));
    */
    Ok(())
}

async fn run_local_peer() -> anyhow::Result<()> {
    // create local peer with one unordered data channel
    let local = SimpleLocalPeerConnection::build(false).await?;
    let offer = local.get_local_description().await.unwrap();
    let candidates = local.collect_ice_candidates().await?;

    // ... send (offer, candidates) via your signalling (your WS backend) ...

    // ... receive (answer, answer_candidates) ...
    /* 
    local.set_remote_description(answer).await?;
    local.add_ice_candidates(answer_candidates).await?;

    while local.state_change().await != PeerConnectionState::Connected {}

    let channel = local.receive_channel().await.unwrap();
    channel.wait_ready().await;
    channel.send("hello remote!".into()).await?;
    */
    Ok(())
}