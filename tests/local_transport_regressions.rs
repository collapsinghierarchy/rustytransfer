use anyhow::{Context, Result, anyhow, ensure};
use iroh::{Endpoint, EndpointAddr, RelayMode, SecretKey, endpoint::presets};
use rand::{RngCore, SeedableRng, rngs::StdRng};
use rustytransfer::{
    transfer::{self, TransferConfig},
    transport::{
        DataTransport, PathObservation,
        frames::Frame,
        iroh::{IrohState, accept_connection, connect_connection, state},
        webrtc::{TRANSFER_DATA_CHANNEL_ORDERED, WebRtcState},
    },
};
use serde::Serialize;
use std::{
    fs::{self, File, OpenOptions},
    io::{BufWriter, Write},
    path::{Path, PathBuf},
    process::Command,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::mpsc;
use tokio::time::{sleep, timeout};
use webrtc::data_channel::{DataChannel, RTCDataChannelInit};
use webrtc::peer_connection::{
    PeerConnection, PeerConnectionBuilder, PeerConnectionEventHandler, RTCConfigurationBuilder,
    RTCIceCandidateInit, RTCIceGatheringState, RTCPeerConnectionIceEvent, RTCSessionDescription,
};
use webrtc::runtime::default_runtime;

const TEST_TIMEOUT: Duration = Duration::from_secs(30);
const FINAL_STREAM_TIMEOUT: Duration = Duration::from_secs(10);
const LOCAL_BENCH_RUN_TIMEOUT: Duration = Duration::from_secs(900);
const BASELINE_CHUNK_SIZE: u32 = 8 * 1024;
const IROH_BASELINE_CHUNK_SIZE: u32 = 256 * 1024;
const PHASE2_CHUNK_SIZES: [u32; 5] = [8 * 1024, 32 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024];
const LOCAL_WEBRTC_SEND_BUFFER_LIMIT: usize = 1024 * 1024;

async fn local_iroh_pair() -> Result<(IrohState, IrohState)> {
    let (relay_map, relay_url, _relay_server) = iroh::test_utils::run_relay_server().await?;
    let endpoint_a = Endpoint::builder(presets::Minimal)
        .secret_key(SecretKey::generate())
        .relay_mode(RelayMode::Custom(relay_map.clone()))
        .ca_tls_config(iroh::tls::CaTlsConfig::insecure_skip_verify())
        .alpns(vec![b"rustytransfer/2".to_vec()])
        .bind()
        .await?;
    let endpoint_b = Endpoint::builder(presets::Minimal)
        .secret_key(SecretKey::generate())
        .relay_mode(RelayMode::Custom(relay_map))
        .ca_tls_config(iroh::tls::CaTlsConfig::insecure_skip_verify())
        .alpns(vec![b"rustytransfer/2".to_vec()])
        .bind()
        .await?;

    timeout(TEST_TIMEOUT, endpoint_a.online())
        .await
        .context("local Iroh endpoint A did not reach its relay")?;
    let remote_addr = EndpointAddr::new(endpoint_a.id()).with_relay_url(relay_url);

    let accept_endpoint = endpoint_a.clone();
    let accept_task = tokio::spawn(async move { accept_connection(&accept_endpoint).await });
    let (connection_b, send_b, recv_b) = timeout(
        Duration::from_secs(15),
        connect_connection(&endpoint_b, remote_addr),
    )
    .await
    .context("local Iroh dial timed out")??;
    let mut state_b = state(endpoint_b, connection_b, send_b, recv_b);

    // Iroh doesn't expose an opened bidirectional stream to the accept side
    // until the client sends its first bytes, so use an empty framed message
    // to complete local test setup before awaiting accept_bi.
    state_b.send_vec(Vec::new()).await?;
    let accepted = timeout(TEST_TIMEOUT, accept_task)
        .await
        .context("local Iroh accept timed out")??;
    let (connection_a, send_a, recv_a) = accepted?;
    let mut state_a = state(endpoint_a, connection_a, send_a, recv_a);
    ensure!(
        state_a.recv_vec().await?.is_empty(),
        "unexpected setup frame"
    );

    Ok((state_a, state_b))
}

struct LocalPeerEvents {
    candidates: Mutex<Vec<RTCIceCandidateInit>>,
    gathering_complete_tx: mpsc::UnboundedSender<()>,
    data_channel_tx: mpsc::UnboundedSender<Arc<dyn DataChannel>>,
}

#[async_trait::async_trait]
impl PeerConnectionEventHandler for LocalPeerEvents {
    async fn on_ice_candidate(&self, event: RTCPeerConnectionIceEvent) {
        if let Ok(candidate) = event.candidate.to_json()
            && !candidate.candidate.is_empty()
            && let Ok(mut candidates) = self.candidates.lock()
        {
            candidates.push(candidate);
        }
    }

    async fn on_ice_gathering_state_change(&self, state: RTCIceGatheringState) {
        if state == RTCIceGatheringState::Complete {
            let _send_result = self.gathering_complete_tx.send(());
        }
    }

    async fn on_data_channel(&self, channel: Arc<dyn DataChannel>) {
        let _send_result = self.data_channel_tx.send(channel);
    }
}

async fn local_peer() -> Result<(
    Arc<dyn PeerConnection>,
    Arc<LocalPeerEvents>,
    mpsc::UnboundedReceiver<()>,
    mpsc::UnboundedReceiver<Arc<dyn DataChannel>>,
)> {
    let (gathering_complete_tx, gathering_complete_rx) = mpsc::unbounded_channel();
    let (data_channel_tx, data_channel_rx) = mpsc::unbounded_channel();
    let events = Arc::new(LocalPeerEvents {
        candidates: Mutex::new(Vec::new()),
        gathering_complete_tx,
        data_channel_tx,
    });
    let runtime = default_runtime().ok_or_else(|| anyhow::anyhow!("no WebRTC runtime"))?;
    let pc = PeerConnectionBuilder::new()
        .with_configuration(RTCConfigurationBuilder::new().build())
        .with_handler(events.clone())
        .with_runtime(runtime)
        .with_udp_addrs(vec!["127.0.0.1:0".to_owned()])
        .with_data_channel_send_buffer_limit(LOCAL_WEBRTC_SEND_BUFFER_LIMIT)
        .build()
        .await?;
    Ok((Arc::new(pc), events, gathering_complete_rx, data_channel_rx))
}

async fn local_candidates(
    events: &LocalPeerEvents,
    gathering_complete: &mut mpsc::UnboundedReceiver<()>,
) -> Result<Vec<RTCIceCandidateInit>> {
    timeout(TEST_TIMEOUT, gathering_complete.recv())
        .await
        .context("local WebRTC ICE gathering timed out")?
        .context("local WebRTC ICE gathering event stream ended")?;
    events
        .candidates
        .lock()
        .map(|candidates| candidates.clone())
        .map_err(|error| anyhow::anyhow!("local WebRTC candidate list lock was poisoned: {error}"))
}

async fn add_candidates(
    pc: &dyn PeerConnection,
    candidates: Vec<RTCIceCandidateInit>,
) -> Result<()> {
    for candidate in candidates {
        pc.add_ice_candidate(candidate).await?;
    }
    Ok(())
}

async fn local_webrtc_pair() -> Result<(WebRtcState, WebRtcState)> {
    let (pc_a, events_a, mut gathering_a, _) = local_peer().await?;
    let channel_a = pc_a
        .create_data_channel(
            "probe",
            Some(RTCDataChannelInit {
                ordered: TRANSFER_DATA_CHANNEL_ORDERED,
                ..Default::default()
            }),
        )
        .await?;
    let offer = pc_a.create_offer(None).await?;
    pc_a.set_local_description(offer.clone()).await?;
    let candidates_a = local_candidates(&events_a, &mut gathering_a).await?;

    let (pc_b, events_b, mut gathering_b, mut incoming_channels_b) = local_peer().await?;
    pc_b.set_remote_description(RTCSessionDescription::offer(offer.sdp)?)
        .await?;
    add_candidates(pc_b.as_ref(), candidates_a).await?;

    let answer = pc_b.create_answer(None).await?;
    pc_b.set_local_description(answer.clone()).await?;
    let candidates_b = local_candidates(&events_b, &mut gathering_b).await?;

    pc_a.set_remote_description(RTCSessionDescription::answer(answer.sdp)?)
        .await?;
    add_candidates(pc_a.as_ref(), candidates_b).await?;
    let channel_b = timeout(TEST_TIMEOUT, incoming_channels_b.recv())
        .await
        .context("local WebRTC answerer did not receive the data channel")?
        .context("local WebRTC answerer data channel event stream ended")?;

    let (state_a, state_b) = tokio::try_join!(
        WebRtcState::new(pc_a, channel_a),
        WebRtcState::new(pc_b, channel_b)
    )?;
    Ok((state_a, state_b))
}

async fn exercise_fin_ack_shutdown<T>(sender: T, receiver: T) -> Result<()>
where
    T: LocalMessageTransport,
{
    // Send several fragmented transfer messages without waiting for per-frame
    // acknowledgements; both transports must preserve their message order.
    let payloads: Vec<Vec<u8>> = (0_u8..4)
        .map(|marker| {
            let mut payload = vec![marker; 64 * 1024 + usize::from(marker)];
            payload
                .get_mut(..8)
                .ok_or_else(|| anyhow!("test payload is shorter than its marker prefix"))?
                .copy_from_slice(&[marker; 8]);
            Ok(payload)
        })
        .collect::<Result<_>>()?;
    let sender_payloads = payloads.clone();
    let receiver_payloads = payloads;

    let sender_side = async move {
        let mut sender = sender;
        for payload in &sender_payloads {
            sender.send(payload.clone()).await?;
        }

        ensure!(sender.recv().await? == b"FIN");
        sender.send(b"FIN_ACK".to_vec()).await?;
        sender.close_send()?;
        timeout(FINAL_STREAM_TIMEOUT, sender.finish_recv())
            .await
            .context("sender stream receive timed out")??;
        timeout(FINAL_STREAM_TIMEOUT, sender.wait_for_peer_close())
            .await
            .context("sender wait for peer close timed out")??;
        Ok::<_, anyhow::Error>(())
    };

    let receiver_side = async move {
        let mut receiver = receiver;
        for payload in &receiver_payloads {
            ensure!(
                receiver.recv().await? == *payload,
                "payload order/content mismatch"
            );
        }

        receiver.send(b"FIN".to_vec()).await?;
        ensure!(receiver.recv().await? == b"FIN_ACK");
        timeout(FINAL_STREAM_TIMEOUT, receiver.finish_recv())
            .await
            .context("receiver stream receive timed out")??;
        timeout(FINAL_STREAM_TIMEOUT, receiver.finish_send())
            .await
            .context("receiver stream finish timed out")??;
        receiver.close_transport().await?;
        Ok::<_, anyhow::Error>(())
    };

    let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
        tokio::join!(sender_side, receiver_side)
    })
    .await
    .context("local FIN/FIN_ACK shutdown timed out")?;
    sender_result?;
    receiver_result?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn local_iroh_fin_ack_closes_after_streams_are_drained() -> Result<()> {
    let (sender, receiver) = timeout(TEST_TIMEOUT, local_iroh_pair())
        .await
        .context("local Iroh setup timed out")??;
    let path = sender.path_observation();
    ensure!(
        path.path != "unknown",
        "Iroh selected path was not observable: {path:?}"
    );
    eprintln!("local baseline path: iroh {path:?}");
    timeout(TEST_TIMEOUT, exercise_fin_ack_shutdown(sender, receiver))
        .await
        .context("local Iroh FIN/FIN_ACK shutdown timed out")?
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn local_webrtc_fin_ack_closes_after_peer_close_signal() -> Result<()> {
    let (sender, receiver) = timeout(TEST_TIMEOUT, local_webrtc_pair())
        .await
        .context("local WebRTC setup timed out")??;
    let path = sender.path_observation().await?;
    ensure!(
        path.path != "unknown",
        "WebRTC selected ICE pair was not observable: {path:?}"
    );
    ensure!(
        path.path == "direct",
        "loopback WebRTC selected a non-direct path: {path:?}"
    );
    eprintln!("local baseline path: webrtc {path:?}");
    timeout(TEST_TIMEOUT, exercise_fin_ack_shutdown(sender, receiver))
        .await
        .context("local WebRTC FIN/FIN_ACK shutdown timed out")?
}

#[test]
fn encrypted_webrtc_transfer_channel_stays_ordered() {
    const {
        assert!(TRANSFER_DATA_CHANNEL_ORDERED);
    }
}

#[test]
fn iroh_offer_frame_roundtrips_with_the_signaling_wire_name() -> Result<()> {
    let addr = EndpointAddr::new(SecretKey::generate().public());
    let frame = Frame::IrohOffer { addr: addr.clone() };
    let encoded = serde_json::to_string(&frame)?;
    ensure!(encoded.contains("\"type\":\"iroh_offer\""));

    let decoded: Frame = serde_json::from_str(&encoded)?;
    match decoded {
        Frame::IrohOffer { addr: decoded_addr } => ensure!(decoded_addr == addr),
        other => anyhow::bail!("expected iroh_offer, received {other:?}"),
    }
    Ok(())
}

#[derive(Clone)]
struct EndpointTimings {
    role: &'static str,
    path_start: PathObservation,
    path_end: PathObservation,
    path_evidence: Option<rustytransfer::transport::PathEvidence>,
    direct_route_verified_both: bool,
    chunk_size: u32,
    bytes_transferred: u64,
    handshake_seconds: f64,
    payload_seconds: f64,
    shutdown_seconds: f64,
    payload_profile: Option<transfer::PayloadProfile>,
}

#[derive(Serialize)]
struct LocalBaselineRecord {
    schema_version: u32,
    commit: Option<String>,
    working_tree_dirty: Option<bool>,
    role: &'static str,
    transport: &'static str,
    path: &'static str,
    path_start: &'static str,
    path_end: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    path_evidence: Option<rustytransfer::transport::PathEvidence>,
    direct_route_verified_both: bool,
    local_candidate_type: Option<String>,
    remote_candidate_type: Option<String>,
    size_bytes: u64,
    chunk_size: u32,
    bytes_transferred: u64,
    pipeline_depth: u8,
    handshake_seconds: f64,
    payload_seconds: f64,
    shutdown_seconds: f64,
    wall_seconds: f64,
    effective_mib_per_second: f64,
    sender_cpu_seconds: Option<f64>,
    receiver_cpu_seconds: Option<f64>,
    sender_max_rss_kib: Option<u64>,
    receiver_max_rss_kib: Option<u64>,
    source_sha256: String,
    received_sha256: String,
    success: bool,
    profile_mode: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    payload_profile: Option<transfer::PayloadProfile>,
    run_index: usize,
    warmup: bool,
    measurement_scope: &'static str,
}

fn baseline_chunk_size(transport: &str) -> u32 {
    match transport {
        "iroh" => IROH_BASELINE_CHUNK_SIZE,
        _ => BASELINE_CHUNK_SIZE,
    }
}

fn prepare_local_benchmark_files(
    temp_dir: &Path,
    size_bytes: u64,
) -> Result<(Option<PathBuf>, PathBuf, PathBuf, String)> {
    let size_mib = size_bytes / (1024 * 1024);
    let file_name = format!("{size_mib}mib.bin");
    let persistent_source = std::env::var_os("RUSTYTRANSFER_BENCH_SOURCE").map(PathBuf::from);
    let source_path = persistent_source
        .clone()
        .unwrap_or_else(|| temp_dir.join(format!("source-{file_name}")));
    let received_path = temp_dir.join(format!("received-{file_name}"));
    if let Some(parent) = source_path.parent() {
        fs::create_dir_all(parent).context("failed to create benchmark source directory")?;
    }
    if source_path.exists() {
        ensure!(
            fs::metadata(&source_path)
                .context("failed to inspect configured benchmark source")?
                .len()
                == size_bytes,
            "configured source must be exactly {size_mib} MiB"
        );
    } else {
        write_incompressible_file(&source_path, size_bytes)?;
    }
    let source_sha256 = sha256_file(&source_path)?;
    Ok((persistent_source, source_path, received_path, source_sha256))
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "manual local full-file performance baseline"]
async fn local_full_file_performance_baseline() -> Result<()> {
    if std::env::var_os("RUSTYTRANSFER_PERFORMANCE_TRIAL_DIR").is_some() {
        return controlled_iroh_performance_trial().await;
    }
    let transports = local_baseline_transports_from(
        std::env::var("RUSTYTRANSFER_LOCAL_BENCH_TRANSPORT")
            .ok()
            .as_deref(),
    )?;
    let size_mib = std::env::var("RUSTYTRANSFER_BENCH_SIZE_MIB")
        .unwrap_or_else(|_| "512".to_owned())
        .parse::<u64>()
        .context("RUSTYTRANSFER_BENCH_SIZE_MIB must be an integer")?;
    ensure!(
        size_mib == 64 || size_mib == 512,
        "supported local baseline sizes are 64 or 512 MiB"
    );
    let metrics_path = std::env::var_os("RUSTYTRANSFER_LOCAL_BENCH_JSONL")
        .map(PathBuf::from)
        .ok_or_else(|| anyhow!("set RUSTYTRANSFER_LOCAL_BENCH_JSONL to an output JSONL path"))?;
    if let Some(parent) = metrics_path.parent() {
        fs::create_dir_all(parent).context("failed to create baseline output directory")?;
    }

    let temp_dir = std::env::temp_dir().join(format!(
        "rustytransfer-local-benchmark-{}",
        std::process::id()
    ));
    fs::create_dir_all(&temp_dir).context("failed to create local baseline temp directory")?;
    let size_bytes = size_mib * 1024 * 1024;
    let (persistent_source, source_path, received_path, source_sha256) =
        prepare_local_benchmark_files(&temp_dir, size_bytes)?;

    // One unscored warm-up per selected transport, then five runs in alternating order.
    for transport in &transports {
        eprintln!("local warmup start transport={transport} size_mib={size_mib}");
        let (_sender_timing, _receiver_timing) = run_local_full_transfer(
            *transport,
            &source_path,
            &received_path,
            size_bytes,
            baseline_chunk_size(transport),
        )
        .await
        .with_context(|| format!("{transport} warm-up failed"))?;
        verify_local_copy(&source_path, &received_path, &source_sha256)?;
        eprintln!("local warmup complete transport={transport} size_mib={size_mib}");
    }

    let run_order = [
        "webrtc", "iroh", "iroh", "webrtc", "webrtc", "iroh", "iroh", "webrtc", "webrtc", "iroh",
    ];
    let mut per_transport_run = [0_usize; 2];
    for transport in run_order
        .into_iter()
        .filter(|transport| transports.contains(transport))
    {
        let transport_index = usize::from(transport == "iroh");
        let run_count = per_transport_run
            .get_mut(transport_index)
            .ok_or_else(|| anyhow!("invalid transport index {transport_index}"))?;
        *run_count += 1;
        let trial = *run_count;
        let (sender, receiver) = run_local_full_transfer(
            transport,
            &source_path,
            &received_path,
            size_bytes,
            baseline_chunk_size(transport),
        )
        .await?;
        let received_sha256 = sha256_file(&received_path)?;
        ensure!(
            received_sha256 == source_sha256,
            "{transport} trial {trial}: received SHA-256 does not match source"
        );
        let received_len = fs::metadata(&received_path)
            .context("failed to read received file metadata")?
            .len();
        ensure!(
            received_len == size_bytes,
            "received file size differs from source"
        );
        append_local_metric(
            &metrics_path,
            &sender,
            transport,
            size_bytes,
            trial,
            false,
            &source_sha256,
            &received_sha256,
        )?;
        append_local_metric(
            &metrics_path,
            &receiver,
            transport,
            size_bytes,
            trial,
            false,
            &source_sha256,
            &received_sha256,
        )?;
        let expected_path =
            std::env::var("RUSTYTRANSFER_BENCH_EXPECTED_PATH").unwrap_or_else(|_| "direct".into());
        if transport == "iroh" && expected_path == "direct" && !sender.direct_route_verified_both {
            anyhow::bail!(
                "Iroh diagnostic row was retained, but direct STREAM-frame evidence was not verified by both endpoints"
            );
        }
        println!(
            "local transport={transport} size_mib={size_mib} chunk_size={} trial={trial} sender_payload_s={:.3} receiver_payload_s={:.3} path={} sha256={received_sha256}",
            sender.chunk_size,
            sender.payload_seconds,
            receiver.payload_seconds,
            route_at_both_ends(&sender.path_start, &sender.path_end),
        );
    }

    if persistent_source.is_none() {
        fs::remove_file(&source_path).context("failed to remove local baseline input")?;
    }
    fs::remove_file(&received_path).context("failed to remove local baseline output")?;
    fs::remove_dir(&temp_dir).context("failed to remove local baseline temp directory")?;
    Ok(())
}

// performance harness v1: identical committed harness required on both builds.
async fn controlled_iroh_performance_trial() -> Result<()> {
    let trial_dir = PathBuf::from(
        std::env::var_os("RUSTYTRANSFER_PERFORMANCE_TRIAL_DIR")
            .context("missing trial directory")?,
    );
    ensure!(
        trial_dir.is_dir(),
        "runner must create a fresh trial directory"
    );
    let source = PathBuf::from(
        std::env::var_os("RUSTYTRANSFER_BENCH_SOURCE").context("missing prestaged source")?,
    );
    let size = fs::metadata(&source)?.len();
    ensure!(
        size == 64 * 1024 * 1024 || size == 512 * 1024 * 1024,
        "controlled trials require 64 or 512 MiB"
    );
    let received = trial_dir.join("received.bin");
    let rows = trial_dir.join("endpoints.jsonl");
    ensure!(
        !received.exists() && !rows.exists(),
        "trial outputs must be fresh"
    );
    let source_hash = sha256_file(&source)?;
    // Full external hashing and JSON emission are outside this single pair timer.
    let started = Instant::now();
    let (sender, receiver) =
        run_local_full_transfer("iroh", &source, &received, size, IROH_BASELINE_CHUNK_SIZE).await?;
    let elapsed = started.elapsed().as_secs_f64();
    let received_hash = sha256_file(&received)?;
    for timing in [&sender, &receiver] {
        append_local_metric(
            &rows,
            timing,
            "iroh",
            size,
            1,
            false,
            &source_hash,
            &received_hash,
        )?;
    }
    let pair = serde_json::json!({"harness_version": 1, "elapsed_seconds": elapsed,
        "received_size_bytes": fs::metadata(&received)?.len(),
        "source_sha256": source_hash, "received_sha256": received_hash});
    fs::write(
        trial_dir.join("pair.json"),
        serde_json::to_vec_pretty(&pair)?,
    )?;
    ensure!(
        received_hash == source_hash && fs::metadata(&received)?.len() == size,
        "received file verification failed"
    );
    ensure!(
        sender.direct_route_verified_both && receiver.direct_route_verified_both,
        "direct STREAM evidence must be verified at both endpoints"
    );
    Ok(())
}

fn local_baseline_transports_from(filter: Option<&str>) -> Result<Vec<&'static str>> {
    match filter {
        None | Some("all") => Ok(vec!["webrtc", "iroh"]),
        Some("webrtc") => Ok(vec!["webrtc"]),
        Some("iroh") => Ok(vec!["iroh"]),
        Some(other) => anyhow::bail!(
            "unsupported local baseline transport filter: {other}; choose iroh, webrtc, or all"
        ),
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "manual Phase 2 512 MiB block-size sweep"]
async fn local_chunk_size_performance_sweep() -> Result<()> {
    let size_mib = std::env::var("RUSTYTRANSFER_CHUNK_SWEEP_SIZE_MIB")
        .unwrap_or_else(|_| "512".to_owned())
        .parse::<u64>()
        .context("RUSTYTRANSFER_CHUNK_SWEEP_SIZE_MIB must be an integer")?;
    ensure!(
        size_mib == 64 || size_mib == 512,
        "chunk sweeps support 64 or 512 MiB inputs"
    );
    let size_bytes = size_mib * 1024 * 1024;
    let transports: Vec<&'static str> = match std::env::var("RUSTYTRANSFER_CHUNK_SWEEP_TRANSPORT")
        .ok()
        .as_deref()
    {
        None => vec!["webrtc", "iroh"],
        Some("webrtc") => vec!["webrtc"],
        Some("iroh") => vec!["iroh"],
        Some(other) => anyhow::bail!("unsupported chunk-sweep transport: {other}"),
    };
    let metrics_path = std::env::var_os("RUSTYTRANSFER_LOCAL_BENCH_JSONL")
        .map(PathBuf::from)
        .ok_or_else(|| anyhow!("set RUSTYTRANSFER_LOCAL_BENCH_JSONL to an output JSONL path"))?;
    ensure!(
        !metrics_path.exists(),
        "refusing to append a new chunk-size sweep to an existing file: {}",
        metrics_path.display()
    );
    if let Some(parent) = metrics_path.parent() {
        fs::create_dir_all(parent).context("failed to create sweep output directory")?;
    }

    let temp_dir = std::env::temp_dir().join(format!(
        "rustytransfer-chunk-sweep-{size_mib}-{}",
        std::process::id()
    ));
    fs::create_dir_all(&temp_dir).context("failed to create chunk-sweep temp directory")?;
    let (persistent_source, source_path, received_path, source_sha256) =
        prepare_local_benchmark_files(&temp_dir, size_bytes)?;

    for chunk_size in PHASE2_CHUNK_SIZES {
        for transport in &transports {
            let transport = *transport;
            eprintln!("chunk sweep warmup transport={transport} chunk_size={chunk_size} bytes");
            let _ = run_local_full_transfer(
                transport,
                &source_path,
                &received_path,
                size_bytes,
                chunk_size,
            )
            .await
            .with_context(|| format!("{transport} {chunk_size}-byte chunk-size warm-up failed"))?;
            verify_local_copy(&source_path, &received_path, &source_sha256)?;
        }
    }

    for trial in 1..=5 {
        let mut chunk_order = PHASE2_CHUNK_SIZES;
        if trial % 2 == 0 {
            chunk_order.reverse();
        }
        for (candidate_index, chunk_size) in chunk_order.into_iter().enumerate() {
            let transport_order = if transports.len() == 2 && (trial + candidate_index) % 2 == 0 {
                ["webrtc", "iroh"]
            } else if transports.len() == 2 {
                ["iroh", "webrtc"]
            } else {
                let transport = transports
                    .first()
                    .copied()
                    .ok_or_else(|| anyhow!("chunk-size sweep has no transport"))?;
                [transport, transport]
            };
            let order: Vec<&'static str> = if transports.len() == 2 {
                transport_order.into_iter().collect()
            } else {
                vec![
                    transports
                        .first()
                        .copied()
                        .ok_or_else(|| anyhow!("chunk-size sweep has no transport"))?,
                ]
            };
            for transport in order {
                let (sender, receiver) = run_local_full_transfer(
                    transport,
                    &source_path,
                    &received_path,
                    size_bytes,
                    chunk_size,
                )
                .await
                .with_context(|| {
                    format!("{transport} trial {trial} failed at {chunk_size}-byte chunks")
                })?;
                let received_sha256 = sha256_file(&received_path)?;
                ensure!(
                    received_sha256 == source_sha256,
                    "{transport} trial {trial} at {chunk_size}-byte chunks: SHA-256 mismatch"
                );
                ensure!(
                    fs::metadata(&received_path)
                        .context("failed to read received file metadata")?
                        .len()
                        == size_bytes,
                    "received file size differs from the {size_mib} MiB source"
                );
                append_local_metric(
                    &metrics_path,
                    &sender,
                    transport,
                    size_bytes,
                    trial,
                    false,
                    &source_sha256,
                    &received_sha256,
                )?;
                append_local_metric(
                    &metrics_path,
                    &receiver,
                    transport,
                    size_bytes,
                    trial,
                    false,
                    &source_sha256,
                    &received_sha256,
                )?;
                println!(
                    "chunk sweep transport={transport} trial={trial} chunk_size={chunk_size} sender_payload_s={:.3} receiver_payload_s={:.3} path={} sha256={received_sha256}",
                    sender.payload_seconds,
                    receiver.payload_seconds,
                    route_at_both_ends(&sender.path_start, &sender.path_end),
                );
            }
        }
    }

    if persistent_source.is_none() {
        fs::remove_file(&source_path).context("failed to remove generated sweep input")?;
    }
    fs::remove_file(&received_path).context("failed to remove sweep output")?;
    fs::remove_dir(&temp_dir).context("failed to remove chunk-sweep temp directory")?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "manual 512 MiB process CPU and RSS probe"]
