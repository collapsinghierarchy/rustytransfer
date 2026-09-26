use anyhow::{Context, Result, anyhow, bail, ensure};
use bytes::BytesMut;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::{Mutex as AsyncMutex, mpsc, oneshot, watch};
use tokio::task::JoinHandle;
use tokio::time::{Instant, sleep_until, timeout};
use webrtc::data_channel::{DataChannel, DataChannelEvent, RTCDataChannelInit};
use webrtc::peer_connection::{
    PeerConnection, PeerConnectionBuilder, PeerConnectionEventHandler, RTCConfigurationBuilder,
    RTCIceCandidateInit, RTCIceGatheringState, RTCIceServer, RTCPeerConnectionIceEvent, RTCSdpType,
    RTCSessionDescription,
};
use webrtc::runtime::default_runtime;

use crate::transport::PathObservation;
use crate::transport::frames::{Frame, SignaledIceCandidate};
use crate::transport::websocket::{WsRead, WsRoomTransport, wait_for_room_full};

/// The encrypted chunk stream uses a sequential nonce, so SCTP must preserve order.
pub const TRANSFER_DATA_CHANNEL_ORDERED: bool = true;

// --- Fragmentation settings (tune if needed) ---
const FRAG_MAGIC: &[u8; 4] = b"RTF1";

// Payload bytes per datachannel message.
// If you still see "outbound packet larger than maximum message size", lower this (e.g. 8*1024).
const CHUNK_PAYLOAD: usize = 16 * 1024;

// Safety / robustness
const MAX_REASSEMBLE_BYTES: usize = 1024 * 1024 + 16; // Max plaintext chunk plus AES-GCM tag.
const REASSEMBLE_TIMEOUT: Duration = Duration::from_secs(90);
const ICE_GATHER_TIMEOUT: Duration = Duration::from_secs(20);
const ICE_CANDIDATE_QUIET_PERIOD: Duration = Duration::from_secs(2);
const CHANNEL_OPEN_TIMEOUT: Duration = Duration::from_secs(90);
const CHANNEL_CLOSE_TIMEOUT: Duration = Duration::from_secs(10);
const DATA_CHANNEL_SEND_BUFFER_LIMIT: usize = 1024 * 1024;

type IncomingMessage = Result<Vec<u8>, String>;
type PeerConnectionFuture = Pin<Box<dyn Future<Output = Result<Arc<dyn PeerConnection>>> + Send>>;
type ChannelPump = (
    oneshot::Receiver<Result<(), String>>,
    mpsc::UnboundedReceiver<IncomingMessage>,
    watch::Receiver<bool>,
    JoinHandle<()>,
);

pub struct WebRtcState {
    pc: Arc<dyn PeerConnection>,
    ch: Arc<dyn DataChannel>,
    incoming: AsyncMutex<mpsc::UnboundedReceiver<IncomingMessage>>,
    peer_closed: watch::Receiver<bool>,
    pump: JoinHandle<()>,
}

impl Drop for WebRtcState {
    fn drop(&mut self) {
        self.pump.abort();
    }
}

impl WebRtcState {
    /// Attach an already negotiated data channel and wait until it can transfer data.
    ///
    /// # Errors
    ///
    /// Returns an error if the data channel closes before opening or does not open
    /// before the timeout.
    pub async fn new(pc: Arc<dyn PeerConnection>, ch: Arc<dyn DataChannel>) -> Result<Self> {
        let (opened, incoming, peer_closed, pump) = start_channel_pump(Arc::clone(&ch));
        let readiness = async {
            timeout(CHANNEL_OPEN_TIMEOUT, opened)
                .await
                .context("timed out waiting for WebRTC data channel to open")?
                .context("WebRTC data channel closed before opening")?
                .map_err(|reason| anyhow!(reason))
        }
        .await;
        if let Err(error) = readiness {
            pump.abort();
            drop(timeout(CHANNEL_CLOSE_TIMEOUT, ch.close()).await);
            drop(timeout(CHANNEL_CLOSE_TIMEOUT, pc.close()).await);
            return Err(error);
        }

        Ok(Self {
            pc,
            ch,
            incoming: AsyncMutex::new(incoming),
            peer_closed,
            pump,
        })
    }

