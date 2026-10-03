use anyhow::{Context, Result, anyhow, ensure};
use futures_util::StreamExt;
use iroh::{
    Endpoint, EndpointAddr, EndpointId, SecretKey,
    endpoint::{Connection, QuicTransportConfig, RecvStream, SendStream, VarInt, presets},
};
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    time::Instant,
};
use tokio::io::AsyncWriteExt;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tokio::time::{Duration, timeout};

use crate::transport::errors::TransportError;
use crate::transport::frames::Frame;
use crate::transport::websocket::{WsRead, WsRoomTransport, wait_for_room_full};
use crate::transport::{IrohConnectionEvidence, PathEvidence, PathObservation};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(90);
const ENDPOINT_READY_TIMEOUT: Duration = Duration::from_secs(10);
const ENDPOINT_CLOSE_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_MESSAGE_BYTES: usize = 1024 * 1024 + 16; // Max plaintext chunk plus AES-GCM tag.
const IROH_ALPN: &[u8] = b"rustytransfer/2";
const DIRECT_ALPN: &[u8] = b"rustytransfer/direct/2";
pub const IROH_PROBE_ALPN: &[u8] = b"rustytransfer/probe/1";
const CONNECTION_SAMPLE_INTERVAL: Duration = Duration::from_millis(250);
const STREAM_WINDOW_ENV: &str = "RUSTYTRANSFER_BENCH_STREAM_WINDOW_BYTES";
const MIN_STREAM_WINDOW_BYTES: u32 = 1_250_000;
const MAX_STREAM_WINDOW_BYTES: u32 = 5_000_000;
const CONNECTION_RECEIVE_WINDOW_BYTES: u32 = 5_000_000;

/// Return the per-user key path used by direct transfers.
pub fn default_identity_path() -> Result<PathBuf> {
    #[cfg(windows)]
    let base = std::env::var_os("LOCALAPPDATA").map(PathBuf::from);
    #[cfg(not(windows))]
    let base = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/share")));
    Ok(base
        .ok_or_else(|| anyhow!("no user data directory; pass --identity-file"))?
        .join("rustytransfer/iroh-identity.key"))
}

/// Load a persistent Iroh identity, creating it atomically on first use.
pub fn load_or_create_key(path: &Path) -> Result<SecretKey> {
    match fs::read(path) {
        Ok(bytes) => {
            let bytes: [u8; 32] = bytes.try_into().map_err(|_bytes| {
                anyhow!(
                    "Iroh key file must contain exactly 32 bytes: {}",
                    path.display()
                )
            })?;
            Ok(SecretKey::from_bytes(&bytes))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            if let Some(parent) = path
                .parent()
                .filter(|parent| !parent.as_os_str().is_empty())
            {
                fs::create_dir_all(parent)?;
            }
            let key = SecretKey::generate();
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            options.mode(0o600);
            match options.open(path) {
                Ok(mut file) => {
                    file.write_all(&key.to_bytes())?;
                    file.sync_all()?;
                    Ok(key)
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    load_or_create_key(path)
                }
                Err(error) => {
                    Err(error).with_context(|| format!("cannot create {}", path.display()))
                }
            }
        }
        Err(error) => Err(error).with_context(|| format!("cannot read {}", path.display())),
    }
}

/// Validate an Iroh endpoint ID without starting network activity.
pub fn validate_endpoint_id(peer_id: &str) -> Result<()> {
    let _parsed: EndpointId = peer_id.parse().context("invalid Iroh EndpointId")?;
    Ok(())
}

/// A reliable, bidirectional, message-oriented view over one Iroh QUIC stream.
// Clippy baseline: the transport name distinguishes this public state type.
pub struct IrohState {
    // Keep both handles alive for the lifetime of the transfer.
    endpoint: Endpoint,
    connection: Connection,
    send: SendStream,
    recv: RecvStream,
    path_evidence: Option<PathEvidenceSession>,
    connection_profile: Option<Box<IrohConnectionProfile>>,
}

#[derive(Clone, Default)]
struct ConnectionSample {
    path_id: Option<String>,
    rtt_us: Option<u64>,
    congestion_window_bytes: Option<u64>,
    congestion_events: Option<u64>,
    lost_packets: u64,
    lost_bytes: u64,
    udp_tx_datagrams: u64,
    udp_tx_bytes: u64,
    udp_rx_datagrams: u64,
    udp_rx_bytes: u64,
    data_blocked_frames: u64,
    stream_data_blocked_frames: u64,
    data_blocked_frames_rx: u64,
    stream_data_blocked_frames_rx: u64,
}

struct IrohConnectionProfile {
    started: ConnectionSample,
    ended: Option<ConnectionSample>,
    last_sample: Instant,
    samples: u64,
    rtt_min_us: Option<u64>,
    rtt_max_us: Option<u64>,
    congestion_window_min_bytes: Option<u64>,
    congestion_window_max_bytes: Option<u64>,
    send_wait_calls: u64,
    send_stalls_over_1ms: u64,
    send_stalls_over_10ms: u64,
    send_wait_max_us: u64,
}

impl IrohConnectionProfile {
    fn new(connection: &Connection) -> Self {
        let started = connection_sample(connection);
        let mut profile = Self {
            started: started.clone(),
            ended: None,
            last_sample: Instant::now(),
            samples: 0,
            rtt_min_us: None,
            rtt_max_us: None,
            congestion_window_min_bytes: None,
            congestion_window_max_bytes: None,
            send_wait_calls: 0,
            send_stalls_over_1ms: 0,
            send_stalls_over_10ms: 0,
            send_wait_max_us: 0,
        };
        profile.record_sample(started.clone());
        profile
    }