async fn local_transfer_resource_probe() -> Result<()> {
    let transport = std::env::var("RUSTYTRANSFER_RESOURCE_PROBE_TRANSPORT")
        .context("set RUSTYTRANSFER_RESOURCE_PROBE_TRANSPORT to webrtc or iroh")?;
    ensure!(
        transport == "webrtc" || transport == "iroh",
        "unsupported resource-probe transport: {transport}"
    );
    let chunk_kib = std::env::var("RUSTYTRANSFER_RESOURCE_PROBE_CHUNK_KIB")
        .context("set RUSTYTRANSFER_RESOURCE_PROBE_CHUNK_KIB")?
        .parse::<u32>()
        .context("resource-probe chunk size must be an integer number of KiB")?;
    let chunk_size = chunk_kib
        .checked_mul(1024)
        .context("resource-probe chunk size overflowed")?;
    ensure!(chunk_size > 0, "resource-probe chunk size must be positive");

    let size_bytes = 512 * 1024 * 1024_u64;
    let temp_dir = std::env::temp_dir().join(format!(
        "rustytransfer-resource-probe-{}",
        std::process::id()
    ));
    fs::create_dir_all(&temp_dir).context("failed to create resource-probe temp directory")?;
    let (persistent_source, source_path, received_path, source_sha256) =
        prepare_local_benchmark_files(&temp_dir, size_bytes)?;

    for run in ["warmup", "measured"] {
        let (sender, receiver) = run_local_full_transfer(
            if transport == "webrtc" {
                "webrtc"
            } else {
                "iroh"
            },
            &source_path,
            &received_path,
            size_bytes,
            chunk_size,
        )
        .await
        .with_context(|| format!("{transport} {run} resource probe failed"))?;
        verify_local_copy(&source_path, &received_path, &source_sha256)?;
        if run == "measured" {
            println!(
                "resource probe transport={transport} chunk_kib={chunk_kib} path={} sender_payload_s={:.3} receiver_payload_s={:.3} sha256={source_sha256}",
                route_at_both_ends(&sender.path_start, &sender.path_end),
                sender.payload_seconds,
                receiver.payload_seconds,
            );
        }
    }

    fs::remove_file(&received_path).context("failed to remove resource-probe output")?;
    if persistent_source.is_none() {
        fs::remove_file(&source_path).context("failed to remove generated resource-probe input")?;
    }
    fs::remove_dir(&temp_dir).context("failed to remove resource-probe temp directory")?;
    Ok(())
}