    /// Sends one message, fragmenting payloads that exceed the data channel limit.
    ///
    /// # Errors
    ///
    /// Returns an error if any fragment cannot be sent.
    pub async fn send_vec(&self, data: Vec<u8>) -> Result<()> {
        ensure!(
            data.len() <= MAX_REASSEMBLE_BYTES,
            "WebRTC message exceeds transfer limit"
        );
        // Small payloads: send as a single message.
        if data.len() <= CHUNK_PAYLOAD {
            self.ch
                .send(BytesMut::from(data.as_slice()))
                .await
                .context("failed to send WebRTC data channel message")?;
            return Ok(());
        }

        // Large payloads: send a header then raw chunks.
        // Header = MAGIC(4) + total_len(u64 BE) => 12 bytes.
        let total_len = u64::try_from(data.len()).context("message length does not fit in u64")?;
        let mut hdr = Vec::with_capacity(12);
        hdr.extend_from_slice(FRAG_MAGIC);
        hdr.extend_from_slice(&total_len.to_be_bytes());

        self.ch
            .send(BytesMut::from(hdr.as_slice()))
            .await
            .context("failed to send WebRTC fragment header")?;

        for chunk in data.chunks(CHUNK_PAYLOAD) {
            self.ch
                .send(BytesMut::from(chunk))
                .await
                .context("failed to send WebRTC fragment")?;
        }

        Ok(())
    }

    /// Waits for the peer data channel to close.
    ///
    /// # Errors
    ///
    /// Returns an error if the close signal stream ends unexpectedly.
    pub async fn wait_for_peer_close(&self) -> Result<()> {
        let mut peer_closed = self.peer_closed.clone();
        while !*peer_closed.borrow() {
            peer_closed
                .changed()
                .await
                .context("WebRTC data channel close signal ended")?;
        }
        Ok(())
    }

    /// Report the selected ICE candidate types without exposing candidate addresses.
    ///
    /// # Errors
    ///
    /// Returns an error if the WebRTC implementation cannot inspect the selected
    /// candidate pair.
    pub async fn path_observation(&self) -> Result<PathObservation> {
        let Some(sctp) = self.pc.sctp().await else {
            return Ok(PathObservation::unknown());
        };
        let ice = sctp.transport().ice_transport();
        let Some(pair) = ice.get_selected_candidate_pair().await? else {
            return Ok(PathObservation::unknown());
        };

        let local_candidate_type = format!("{:?}", pair.local().typ).to_lowercase();
        let remote_candidate_type = format!("{:?}", pair.remote().typ).to_lowercase();
        let path = if local_candidate_type == "relay" || remote_candidate_type == "relay" {
            "relay"
        } else if local_candidate_type != "unspecified" && remote_candidate_type != "unspecified" {
            "direct"
        } else {
            "unknown"
        };

        Ok(PathObservation {
            path,
            local_candidate_type: Some(local_candidate_type),
            remote_candidate_type: Some(remote_candidate_type),
        })
    }

    /// Closes the data channel and then the peer connection.
    ///
    /// # Errors
    ///
    /// Returns an error if either close operation fails or times out.
    pub async fn close_transport(&self) -> Result<()> {
        let mut close_error = timeout(CHANNEL_CLOSE_TIMEOUT, self.ch.close())
            .await
            .context("timed out closing WebRTC data channel")
            .and_then(|result| result.context("failed to close WebRTC data channel"))
            .err();

        if let Err(error) = timeout(CHANNEL_CLOSE_TIMEOUT, self.wait_for_peer_close())
            .await
            .context("timed out waiting for WebRTC data channel to close")
            .and_then(|result| result)
        {
            close_error.get_or_insert(error);
        }

        if let Err(error) = timeout(CHANNEL_CLOSE_TIMEOUT, self.pc.close())
            .await
            .context("timed out closing WebRTC peer connection")
            .and_then(|result| result.context("failed to close WebRTC peer connection"))
        {
            close_error.get_or_insert(error);
        }

        self.pump.abort();
        close_error.map_or(Ok(()), Err)
    }

    async fn recv_message(&self) -> Result<Vec<u8>> {
        let mut incoming = self.incoming.lock().await;
        match incoming.recv().await {
            Some(Ok(message)) => Ok(message),
            Some(Err(reason)) => bail!("{reason}"),
            None => bail!("WebRTC data channel event stream ended"),
        }
    }