    fn sample_if_due(&mut self, connection: &Connection) {
        if self.ended.is_none() && self.last_sample.elapsed() >= CONNECTION_SAMPLE_INTERVAL {
            self.last_sample = Instant::now();
            self.record_sample(connection_sample(connection));
        }
    }

    fn record_sample(&mut self, sample: ConnectionSample) {
        self.samples = self.samples.saturating_add(1);
        if let Some(rtt) = sample.rtt_us {
            self.rtt_min_us = Some(self.rtt_min_us.map_or(rtt, |value| value.min(rtt)));
            self.rtt_max_us = Some(self.rtt_max_us.map_or(rtt, |value| value.max(rtt)));
        }
        if let Some(window) = sample.congestion_window_bytes {
            self.congestion_window_min_bytes = Some(
                self.congestion_window_min_bytes
                    .map_or(window, |value| value.min(window)),
            );
            self.congestion_window_max_bytes = Some(
                self.congestion_window_max_bytes
                    .map_or(window, |value| value.max(window)),
            );
        }
    }

    fn record_send_wait(&mut self, duration: Duration) {
        if self.ended.is_some() {
            return;
        }
        let micros = u64::try_from(duration.as_micros()).unwrap_or(u64::MAX);
        self.send_wait_calls = self.send_wait_calls.saturating_add(1);
        self.send_wait_max_us = self.send_wait_max_us.max(micros);
        if duration > Duration::from_millis(1) {
            self.send_stalls_over_1ms = self.send_stalls_over_1ms.saturating_add(1);
        }
        if duration > Duration::from_millis(10) {
            self.send_stalls_over_10ms = self.send_stalls_over_10ms.saturating_add(1);
        }
    }

    fn finish(&mut self, connection: &Connection) {
        self.finish_with_sample(connection_sample(connection));
    }

    fn finish_with_sample(&mut self, ended: ConnectionSample) {
        self.record_sample(ended.clone());
        self.ended = Some(ended);
    }

    fn into_evidence(self) -> IrohConnectionEvidence {
        let ended = self.ended.unwrap_or_default();
        let selected_path_differs_at_end = self.started.path_id != ended.path_id;
        IrohConnectionEvidence {
            sample_interval_ms: u64::try_from(CONNECTION_SAMPLE_INTERVAL.as_millis())
                .unwrap_or(u64::MAX),
            sampling_mode: "start/end plus message-boundary samples at least 250ms apart",
            samples: self.samples,
            selected_path_start: self.started.path_id,
            selected_path_end: ended.path_id.clone(),
            selected_path_differs_at_end,
            rtt_start_us: self.started.rtt_us,
            rtt_end_us: ended.rtt_us,
            rtt_min_us: self.rtt_min_us,
            rtt_max_us: self.rtt_max_us,
            congestion_window_start_bytes: self.started.congestion_window_bytes,
            congestion_window_end_bytes: ended.congestion_window_bytes,
            congestion_window_min_bytes: self.congestion_window_min_bytes,
            congestion_window_max_bytes: self.congestion_window_max_bytes,
            congestion_events_start: self.started.congestion_events,
            congestion_events_end: ended.congestion_events,
            congestion_events_delta: if selected_path_differs_at_end {
                None
            } else {
                self.started
                    .congestion_events
                    .zip(ended.congestion_events)
                    .map(|(start, end)| end.saturating_sub(start))
            },
            lost_packets_start: self.started.lost_packets,
            lost_packets_end: ended.lost_packets,
            lost_packets_delta: ended.lost_packets.saturating_sub(self.started.lost_packets),
            lost_bytes_start: self.started.lost_bytes,
            lost_bytes_end: ended.lost_bytes,
            lost_bytes_delta: ended.lost_bytes.saturating_sub(self.started.lost_bytes),
            udp_tx_datagrams_delta: ended
                .udp_tx_datagrams
                .saturating_sub(self.started.udp_tx_datagrams),
            udp_tx_bytes_delta: ended.udp_tx_bytes.saturating_sub(self.started.udp_tx_bytes),
            udp_rx_datagrams_delta: ended
                .udp_rx_datagrams
                .saturating_sub(self.started.udp_rx_datagrams),
            udp_rx_bytes_delta: ended.udp_rx_bytes.saturating_sub(self.started.udp_rx_bytes),
            data_blocked_frames_delta: ended
                .data_blocked_frames
                .saturating_sub(self.started.data_blocked_frames),
            stream_data_blocked_frames_delta: ended
                .stream_data_blocked_frames
                .saturating_sub(self.started.stream_data_blocked_frames),
            data_blocked_frames_rx_delta: ended
                .data_blocked_frames_rx
                .saturating_sub(self.started.data_blocked_frames_rx),
            stream_data_blocked_frames_rx_delta: ended
                .stream_data_blocked_frames_rx
                .saturating_sub(self.started.stream_data_blocked_frames_rx),
            send_wait_calls: self.send_wait_calls,
            send_stalls_over_1ms: self.send_stalls_over_1ms,
            send_stalls_over_10ms: self.send_stalls_over_10ms,
            send_wait_max_us: self.send_wait_max_us,
            unavailable_counters: vec![
                "bytes_in_flight",
                "congestion_blocked_duration",
                "stream_flow_control_credit_bytes",
                "retransmission_duration",
            ],
            counter_limitations: vec![
                "noq 1.3.0 does not emit TX DATA_BLOCKED or STREAM_DATA_BLOCKED frames; zero TX deltas do not prove flow control was unblocked",
            ],
        }
    }
}