async fn run_local_full_transfer(
    transport: &'static str,
    source_path: &Path,
    received_path: &Path,
    size_bytes: u64,
    chunk_size: u32,
) -> Result<(EndpointTimings, EndpointTimings)> {
    remove_stale_local_benchmark_output(source_path, received_path)?;
    let setup_started = Instant::now();
    let expected_path =
        std::env::var("RUSTYTRANSFER_BENCH_EXPECTED_PATH").unwrap_or_else(|_| "direct".to_owned());
    ensure!(
        expected_path == "direct" || expected_path == "relay",
        "unsupported expected benchmark path: {expected_path}"
    );
    let (sender, receiver) = match transport {
        "webrtc" => {
            let (sender, receiver) = timeout(TEST_TIMEOUT, local_webrtc_pair())
                .await
                .context("local WebRTC setup timed out")??;
            (
                DataTransport::WebRtc(sender),
                DataTransport::WebRtc(receiver),
            )
        }
        "iroh" => {
            let (sender, receiver) = timeout(TEST_TIMEOUT, local_iroh_pair())
                .await
                .context("local Iroh setup timed out")??;
            (DataTransport::Iroh(sender), DataTransport::Iroh(receiver))
        }
        _ => anyhow::bail!("unsupported local baseline transport: {transport}"),
    };
    let (sender_path, receiver_path) = if transport == "iroh" {
        tokio::try_join!(
            wait_for_selected_path(&sender, &expected_path),
            wait_for_selected_path(&receiver, &expected_path)
        )?
    } else {
        (
            sender.path_observation().await?,
            receiver.path_observation().await?,
        )
    };
    let setup_seconds = setup_started.elapsed().as_secs_f64();
    ensure!(
        sender_path.path == expected_path,
        "sender selected {:?}, expected {expected_path}",
        sender_path.path
    );
    ensure!(
        receiver_path.path == expected_path,
        "receiver selected {:?}, expected {expected_path}",
        receiver_path.path
    );

    let sender_task = send_local_file(
        sender,
        source_path.to_owned(),
        size_bytes,
        chunk_size,
        sender_path,
        setup_seconds,
    );
    let receiver_task = receive_local_file(
        receiver,
        received_path.to_owned(),
        size_bytes,
        chunk_size,
        receiver_path,
        setup_seconds,
    );
    let (sender_result, receiver_result) = timeout(LOCAL_BENCH_RUN_TIMEOUT, async {
        tokio::join!(sender_task, receiver_task)
    })
    .await
    .context("local full-file transfer exceeded 15 minutes")?;
    match (sender_result, receiver_result) {
        (Ok(mut sender), Ok(mut receiver)) => {
            let direct_route_verified_both =
                verified_direct_evidence(sender.path_evidence.as_ref())
                    && verified_direct_evidence(receiver.path_evidence.as_ref());
            sender.direct_route_verified_both = direct_route_verified_both;
            receiver.direct_route_verified_both = direct_route_verified_both;
            Ok((sender, receiver))
        }
        (Err(sender), Err(receiver)) => Err(anyhow!(
            "{transport} sender failed: {sender:#}; receiver failed: {receiver:#}"
        )),
        (Err(sender), Ok(_)) => Err(sender).with_context(|| format!("{transport} sender failed")),
        (Ok(_), Err(receiver)) => {
            Err(receiver).with_context(|| format!("{transport} receiver failed"))
        }
    }
}