    /// Receives one message, reassembling fragmented payloads when necessary.
    ///
    /// # Errors
    ///
    /// Returns an error if the channel closes, the fragment header is invalid, or
    /// reassembly exceeds its size or time limit.
    pub async fn recv_vec(&self) -> Result<Vec<u8>> {
        let first_bytes = self.recv_message().await?;

        // Not a fragment header? return as-is.
        if first_bytes.len() != 12 || !first_bytes.starts_with(FRAG_MAGIC) {
            ensure!(
                first_bytes.len() <= MAX_REASSEMBLE_BYTES,
                "WebRTC message exceeds transfer limit"
            );
            return Ok(first_bytes);
        }

        let header_length = first_bytes
            .get(4..12)
            .ok_or_else(|| anyhow!("invalid fragment header"))?;
        let total_len = usize::try_from(u64::from_be_bytes(
            header_length
                .try_into()
                .context("invalid fragment header length")?,
        ))
        .context("fragment length does not fit in usize")?;
        ensure!(total_len > 0, "invalid fragmented total_len=0");
        ensure!(
            total_len <= MAX_REASSEMBLE_BYTES,
            "refusing to reassemble {total_len} bytes (cap={MAX_REASSEMBLE_BYTES})"
        );

        // Reassemble until we hit total_len, with a timeout to avoid hanging forever.
        let assembled = timeout(REASSEMBLE_TIMEOUT, async {
            let mut out = Vec::with_capacity(total_len);

            while out.len() < total_len {
                let chunk = self.recv_message().await?;

                // Under your stated protocol constraints this should never happen.
                if chunk.len() == 12 && chunk.starts_with(FRAG_MAGIC) {
                    bail!("unexpected fragment header during reassembly (concurrent recv or interleaving?)");
                }

                ensure!(
                    chunk.len() <= total_len.saturating_sub(out.len()),
                    "WebRTC fragment exceeds declared message length"
                );
                out.extend_from_slice(&chunk);
            }

            out.truncate(total_len);
            Ok::<_, anyhow::Error>(out)
        })
        .await
        .map_err(|error| anyhow!("timeout while reassembling fragmented payload: {error}"))??;

        Ok(assembled)
    }
}

// The upstream API reports all lifecycle and data events through DataChannel::poll().
// Pump them continuously so readiness handling cannot consume an early application message.
fn start_channel_pump(channel: Arc<dyn DataChannel>) -> ChannelPump {
    let (opened_tx, opened_rx) = oneshot::channel();
    let (incoming_tx, incoming_rx) = mpsc::unbounded_channel();
    let (peer_closed_tx, peer_closed_rx) = watch::channel(false);

    let pump = tokio::spawn(async move {
        let mut opened_tx = Some(opened_tx);
        while let Some(event) = channel.poll().await {
            match event {
                DataChannelEvent::OnOpen => {
                    if let Some(tx) = opened_tx.take() {
                        drop(tx.send(Ok(())));
                    }
                }
                DataChannelEvent::OnMessage(message) => {
                    if incoming_tx.send(Ok(message.data.to_vec())).is_err() {
                        return;
                    }
                }
                DataChannelEvent::OnClose => {
                    let reason = "WebRTC data channel closed".to_owned();
                    if peer_closed_tx.send(true).is_err() {
                        return;
                    }
                    if let Some(tx) = opened_tx.take() {
                        drop(tx.send(Err(reason.clone())));
                    }
                    drop(incoming_tx.send(Err(reason)));
                    return;
                }
                _ => {}
            }
        }

        if peer_closed_tx.send(true).is_err() {
            return;
        }
        let reason = "WebRTC data channel event stream ended".to_owned();
        if let Some(tx) = opened_tx.take() {
            drop(tx.send(Err(reason.clone())));
        }
        drop(incoming_tx.send(Err(reason)));
    });
    (opened_rx, incoming_rx, peer_closed_rx, pump)
}

struct PeerEvents {
    local_candidates: Mutex<Vec<SignaledIceCandidate>>,
    candidate_update_tx: mpsc::UnboundedSender<()>,
    gathering_complete_tx: mpsc::UnboundedSender<()>,
    incoming_data_channel_tx: mpsc::UnboundedSender<Arc<dyn DataChannel>>,
}

#[async_trait::async_trait]
impl PeerConnectionEventHandler for PeerEvents {
    async fn on_ice_candidate(&self, event: RTCPeerConnectionIceEvent) {
        if let Ok(candidate) = event.candidate.to_json()
            && !candidate.candidate.is_empty()
            && let Ok(mut candidates) = self.local_candidates.lock()
        {
            candidates.push(candidate.into());
            if self.candidate_update_tx.send(()).is_err() {
                return;
            }
        }
    }

    async fn on_ice_gathering_state_change(&self, state: RTCIceGatheringState) {
        if state == RTCIceGatheringState::Complete && self.gathering_complete_tx.send(()).is_err() {
            return;
        }
    }

    async fn on_data_channel(&self, channel: Arc<dyn DataChannel>) {
        drop(self.incoming_data_channel_tx.send(channel));
    }
}