fn connection_sample(connection: &Connection) -> ConnectionSample {
    let stats = connection.stats();
    let paths = connection.paths();
    let selected = paths.iter().find(iroh::endpoint::Path::is_selected);
    let path_stats = selected.as_ref().map(iroh::endpoint::Path::stats);
    ConnectionSample {
        path_id: selected.as_ref().map(|path| path.id().to_string()),
        rtt_us: path_stats.and_then(|stats| u64::try_from(stats.rtt.as_micros()).ok()),
        congestion_window_bytes: path_stats.map(|stats| stats.cwnd),
        congestion_events: path_stats.map(|stats| stats.congestion_events),
        lost_packets: stats.lost_packets,
        lost_bytes: stats.lost_bytes,
        udp_tx_datagrams: stats.udp_tx.datagrams,
        udp_tx_bytes: stats.udp_tx.bytes,
        udp_rx_datagrams: stats.udp_rx.datagrams,
        udp_rx_bytes: stats.udp_rx.bytes,
        data_blocked_frames: stats.frame_tx.data_blocked,
        stream_data_blocked_frames: stats.frame_tx.stream_data_blocked,
        data_blocked_frames_rx: stats.frame_rx.data_blocked,
        stream_data_blocked_frames_rx: stats.frame_rx.stream_data_blocked,
    }
}

struct PathSample {
    id: String,
    kind: PathKind,
    selected: bool,
    stream_tx: u64,
    stream_rx: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PathKind {
    Direct,
    Relay,
    Unknown,
}

struct PathEventSummary {
    closed: Vec<PathSample>,
    opened: Vec<(String, PathKind)>,
    selected: Vec<(String, PathKind)>,
    lagged: bool,
}

struct PathEvidenceSession {
    baseline: Vec<PathSample>,
    ending: Vec<PathSample>,
    stop: Option<oneshot::Sender<()>>,
    task: Option<JoinHandle<PathEventSummary>>,
}

impl Drop for PathEvidenceSession {
    fn drop(&mut self) {
        if let Some(stop) = self.stop.take() {
            let _stop_sent = stop.send(()).is_ok();
        }
        if let Some(task) = self.task.as_ref() {
            task.abort();
        }
    }
}

impl IrohState {
    fn new(endpoint: Endpoint, connection: Connection, send: SendStream, recv: RecvStream) -> Self {
        Self {
            endpoint,
            connection,
            send,
            recv,
            path_evidence: None,
            connection_profile: None,
        }
    }

    pub fn begin_payload_observation(&mut self) {
        if std::env::var("RUSTYTRANSFER_BENCH_PATH_EVIDENCE").as_deref() != Ok("1") {
            return;
        }
        let mut events = self.connection.path_events();
        let baseline = snapshot_paths(&self.connection);
        let (stop, mut stop_rx) = oneshot::channel();
        let task = tokio::spawn(async move {
            let mut summary = PathEventSummary {
                closed: Vec::new(),
                opened: Vec::new(),
                selected: Vec::new(),
                lagged: false,
            };
            loop {
                tokio::select! {
                    biased;
                    event = events.next() => {
                        let Some(event) = event else { break };
                        use iroh::endpoint::PathEvent;
                        match event {
                            PathEvent::Opened { id, remote_addr, .. } => {
                                summary.opened.push((id.to_string(), path_kind(remote_addr.is_ip(), remote_addr.is_relay())));
                            }
                            PathEvent::Closed { id, remote_addr, last_stats, .. } => {
                                summary.closed.push(PathSample {
                                    id: id.to_string(),
                                    kind: path_kind(remote_addr.is_ip(), remote_addr.is_relay()),
                                    selected: false,
                                    stream_tx: last_stats.frame_tx.stream,
                                    stream_rx: last_stats.frame_rx.stream,
                                });
                            }
                            PathEvent::Selected { id, remote_addr, .. } => {
                                summary.selected.push((id.to_string(), path_kind(remote_addr.is_ip(), remote_addr.is_relay())));
                            }
                            PathEvent::Lagged { .. } => summary.lagged = true,
                            _ => summary.lagged = true,
                        }
                    }
                    _ = &mut stop_rx => break,
                }
            }
            summary
        });
        self.path_evidence = Some(PathEvidenceSession {
            baseline,
            ending: Vec::new(),
            stop: Some(stop),
            task: Some(task),
        });
        self.connection_profile = std::env::var("RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE")
            .is_ok_and(|value| value == "1")
            .then(|| Box::new(IrohConnectionProfile::new(&self.connection)));
    }

    pub fn end_payload_observation(&mut self) {
        if let Some(profile) = self.connection_profile.as_mut() {
            profile.finish(&self.connection);
        }
        if let Some(session) = self.path_evidence.as_mut() {
            session.ending = snapshot_paths(&self.connection);
            if let Some(stop) = session.stop.take() {
                let _stop_sent = stop.send(()).is_ok();
            }
        }
    }