async fn wait_for_selected_path(
    transport: &DataTransport,
    expected_path: &str,
) -> Result<PathObservation> {
    let deadline = Instant::now()
        .checked_add(Duration::from_secs(30))
        .ok_or_else(|| anyhow!("path-selection deadline overflowed"))?;
    loop {
        let observation = transport.path_observation().await?;
        if observation.path == expected_path {
            return Ok(observation);
        }
        ensure!(
            Instant::now() < deadline,
            "Iroh did not select a {expected_path} path within 30 seconds; last observation: {observation:?}"
        );
        sleep(Duration::from_millis(100)).await;
    }
}

async fn send_local_file(
    mut transport: DataTransport,
    source_path: PathBuf,
    size_bytes: u64,
    chunk_size: u32,
    path_start: PathObservation,
    setup_seconds: f64,
) -> Result<EndpointTimings> {
    // Synthetic calibration only; the runner clears this for ordinary trials.
    let calibration_delay = std::env::var("RUSTYTRANSFER_PERFORMANCE_DELAY_MS")
        .ok()
        .map(|value| value.parse::<u64>())
        .transpose()?
        .unwrap_or(0);
    let source = tokio::fs::File::open(&source_path)
        .await
        .context("failed to open local baseline source")?;
    let metrics = transfer::send_file(
        &mut transport,
        source,
        size_bytes,
        b"ABCDE",
        TransferConfig {
            chunk_size: usize::try_from(chunk_size).context("chunk size exceeds usize")?,
        },
        |_, _| {
            if calibration_delay > 0 {
                std::thread::sleep(Duration::from_millis(calibration_delay));
            }
        },
    )
    .await?;
    let path_end = metrics
        .path_end
        .ok_or_else(|| anyhow!("sender path observation is missing"))?;
    let path_evidence = transport.take_path_evidence().await;

    Ok(EndpointTimings {
        role: "sender",
        path_start,
        path_end,
        path_evidence,
        direct_route_verified_both: false,
        chunk_size: metrics.chunk_size,
        bytes_transferred: metrics.bytes_transferred,
        handshake_seconds: setup_seconds + metrics.handshake_seconds,
        payload_seconds: metrics.payload_seconds,
        shutdown_seconds: metrics.shutdown_seconds,
        payload_profile: metrics.payload_profile,
    })
}