struct PeerEventReceivers {
    candidate_updates: mpsc::UnboundedReceiver<()>,
    gathering_complete: mpsc::UnboundedReceiver<()>,
    incoming_data_channel: mpsc::UnboundedReceiver<Arc<dyn DataChannel>>,
}

fn new_peer_events() -> (Arc<PeerEvents>, PeerEventReceivers) {
    let (candidate_update_tx, candidate_updates) = mpsc::unbounded_channel();
    let (gathering_complete_tx, gathering_complete) = mpsc::unbounded_channel();
    let (incoming_data_channel_tx, incoming_data_channel) = mpsc::unbounded_channel();
    let events = Arc::new(PeerEvents {
        local_candidates: Mutex::new(Vec::new()),
        candidate_update_tx,
        gathering_complete_tx,
        incoming_data_channel_tx,
    });

    (
        events,
        PeerEventReceivers {
            candidate_updates,
            gathering_complete,
            incoming_data_channel,
        },
    )
}

fn new_peer_connection(events: Arc<PeerEvents>) -> PeerConnectionFuture {
    Box::pin(async move {
        let configuration = RTCConfigurationBuilder::new()
            .with_ice_servers(vec![RTCIceServer {
                urls: vec!["stun:stun.l.google.com:19302".to_owned()],
                ..Default::default()
            }])
            .build();
        let runtime = default_runtime().ok_or_else(|| anyhow!("no WebRTC runtime is available"))?;
        // Clippy baseline: the upstream WebRTC builder future is large by design.
        let pc = PeerConnectionBuilder::new()
            .with_configuration(configuration)
            .with_handler(events)
            .with_runtime(runtime)
            .with_udp_addrs(vec!["0.0.0.0:0".to_owned()])
            .with_data_channel_send_buffer_limit(DATA_CHANNEL_SEND_BUFFER_LIMIT)
            .build()
            .await?;

        let pc: Arc<dyn PeerConnection> = Arc::new(pc);
        Ok(pc)
    })
}

async fn wait_for_ice_gathering(
    events: &PeerEvents,
    candidate_updates: &mut mpsc::UnboundedReceiver<()>,
    gathering_complete: &mut mpsc::UnboundedReceiver<()>,
) -> Result<Vec<SignaledIceCandidate>> {
    let deadline = Instant::now()
        .checked_add(ICE_GATHER_TIMEOUT)
        .ok_or_else(|| anyhow!("ICE gathering deadline overflowed"))?;
    let mut last_candidate_at: Option<Instant> = None;

    loop {
        let candidate_quiet_deadline =
            last_candidate_at.and_then(|at| at.checked_add(ICE_CANDIDATE_QUIET_PERIOD));
        tokio::select! {
            complete = gathering_complete.recv() => match complete {
                Some(()) => break,
                None => bail!("WebRTC ICE gathering state event stream ended"),
            },
            candidate = candidate_updates.recv() => match candidate {
                Some(()) => last_candidate_at = Some(Instant::now()),
                None => bail!("WebRTC ICE candidate event stream ended"),
            },
            () = sleep_until(candidate_quiet_deadline.unwrap_or(deadline)), if candidate_quiet_deadline.is_some() => {
                let candidate_count = candidate_count(events)?;
                eprintln!(
                    "WebRTC ICE gathering did not emit Complete; proceeding after {}s without new candidates ({candidate_count} local candidates)",
                    ICE_CANDIDATE_QUIET_PERIOD.as_secs()
                );
                break;
            },
            () = sleep_until(deadline) => {
                let candidate_count = candidate_count(events)?;
                bail!(
                    "timed out waiting for WebRTC ICE gathering to complete ({candidate_count} local candidates received; no candidate-quiescence fallback was available)"
                );
            },
        }
    }

    let candidates = events
        .local_candidates
        .lock()
        .map(|candidates| candidates.clone())
        .map_err(|error| anyhow!("WebRTC candidate list lock was poisoned: {error}"))?;
    ensure!(
        !candidates.is_empty(),
        "WebRTC ICE gathering completed without any local candidates"
    );
    Ok(candidates)
}

fn candidate_count(events: &PeerEvents) -> Result<usize> {
    events
        .local_candidates
        .lock()
        .map(|candidates| candidates.len())
        .map_err(|error| anyhow!("WebRTC candidate list lock was poisoned: {error}"))
}

async fn add_remote_candidates(
    pc: &dyn PeerConnection,
    candidates: Vec<SignaledIceCandidate>,
) -> Result<()> {
    for candidate in candidates {
        let candidate: RTCIceCandidateInit = candidate.into();
        pc.add_ice_candidate(candidate).await?;
    }
    Ok(())
}