    pub async fn take_path_evidence(&mut self) -> Option<PathEvidence> {
        let mut session = self.path_evidence.take()?;
        let task = session.task.take()?;
        let summary = task.await.ok()?;
        let mut evidence = classify_path_evidence(&session.baseline, &session.ending, &summary);
        evidence.connection_stats = self
            .connection_profile
            .take()
            .map(|profile| (*profile).into_evidence());
        Some(evidence)
    }

    /// Sends one length-prefixed message over the QUIC stream.
    ///
    /// # Errors
    ///
    /// Returns an error if the message is too large or the stream cannot be written.
    pub async fn send_vec(&mut self, data: Vec<u8>) -> Result<()> {
        ensure!(
            data.len() <= MAX_MESSAGE_BYTES,
            "message is too large: {} bytes (maximum {})",
            data.len(),
            MAX_MESSAGE_BYTES
        );

        let started = self
            .connection_profile
            .as_ref()
            .filter(|profile| profile.ended.is_none())
            .map(|_| Instant::now());
        let result = async {
            let length = u32::try_from(data.len()).context("message length exceeds u32")?;
            self.send.write_all(&length.to_be_bytes()).await?;
            self.send.write_all(&data).await?;
            self.send.flush().await?;
            Result::<()>::Ok(())
        }
        .await;
        if let (Some(profile), Some(started)) = (&mut self.connection_profile, started) {
            profile.record_send_wait(started.elapsed());
            profile.sample_if_due(&self.connection);
        }
        result?;
        Ok(())
    }

    /// Report the currently selected Iroh path. The path list is a live snapshot and may
    /// change after hole punching, so callers should sample it after transfer setup.
    pub fn path_observation(&self) -> PathObservation {
        self.connection
            .paths()
            .iter()
            .find(iroh::endpoint::Path::is_selected)
            .map_or_else(PathObservation::unknown, |path| PathObservation {
                path: if path.is_ip() {
                    "direct"
                } else if path.is_relay() {
                    "relay"
                } else {
                    "unknown"
                },
                local_candidate_type: None,
                remote_candidate_type: None,
            })
    }

    /// Finish the sending half. Iroh's `AsyncWrite::flush` is a no-op, so the
    /// peer must read through EOF before either side drops the connection.
    ///
    /// # Errors
    ///
    /// Returns an error if the stream cannot be finished.
    pub fn close_send(&mut self) -> Result<()> {
        self.send
            .finish()
            .context("failed to finish Iroh send stream")?;
        Ok(())
    }

    /// Finish the sending half and wait until the peer has consumed it.
    ///
    /// # Errors
    ///
    /// Returns an error if finishing or delivery acknowledgement fails.
    pub async fn finish_send(&mut self) -> Result<()> {
        self.close_send()?;
        let stopped = self
            .send
            .stopped()
            .await
            .context("waiting for Iroh stream delivery failed")?;
        ensure!(stopped.is_none(), "peer stopped the Iroh stream early");
        Ok(())
    }

    /// Keep the connection alive until the peer has finished draining its send stream.
    ///
    /// The sender calls this after reading EOF from the peer. That EOF is emitted when the
    /// peer finishes its stream, but the QUIC acknowledgement for the peer's final bytes can
    /// still be in flight. Waiting for the peer to close keeps the connection alive long
    /// enough for `SendStream::stopped()` on the peer to observe delivery.
    ///
    /// # Errors
    ///
    /// Returns an error if the connection close notification cannot be observed.
    pub async fn wait_for_peer_close(&self) -> Result<()> {
        self.connection.closed().await;
        Ok(())
    }

    /// Gracefully close the endpoint after the receiver has drained both streams.
    ///
    /// Iroh's endpoint close waits for QUIC close notifications to be acknowledged,
    /// so the sender does not mistake a completed transfer for a lost connection.
    pub async fn close_transport(&self) -> Result<()> {
        timeout(ENDPOINT_CLOSE_TIMEOUT, self.endpoint.close())
            .await
            .context("timed out while closing Iroh endpoint")?;
        Ok(())
    }

    /// Read the peer's stream FIN after the last framed message so its
    /// `finish_send` can complete before either side drops the connection.
    ///
    /// # Errors
    ///
    /// Returns an error if the receive stream cannot be drained.
    pub async fn finish_recv(&mut self) -> Result<()> {
        self.recv
            .read_to_end(0)
            .await
            .context("waiting for Iroh receive stream to finish")?;
        Ok(())
    }