async fn receive_local_file(
    mut transport: DataTransport,
    received_path: PathBuf,
    expected_size: u64,
    expected_chunk_size: u32,
    path_start: PathObservation,
    setup_seconds: f64,
) -> Result<EndpointTimings> {
    let metrics =
        transfer::receive_file(&mut transport, b"ABCDE", &received_path, |_, _| {}).await?;
    ensure!(
        metrics.bytes_transferred == expected_size,
        "SMT header file size differs from test input"
    );
    ensure!(
        metrics.chunk_size == expected_chunk_size,
        "SMT header chunk size differs from benchmark configuration"
    );
    let path_end = metrics
        .path_end
        .ok_or_else(|| anyhow!("receiver path observation is missing"))?;
    let path_evidence = transport.take_path_evidence().await;

    Ok(EndpointTimings {
        role: "receiver",
        path_start,
        path_end,
        path_evidence,
        direct_route_verified_both: false,
        chunk_size: metrics.chunk_size,
        bytes_transferred: metrics.bytes_transferred,
        handshake_seconds: setup_seconds + metrics.handshake_seconds,
        payload_seconds: metrics.payload_seconds,
        shutdown_seconds: metrics.shutdown_seconds,
        payload_profile: metrics.payload_profile,
    })
}

fn write_incompressible_file(path: &Path, size_bytes: u64) -> Result<()> {
    let file = File::create(path).context("failed to create local baseline input")?;
    let mut writer = BufWriter::new(file);
    let mut rng = StdRng::seed_from_u64(0x5255_5354_5954_5241);
    let mut buffer = vec![0_u8; 1024 * 1024];
    let mut remaining = size_bytes;
    while remaining > 0 {
        let buffer_len = u64::try_from(buffer.len()).context("buffer length exceeds u64")?;
        let count_u64 = remaining.min(buffer_len);
        let count = usize::try_from(count_u64).context("write size exceeds usize")?;
        let target = buffer
            .get_mut(..count)
            .ok_or_else(|| anyhow!("write size exceeds the random buffer"))?;
        rng.fill_bytes(target);
        writer.write_all(target)?;
        remaining = remaining
            .checked_sub(count_u64)
            .ok_or_else(|| anyhow!("remaining byte count underflowed"))?;
    }
    writer.flush()?;
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String> {
    let output = Command::new("sha256sum")
        .arg(path)
        .output()
        .context("failed to run sha256sum; the manual baseline expects WSL/Linux")?;
    ensure!(
        output.status.success(),
        "sha256sum failed for {}",
        path.display()
    );
    let line = String::from_utf8(output.stdout).context("sha256sum returned non-UTF8 output")?;
    line.split_whitespace()
        .next()
        .map(str::to_owned)
        .ok_or_else(|| anyhow!("sha256sum returned no digest"))
}

fn verify_local_copy(source_path: &Path, received_path: &Path, source_sha256: &str) -> Result<()> {
    let received_sha256 = sha256_file(received_path)?;
    ensure!(received_sha256 == source_sha256, "warm-up SHA-256 mismatch");
    let source_len = fs::metadata(source_path)?.len();
    let received_len = fs::metadata(received_path)?.len();
    ensure!(received_len == source_len, "warm-up file size mismatch");
    Ok(())
}

// Clippy baseline: this helper mirrors the flat benchmark schema and uses an approximate f64 metric.
fn append_local_metric(
    output_path: &Path,
    timing: &EndpointTimings,
    transport: &'static str,
    size_bytes: u64,
    run_index: usize,
    warmup: bool,
    source_sha256: &str,
    received_sha256: &str,
) -> Result<()> {
    let path = route_at_both_ends(&timing.path_start, &timing.path_end);
    let record = LocalBaselineRecord {
        schema_version: 1,
        commit: git_output(&["rev-parse", "HEAD"]),
        working_tree_dirty: git_output(&["status", "--porcelain"]).map(|status| !status.is_empty()),
        role: timing.role,
        transport,
        path,
        path_start: timing.path_start.path,
        path_end: timing.path_end.path,
        path_evidence: timing.path_evidence.clone(),
        direct_route_verified_both: timing.direct_route_verified_both,
        local_candidate_type: timing.path_start.local_candidate_type.clone(),
        remote_candidate_type: timing.path_start.remote_candidate_type.clone(),
        size_bytes,
        chunk_size: timing.chunk_size,
        bytes_transferred: timing.bytes_transferred,
        pipeline_depth: 1,
        handshake_seconds: timing.handshake_seconds,
        payload_seconds: timing.payload_seconds,
        shutdown_seconds: timing.shutdown_seconds,
        wall_seconds: timing.handshake_seconds + timing.payload_seconds + timing.shutdown_seconds,
        effective_mib_per_second: (size_bytes as f64 / (1024.0 * 1024.0))
            / (timing.handshake_seconds + timing.payload_seconds + timing.shutdown_seconds),
        sender_cpu_seconds: None,
        receiver_cpu_seconds: None,
        sender_max_rss_kib: None,
        receiver_max_rss_kib: None,
        source_sha256: source_sha256.to_owned(),
        received_sha256: received_sha256.to_owned(),
        success: true,
        profile_mode: if std::env::var("RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE")
            .is_ok_and(|value| value == "1")
        {
            "payload-profile"
        } else {
            "standard"
        },
        payload_profile: timing.payload_profile.clone(),
        run_index,
        warmup,
        measurement_scope: "sender and receiver share one process",
    };
    let mut output = OpenOptions::new()
        .create(true)
        .append(true)
        .open(output_path)
        .context("failed to open local baseline JSONL")?;
    serde_json::to_writer(&mut output, &record)
        .context("failed to serialize local baseline JSONL")?;
    writeln!(output).context("failed to finish local baseline JSONL")?;
    Ok(())
}

fn verified_direct_evidence(evidence: Option<&rustytransfer::transport::PathEvidence>) -> bool {
    evidence.is_some_and(|value| {
        value.verified
            && value.classification == "direct"
            && (value.direct_stream_tx > 0 || value.direct_stream_rx > 0)
            && value.relay_stream_tx == 0
            && value.relay_stream_rx == 0
            && !value.lagged
            && !value.missing_path_stats
            && !value.relay_selected
    })
}

fn remove_stale_local_benchmark_output(source_path: &Path, received_path: &Path) -> Result<()> {
    if received_path.exists() {
        let source_identity = fs::canonicalize(source_path)
            .context("failed to resolve test-owned benchmark source")?;
        let output_identity = fs::canonicalize(received_path)
            .context("failed to resolve existing test-owned benchmark output")?;
        ensure!(
            source_identity != output_identity,
            "refusing to remove the benchmark source as a stale test output"
        );
        // This path is created by prepare_local_benchmark_files for the
        // ignored benchmark. Remove only that known output before the next
        // receive; product no-overwrite behavior stays unchanged.
        fs::remove_file(received_path).context("failed to remove stale test-owned output")?;
    }
    Ok(())
}

fn route_at_both_ends(start: &PathObservation, end: &PathObservation) -> &'static str {
    if start.path == end.path {
        start.path
    } else if start.path == "unknown" || end.path == "unknown" {
        "unknown"
    } else {
        "mixed"
    }
}