/// Waits for a signaling frame of the expected SDP type.
///
/// # Errors
///
/// Returns an error if the signaling WebSocket closes or yields invalid data.
pub async fn wait_for_sdp_frame(
    read: &mut WsRead,
    expected: RTCSdpType,
) -> Result<(String, Vec<SignaledIceCandidate>)> {
    loop {
        match WsRoomTransport::recv_frame(read).await? {
            Frame::Offer { sdp, candidates } if expected == RTCSdpType::Offer => {
                return Ok((sdp, candidates));
            }

            Frame::Answer { sdp, candidates } if expected == RTCSdpType::Answer => {
                return Ok((sdp, candidates));
            }

            _ => {}
        }
    }
}

/// Side A: connect once and return an open `DataChannel` in `WebRtcState`.
///
/// # Errors
///
/// Returns an error if signaling, ICE negotiation, or channel setup fails.
pub async fn connect_offerer(app_id: &str) -> Result<WebRtcState> {
    let ws = WsRoomTransport::new(app_id.to_string(), "A".to_string());
    let (mut write, mut read) = ws.connect_room().await?;

    // The transfer protocol encrypts chunks with a sequential nonce stream,
    // so an unordered SCTP data channel could deliver chunks in the wrong order.
    let (events, mut receivers) = new_peer_events();
    let pc = new_peer_connection(Arc::clone(&events)).await?;
    let ch = pc
        .create_data_channel(
            "simple_channel_0",
            Some(RTCDataChannelInit {
                ordered: TRANSFER_DATA_CHANNEL_ORDERED,
                ..Default::default()
            }),
        )
        .await?;

    // Keep this original SDP and signal gathered candidates separately, as the existing
    // WebSocket protocol expects.
    let offer = pc.create_offer(None).await?;
    pc.set_local_description(offer.clone()).await?;
    let offer_candidates = wait_for_ice_gathering(
        &events,
        &mut receivers.candidate_updates,
        &mut receivers.gathering_complete,
    )
    .await?;

    // Wait until the other peer is present.
    timeout(Duration::from_mins(1), wait_for_room_full(&mut read)).await??;

    WsRoomTransport::send_frame(
        &mut write,
        &Frame::Offer {
            sdp: offer.sdp,
            candidates: offer_candidates,
        },
    )
    .await?;

    let (answer_sdp, answer_candidates) = wait_for_sdp_frame(&mut read, RTCSdpType::Answer).await?;
    pc.set_remote_description(RTCSessionDescription::answer(answer_sdp)?)
        .await?;
    add_remote_candidates(pc.as_ref(), answer_candidates).await?;

    WebRtcState::new(pc, ch).await
}

/// Side B: connect once and return an open `DataChannel` in `WebRtcState`.
///
/// # Errors
///
/// Returns an error if signaling, ICE negotiation, or channel setup fails.
pub async fn connect_answerer(app_id: &str) -> Result<WebRtcState> {
    let ws = WsRoomTransport::new(app_id.to_string(), "B".to_string());
    let (mut write, mut read) = ws.connect_room().await?;

    let (offer_sdp, offer_candidates) = wait_for_sdp_frame(&mut read, RTCSdpType::Offer).await?;
    let offer = RTCSessionDescription::offer(offer_sdp)?;

    let (events, mut receivers) = new_peer_events();
    let pc = new_peer_connection(Arc::clone(&events)).await?;
    pc.set_remote_description(offer).await?;
    add_remote_candidates(pc.as_ref(), offer_candidates).await?;

    let answer = pc.create_answer(None).await?;
    pc.set_local_description(answer.clone()).await?;
    let answer_candidates = wait_for_ice_gathering(
        &events,
        &mut receivers.candidate_updates,
        &mut receivers.gathering_complete,
    )
    .await?;

    WsRoomTransport::send_frame(
        &mut write,
        &Frame::Answer {
            sdp: answer.sdp,
            candidates: answer_candidates,
        },
    )
    .await?;

    let ch = match timeout(CHANNEL_OPEN_TIMEOUT, receivers.incoming_data_channel.recv()).await {
        Ok(Some(channel)) => channel,
        Ok(None) => {
            drop(timeout(CHANNEL_CLOSE_TIMEOUT, pc.close()).await);
            return Err(anyhow!(
                "WebRTC peer connection closed before receiving a data channel"
            ));
        }
        Err(error) => {
            drop(timeout(CHANNEL_CLOSE_TIMEOUT, pc.close()).await);
            return Err(error).context("timed out waiting for incoming WebRTC data channel");
        }
    };

    WebRtcState::new(pc, ch).await
}