    /// Receives one length-prefixed message from the QUIC stream.
    ///
    /// # Errors
    ///
    /// Returns an error if the length prefix or message cannot be read, or if the
    /// peer advertises a message larger than the configured limit.
    pub async fn recv_vec(&mut self) -> Result<Vec<u8>> {
        let mut length_bytes = [0u8; 4];
        self.recv.read_exact(&mut length_bytes).await?;
        let length = usize::try_from(u32::from_be_bytes(length_bytes))
            .context("message length does not fit in usize")?;
        ensure!(
            length <= MAX_MESSAGE_BYTES,
            "peer message is too large: {length} bytes (maximum {MAX_MESSAGE_BYTES})"
        );

        let mut data = vec![0u8; length];
        self.recv.read_exact(&mut data).await?;
        if let Some(profile) = self.connection_profile.as_mut() {
            profile.sample_if_due(&self.connection);
        }
        Ok(data)
    }
}

fn path_kind(is_ip: bool, is_relay: bool) -> PathKind {
    if is_ip {
        PathKind::Direct
    } else if is_relay {
        PathKind::Relay
    } else {
        PathKind::Unknown
    }
}

fn snapshot_paths(connection: &Connection) -> Vec<PathSample> {
    connection
        .paths()
        .iter()
        .map(|path| {
            let stats = path.stats();
            PathSample {
                id: path.id().to_string(),
                kind: path_kind(path.is_ip(), path.is_relay()),
                selected: path.is_selected(),
                stream_tx: stats.frame_tx.stream,
                stream_rx: stats.frame_rx.stream,
            }
        })
        .collect()
}

fn classify_path_evidence(
    baseline: &[PathSample],
    ending: &[PathSample],
    events: &PathEventSummary,
) -> PathEvidence {
    let mut direct_stream_tx = 0_u64;
    let mut direct_stream_rx = 0_u64;
    let mut relay_stream_tx = 0_u64;
    let mut relay_stream_rx = 0_u64;
    let mut missing_path_stats = false;
    let mut observed_ids = std::collections::HashSet::new();
    let baseline_for = |id: &str| baseline.iter().find(|sample| sample.id == id);
    let mut apply_delta = |sample: &PathSample| {
        observed_ids.insert(sample.id.clone());
        let before = baseline_for(&sample.id);
        let (base_tx, base_rx) = before.map_or((0, 0), |value| (value.stream_tx, value.stream_rx));
        let tx = sample.stream_tx.saturating_sub(base_tx);
        let rx = sample.stream_rx.saturating_sub(base_rx);
        match sample.kind {
            PathKind::Direct => {
                direct_stream_tx = direct_stream_tx.saturating_add(tx);
                direct_stream_rx = direct_stream_rx.saturating_add(rx);
            }
            PathKind::Relay => {
                relay_stream_tx = relay_stream_tx.saturating_add(tx);
                relay_stream_rx = relay_stream_rx.saturating_add(rx);
            }
            PathKind::Unknown if tx > 0 || rx > 0 => missing_path_stats = true,
            PathKind::Unknown => {}
        }
    };

    let ending_ids: std::collections::HashSet<_> = ending.iter().map(|s| s.id.as_str()).collect();
    for sample in ending.iter().chain(
        events
            .closed
            .iter()
            .filter(|s| !ending_ids.contains(s.id.as_str())),
    ) {
        apply_delta(sample);
    }
    for sample in baseline {
        if !observed_ids.contains(&sample.id) {
            missing_path_stats = true;
        }
    }
    let closed_ids: std::collections::HashSet<_> =
        events.closed.iter().map(|s| s.id.as_str()).collect();
    if events
        .opened
        .iter()
        .any(|(id, _)| !ending_ids.contains(id.as_str()) && !closed_ids.contains(id.as_str()))
    {
        missing_path_stats = true;
    }

    let direct = direct_stream_tx > 0 || direct_stream_rx > 0;
    let relay = relay_stream_tx > 0 || relay_stream_rx > 0;
    let relay_selected = baseline
        .iter()
        .any(|sample| sample.selected && sample.kind == PathKind::Relay)
        || events
            .selected
            .iter()
            .any(|(_, kind)| *kind == PathKind::Relay);
    let verified = !events.lagged && !missing_path_stats && (direct || relay);
    let classification = if !verified {
        "unverified"
    } else if (direct && relay) || (direct && relay_selected) {
        "mixed"
    } else if relay {
        "relay"
    } else {
        "direct"
    };
    PathEvidence {
        classification,
        verified,
        direct_stream_tx,
        direct_stream_rx,
        relay_stream_tx,
        relay_stream_rx,
        lagged: events.lagged,
        missing_path_stats,
        relay_selected,
        connection_stats: None,
    }
}

/// Creates an Iroh endpoint for the transfer protocol.
///
/// # Errors
///
/// Returns an error if the endpoint cannot be bound.
pub async fn bind_endpoint() -> Result<Endpoint> {
    let relay_only =
        std::env::var("RUSTYTRANSFER_BENCH_RELAY_ONLY").is_ok_and(|value| value == "1");
    bind_endpoint_with_key(SecretKey::generate(), relay_only, IROH_ALPN).await
}

/// Binds an endpoint with a caller-provided identity. Used by the direct-Iroh probe.
///
/// # Errors
///
/// Returns an error if the endpoint cannot be bound.
pub async fn bind_endpoint_with_key(
    key: SecretKey,
    relay_only: bool,
    alpn: &[u8],
) -> Result<Endpoint> {
    let mut builder = Endpoint::builder(presets::N0)
        .secret_key(key)
        .alpns(vec![alpn.to_vec()]);
    if let Some(stream_window_bytes) = benchmark_stream_window_from_env()? {
        let transport_config = QuicTransportConfig::builder()
            .stream_receive_window(VarInt::from_u32(stream_window_bytes))
            .receive_window(VarInt::from_u32(CONNECTION_RECEIVE_WINDOW_BYTES))
            .build();
        eprintln!(
            "Applied benchmark Iroh receive windows: stream={stream_window_bytes} bytes, connection={CONNECTION_RECEIVE_WINDOW_BYTES} bytes"
        );
        builder = builder.transport_config(transport_config);
    }
    if relay_only {
        builder = builder.clear_ip_transports();
    }
    Ok(builder.bind().await?)
}

fn parse_benchmark_stream_window(value: &str) -> Result<u32> {
    let bytes = value
        .parse::<u32>()
        .context("benchmark stream window must be an integer byte count")?;
    ensure!(
        (MIN_STREAM_WINDOW_BYTES..=MAX_STREAM_WINDOW_BYTES).contains(&bytes),
        "benchmark stream window must be between {MIN_STREAM_WINDOW_BYTES} and {MAX_STREAM_WINDOW_BYTES} bytes"
    );
    Ok(bytes)
}

fn benchmark_stream_window_from_env() -> Result<Option<u32>> {
    match std::env::var(STREAM_WINDOW_ENV) {
        Ok(value) => parse_benchmark_stream_window(&value).map(Some),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(error) => Err(error).context("cannot read benchmark stream window"),
    }
}

/// Returns the endpoint address after allowing Iroh to initialize its transports.
///
/// # Errors
///
/// Returns an error if the endpoint has no usable address.
pub async fn endpoint_addr(endpoint: &Endpoint) -> Result<EndpointAddr> {
    // Wait briefly for relay/address setup. Direct addressing may still work
    // if the relay is unavailable.
    match timeout(ENDPOINT_READY_TIMEOUT, endpoint.online()).await {
        Ok(()) => {}
        Err(error) => {
            return Err(anyhow!(TransportError::IrohEndpointNotOnline(format!(
                "timed out after {}s: {error}",
                ENDPOINT_READY_TIMEOUT.as_secs()
            ))));
        }
    }
    let addr = endpoint.addr();
    ensure!(
        !addr.addrs.is_empty(),
        "Iroh endpoint has no usable address"
    );
    Ok(addr)
}

/// Waits for the Iroh offer frame in the rendezvous room.
///
/// # Errors
///
/// Returns an error if the signaling stream closes or contains invalid data.
pub async fn wait_for_offer(read: &mut WsRead) -> Result<EndpointAddr> {
    loop {
        if let Frame::IrohOffer { addr } = WsRoomTransport::recv_frame(read).await? {
            return Ok(addr);
        }
    }
}

/// Side A: advertise an Iroh endpoint and accept one bidirectional stream.
///
/// # Errors
///
/// Returns an error if signaling, endpoint setup, or the incoming connection fails.
pub async fn connect_offerer(app_id: &str) -> Result<IrohState> {
    connect_offerer_with_ready(app_id, || {}).await
}

/// Connect the signaling socket, then report backend readiness before waiting
/// for the receiver. The caller can display a share code only after this point.
pub async fn connect_offerer_with_ready<F: FnOnce()>(
    app_id: &str,
    on_ready: F,
) -> Result<IrohState> {
    let ws = WsRoomTransport::new(app_id.to_string(), "A".to_string());
    let (mut write, mut read) = ws.connect_room().await?;

    let endpoint = bind_endpoint().await?;
    let addr = endpoint_addr(&endpoint).await?;
    on_ready();
    timeout(Duration::from_mins(1), wait_for_room_full(&mut read)).await??;

    WsRoomTransport::send_frame(&mut write, &Frame::IrohOffer { addr }).await?;
    let (connection, send, recv) = accept_connection(&endpoint).await?;
    Ok(state(endpoint, connection, send, recv))
}

/// Side B: receive an Iroh endpoint address and open one bidirectional stream.
///
/// # Errors
///
/// Returns an error if signaling, endpoint setup, or the outgoing connection fails.
pub async fn connect_answerer(app_id: &str) -> Result<IrohState> {
    let ws = WsRoomTransport::new(app_id.to_string(), "B".to_string());
    let (_write, mut read) = ws.connect_room().await?;

    let addr = wait_for_offer(&mut read).await?;
    let endpoint = bind_endpoint().await?;
    let (connection, send, recv) = connect_connection(&endpoint, addr).await?;
    Ok(state(endpoint, connection, send, recv))
}

/// Start a publicly discoverable endpoint with a stable sender identity.
pub async fn bind_direct_sender(identity_file: &Path) -> Result<Endpoint> {
    let key = load_or_create_key(identity_file)?;
    let relay_only =
        std::env::var("RUSTYTRANSFER_BENCH_RELAY_ONLY").is_ok_and(|value| value == "1");
    let endpoint = bind_endpoint_with_key(key, relay_only, DIRECT_ALPN).await?;
    endpoint_addr(&endpoint).await?;
    Ok(endpoint)
}

/// Wait for a peer that presents the current transfer's secret token.
pub async fn accept_direct(endpoint: Endpoint, token: &[u8; 16]) -> Result<IrohState> {
    timeout(CONNECT_TIMEOUT, async {
        loop {
            let incoming = endpoint
                .accept()
                .await
                .ok_or_else(|| anyhow!("direct Iroh endpoint closed"))?;
            let Ok(Ok(connection)) = timeout(Duration::from_secs(10), incoming).await else {
                continue;
            };
            let Ok(Ok((send, recv))) =
                timeout(Duration::from_secs(10), connection.accept_bi()).await
            else {
                connection.close(0u32.into(), b"stream not opened");
                continue;
            };
            let mut candidate = state(endpoint.clone(), connection, send, recv);
            let presented = timeout(Duration::from_secs(5), candidate.recv_vec()).await;
            if let Ok(Ok(bytes)) = presented {
                let difference = bytes
                    .iter()
                    .zip(token)
                    .fold(0u8, |value, (actual, expected)| value | (actual ^ expected));
                if bytes.len() == token.len() && difference == 0 {
                    return Ok(candidate);
                }
            }
            candidate.connection.close(0u32.into(), b"invalid invite");
        }
    })
    .await
    .context("waiting for an authorized direct Iroh receiver timed out")?
}

/// Dial a sender by its authenticated ID and present this transfer's token.
pub async fn connect_direct(peer_id: &str, token: &[u8; 16]) -> Result<IrohState> {
    let peer_id: EndpointId = peer_id.parse().context("invalid sender EndpointId")?;
    let relay_only =
        std::env::var("RUSTYTRANSFER_BENCH_RELAY_ONLY").is_ok_and(|value| value == "1");
    let endpoint = bind_endpoint_with_key(SecretKey::generate(), relay_only, DIRECT_ALPN).await?;
    let connection = timeout(CONNECT_TIMEOUT, endpoint.connect(peer_id, DIRECT_ALPN))
        .await
        .context("direct Iroh connection timed out")??;
    if connection.remote_id() != peer_id {
        return Err(TransportError::PeerIdentityMismatch.into());
    }
    let (send, recv) = timeout(CONNECT_TIMEOUT, connection.open_bi())
        .await
        .context("opening direct Iroh stream timed out")??;
    let mut state = state(endpoint, connection, send, recv);
    state.send_vec(token.to_vec()).await?;
    Ok(state)
}

/// Accepts one incoming Iroh connection and opens its bidirectional stream.
///
/// # Errors
///
/// Returns an error if accepting the connection or opening its stream times out or fails.
pub async fn accept_connection(
    endpoint: &Endpoint,
) -> Result<(Connection, SendStream, RecvStream)> {
    let incoming = timeout(CONNECT_TIMEOUT, endpoint.accept())
        .await
        .context("waiting for Iroh connection timed out")?
        .ok_or_else(|| anyhow!("Iroh endpoint closed before accepting a connection"))?;
    let connection = timeout(CONNECT_TIMEOUT, incoming)
        .await
        .context("Iroh connection handshake timed out")??;
    let (send, recv) = timeout(CONNECT_TIMEOUT, connection.accept_bi())
        .await
        .context("waiting for Iroh bidirectional stream timed out")??;
    Ok((connection, send, recv))
}

/// Connects to an Iroh endpoint and opens a bidirectional stream.
///
/// # Errors
///
/// Returns an error if the connection or stream handshake times out or fails.
pub async fn connect_connection(
    endpoint: &Endpoint,
    addr: EndpointAddr,
) -> Result<(Connection, SendStream, RecvStream)> {
    let connection = timeout(CONNECT_TIMEOUT, endpoint.connect(addr, IROH_ALPN))
        .await
        .context("Iroh connection timed out")??;
    let (send, recv) = timeout(CONNECT_TIMEOUT, connection.open_bi())
        .await
        .context("opening Iroh bidirectional stream timed out")??;
    Ok((connection, send, recv))
}

/// Builds the transport state from an endpoint and its bidirectional stream.
#[must_use]
pub fn state(
    endpoint: Endpoint,
    connection: Connection,
    send: SendStream,
    recv: RecvStream,
) -> IrohState {
    IrohState::new(endpoint, connection, send, recv)
}

#[cfg(test)]
mod path_evidence_tests {
    use super::{
        ConnectionSample, IrohConnectionProfile, PathEventSummary, PathKind, PathSample,
        classify_path_evidence, parse_benchmark_stream_window,
    };
    use std::time::{Duration, Instant};