fn git_output(args: &[&str]) -> Option<String> {
    Command::new("git")
        .args(args)
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|output| output.trim().to_owned())
}

#[cfg(test)]
mod local_baseline_tests {
    use super::*;

    #[test]
    fn transport_filter_defaults_to_both_and_can_select_iroh() -> Result<()> {
        assert_eq!(
            local_baseline_transports_from(None)?,
            vec!["webrtc", "iroh"]
        );
        assert_eq!(
            local_baseline_transports_from(Some("all"))?,
            vec!["webrtc", "iroh"]
        );
        assert_eq!(local_baseline_transports_from(Some("iroh"))?, vec!["iroh"]);
        assert_eq!(
            local_baseline_transports_from(Some("webrtc"))?,
            vec!["webrtc"]
        );
        assert!(local_baseline_transports_from(Some("other")).is_err());
        Ok(())
    }

    #[test]
    fn direct_evidence_requires_verified_direct_classification() {
        let evidence = || rustytransfer::transport::PathEvidence {
            classification: "direct",
            verified: true,
            direct_stream_tx: 1,
            direct_stream_rx: 1,
            relay_stream_tx: 0,
            relay_stream_rx: 0,
            lagged: false,
            missing_path_stats: false,
            relay_selected: false,
            connection_stats: None,
        };
        assert!(verified_direct_evidence(Some(&evidence())));

        let mut invalid = evidence();
        invalid.verified = false;
        assert!(!verified_direct_evidence(Some(&invalid)));

        let mut invalid = evidence();
        invalid.classification = "mixed";
        assert!(!verified_direct_evidence(Some(&invalid)));

        let mut invalid = evidence();
        invalid.direct_stream_tx = 0;
        invalid.direct_stream_rx = 0;
        assert!(!verified_direct_evidence(Some(&invalid)));

        let mut invalid = evidence();
        invalid.relay_stream_tx = 1;
        assert!(!verified_direct_evidence(Some(&invalid)));

        let mut invalid = evidence();
        invalid.relay_stream_rx = 1;
        assert!(!verified_direct_evidence(Some(&invalid)));

        let mut invalid = evidence();
        invalid.lagged = true;
        assert!(!verified_direct_evidence(Some(&invalid)));

        let mut invalid = evidence();
        invalid.missing_path_stats = true;
        assert!(!verified_direct_evidence(Some(&invalid)));

        let mut invalid = evidence();
        invalid.relay_selected = true;
        assert!(!verified_direct_evidence(Some(&invalid)));

        assert!(!verified_direct_evidence(None));
    }