    fn sample(id: &str, kind: PathKind, tx: u64, rx: u64) -> PathSample {
        PathSample {
            id: id.to_owned(),
            kind,
            selected: false,
            stream_tx: tx,
            stream_rx: rx,
        }
    }

    fn events() -> PathEventSummary {
        PathEventSummary {
            closed: vec![],
            opened: vec![],
            selected: vec![],
            lagged: false,
        }
    }

    #[test]
    fn stream_deltas_verify_direct_payload_route() {
        let baseline = [sample("ip", PathKind::Direct, 100, 40)];
        let ending = [sample("ip", PathKind::Direct, 120, 70)];
        let evidence = classify_path_evidence(&baseline, &ending, &events());
        assert_eq!(evidence.classification, "direct");
        assert!(evidence.verified);
        assert_eq!(evidence.direct_stream_tx, 20);
        assert_eq!(evidence.direct_stream_rx, 30);
    }

    #[test]
    fn closed_path_final_stats_are_counted_but_post_end_snapshot_wins() {
        let baseline = [
            sample("ip", PathKind::Direct, 5, 7),
            sample("relay", PathKind::Relay, 2, 3),
        ];
        let ending = [sample("ip", PathKind::Direct, 15, 17)];
        let mut events = events();
        events.closed.push(sample("ip", PathKind::Direct, 900, 900));
        events.closed.push(sample("relay", PathKind::Relay, 12, 13));
        let evidence = classify_path_evidence(&baseline, &ending, &events);
        assert_eq!(evidence.direct_stream_tx, 10);
        assert_eq!(evidence.direct_stream_rx, 10);
        assert_eq!(evidence.relay_stream_tx, 10);
        assert_eq!(evidence.relay_stream_rx, 10);
        assert_eq!(evidence.classification, "mixed");
        assert!(evidence.verified);
    }

    #[test]
    fn lagged_or_missing_events_are_unverified_and_relay_selection_is_conservative() {
        let baseline = [sample("ip", PathKind::Direct, 0, 0)];
        let ending = [sample("ip", PathKind::Direct, 10, 0)];
        let mut event_summary = events();
        event_summary
            .selected
            .push(("relay".to_owned(), PathKind::Relay));
        let evidence = classify_path_evidence(&baseline, &ending, &event_summary);
        assert_eq!(evidence.classification, "mixed");
        assert!(evidence.relay_selected);

        event_summary.lagged = true;
        let evidence = classify_path_evidence(&baseline, &ending, &event_summary);
        assert_eq!(evidence.classification, "unverified");
        assert!(!evidence.verified);

        let mut baseline = [sample("relay", PathKind::Relay, 0, 0)];
        baseline[0].selected = true;
        let ending = [
            sample("relay", PathKind::Relay, 0, 0),
            sample("ip", PathKind::Direct, 10, 0),
        ];
        let evidence = classify_path_evidence(&baseline, &ending, &events());
        assert_eq!(evidence.classification, "mixed");
        assert!(evidence.relay_selected);

        let baseline = [sample("ip", PathKind::Direct, 0, 0)];
        let evidence = classify_path_evidence(
            &baseline,
            &[],
            &PathEventSummary {
                lagged: false,
                ..events()
            },
        );
        assert!(evidence.missing_path_stats);
        assert_eq!(evidence.classification, "unverified");
    }