    #[test]
    fn stale_output_cleanup_removes_only_the_known_output() -> Result<()> {
        let temp_dir = std::env::temp_dir().join(format!(
            "rustytransfer-stale-output-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)?
                .as_nanos()
        ));
        fs::create_dir_all(&temp_dir)?;
        let source = temp_dir.join("source.bin");
        let output = temp_dir.join("received.bin");
        fs::write(&source, b"source")?;
        fs::write(&output, b"old result")?;

        remove_stale_local_benchmark_output(&source, &output)?;

        ensure!(source.exists(), "source should remain in place");
        ensure!(!output.exists(), "stale output should be removed");
        ensure!(
            remove_stale_local_benchmark_output(&source, &source).is_err(),
            "cleanup must reject a received path that aliases the source"
        );
        fs::remove_dir_all(temp_dir)?;
        Ok(())
    }
}

trait LocalMessageTransport {
    async fn send(&mut self, data: Vec<u8>) -> Result<()>;
    async fn recv(&mut self) -> Result<Vec<u8>>;
    async fn finish_send(&mut self) -> Result<()>;
    async fn finish_recv(&mut self) -> Result<()>;
    async fn wait_for_peer_close(&mut self) -> Result<()>;
    fn close_send(&mut self) -> Result<()>;
    async fn close_transport(&self) -> Result<()>;
}