    #[test]
    fn connection_profile_reports_counter_deltas_and_send_stall_buckets() {
        let started = ConnectionSample {
            path_id: Some("path-1".to_owned()),
            rtt_us: Some(20_000),
            congestion_window_bytes: Some(120_000),
            congestion_events: Some(2),
            lost_packets: 5,
            lost_bytes: 6_000,
            udp_tx_datagrams: 10,
            udp_tx_bytes: 15_000,
            udp_rx_datagrams: 9,
            udp_rx_bytes: 14_000,
            data_blocked_frames: 3,
            stream_data_blocked_frames: 4,
            data_blocked_frames_rx: 0,
            stream_data_blocked_frames_rx: 0,
        };
        let mut profile = IrohConnectionProfile {
            started: started.clone(),
            ended: None,
            last_sample: Instant::now(),
            samples: 0,
            rtt_min_us: None,
            rtt_max_us: None,
            congestion_window_min_bytes: None,
            congestion_window_max_bytes: None,
            send_wait_calls: 0,
            send_stalls_over_1ms: 0,
            send_stalls_over_10ms: 0,
            send_wait_max_us: 0,
        };
        profile.record_sample(started);
        profile.record_send_wait(Duration::from_micros(1_001));
        profile.record_send_wait(Duration::from_micros(10_001));
        profile.finish_with_sample(ConnectionSample {
            path_id: Some("path-1".to_owned()),
            rtt_us: Some(30_000),
            congestion_window_bytes: Some(90_000),
            congestion_events: Some(3),
            lost_packets: 8,
            lost_bytes: 9_000,
            udp_tx_datagrams: 20,
            udp_tx_bytes: 30_000,
            udp_rx_datagrams: 18,
            udp_rx_bytes: 28_000,
            data_blocked_frames: 5,
            stream_data_blocked_frames: 9,
            data_blocked_frames_rx: 1,
            stream_data_blocked_frames_rx: 2,
        });
        profile.record_send_wait(Duration::from_secs(1));

        let evidence = profile.into_evidence();
        assert_eq!(evidence.lost_packets_delta, 3);
        assert_eq!(evidence.lost_bytes_delta, 3_000);
        assert_eq!(evidence.congestion_events_delta, Some(1));
        assert_eq!(evidence.data_blocked_frames_delta, 2);
        assert_eq!(evidence.stream_data_blocked_frames_delta, 5);
        assert_eq!(evidence.data_blocked_frames_rx_delta, 1);
        assert_eq!(evidence.stream_data_blocked_frames_rx_delta, 2);
        assert_eq!(evidence.udp_tx_datagrams_delta, 10);
        assert_eq!(evidence.udp_rx_bytes_delta, 14_000);
        assert_eq!(evidence.rtt_min_us, Some(20_000));
        assert_eq!(evidence.congestion_window_min_bytes, Some(90_000));
        assert_eq!(evidence.send_wait_calls, 2);
        assert_eq!(evidence.send_stalls_over_1ms, 2);
        assert_eq!(evidence.send_stalls_over_10ms, 1);
        assert_eq!(evidence.send_wait_max_us, 10_001);
    }

    #[test]
    fn benchmark_stream_window_parser_enforces_the_experiment_bounds() {
        assert_eq!(
            parse_benchmark_stream_window("1250000").ok(),
            Some(1_250_000)
        );
        assert_eq!(
            parse_benchmark_stream_window("2500000").ok(),
            Some(2_500_000)
        );
        assert_eq!(
            parse_benchmark_stream_window("5000000").ok(),
            Some(5_000_000)
        );
        for invalid in ["", "nope", "0", "1249999", "5000001", "4294967296"] {
            assert!(
                parse_benchmark_stream_window(invalid).is_err(),
                "accepted invalid window {invalid:?}"
            );
        }
    }
}