impl LocalMessageTransport for IrohState {
    async fn send(&mut self, data: Vec<u8>) -> Result<()> {
        self.send_vec(data).await
    }

    async fn recv(&mut self) -> Result<Vec<u8>> {
        self.recv_vec().await
    }

    async fn finish_send(&mut self) -> Result<()> {
        IrohState::finish_send(self).await
    }

    async fn finish_recv(&mut self) -> Result<()> {
        IrohState::finish_recv(self).await
    }

    async fn wait_for_peer_close(&mut self) -> Result<()> {
        IrohState::wait_for_peer_close(self).await
    }

    fn close_send(&mut self) -> Result<()> {
        IrohState::close_send(self)
    }

    async fn close_transport(&self) -> Result<()> {
        IrohState::close_transport(self).await
    }
}

impl LocalMessageTransport for WebRtcState {
    async fn send(&mut self, data: Vec<u8>) -> Result<()> {
        self.send_vec(data).await
    }

    async fn recv(&mut self) -> Result<Vec<u8>> {
        self.recv_vec().await
    }

    async fn finish_send(&mut self) -> Result<()> {
        Ok(())
    }

    async fn finish_recv(&mut self) -> Result<()> {
        Ok(())
    }

    async fn wait_for_peer_close(&mut self) -> Result<()> {
        WebRtcState::wait_for_peer_close(self).await
    }

    fn close_send(&mut self) -> Result<()> {
        Ok(())
    }

    async fn close_transport(&self) -> Result<()> {
        WebRtcState::close_transport(self).await
    }
}
