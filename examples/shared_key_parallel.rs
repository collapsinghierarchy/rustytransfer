//! Isolated benchmark of several authenticated data streams sharing one KEM key.
//!
//! This is an experimental framing protocol, not part of the production CLI or wire protocol.

use aes_gcm::{
    Aes256Gcm, KeyInit, Nonce,
    aead::{Aead, Payload},
};
use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use iroh::{
    Endpoint, EndpointId, SecretKey,
    endpoint::{Connection, RecvStream, SendStream},
};
use ml_kem::KemCore;
use rand::{RngCore, rngs::OsRng};
use rustytransfer::{
    crypto::{kem::KemState, mac::MacState},
    transport::{PathEvidence, PathObservation, iroh as native_iroh},
};
use rustytransfer_native::direct::DirectInvite;
use serde::{Deserialize, Serialize};
use sha3::{Digest, Sha3_256};
use std::{
    collections::HashSet,
    fs,
    future::Future,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU8, Ordering},
    },
    time::{Instant, Instant as StdInstant},
};
use tokio::{
    fs::OpenOptions,
    io::{AsyncRead, AsyncReadExt, AsyncSeekExt, AsyncWriteExt},
    task::JoinSet,
    time::{Duration, timeout},
};

const ALPN: &[u8] = b"rustytransfer/bench-parallel/2";
const VERSION: &str = "shared-key-parallel/2";
const MAX_CHUNK_SIZE: u32 = 1_048_576;
const TAG_BYTES: usize = 16;
const IO_IDLE_TIMEOUT: Duration = Duration::from_secs(30);
const DIRECT_PATH_TIMEOUT: Duration = Duration::from_secs(30);
const DIRECT_PATH_STABLE_FOR: Duration = Duration::from_millis(500);
const SESSION_TIMEOUT: Duration = Duration::from_secs(900);

#[derive(Clone, Copy)]
struct SessionDeadline(tokio::time::Instant);

impl SessionDeadline {
    fn new() -> Self {
        Self::after(SESSION_TIMEOUT)
    }

    #[allow(clippy::arithmetic_side_effects)] // Session deadlines use fixed, bounded durations below 15 minutes.
    fn after(duration: Duration) -> Self {
        Self(tokio::time::Instant::now() + duration)
    }

    fn remaining(self) -> Result<Duration> {
        let remaining = self
            .0
            .saturating_duration_since(tokio::time::Instant::now());
        ensure!(
            !remaining.is_zero(),
            "experimental transfer exceeded its 15 minute deadline"
        );
        Ok(remaining)
    }
}

#[derive(Parser)]
#[command(
    name = "rustytransfer-shared-key-bench",
    about = "Test-only shared-key stream benchmark"
)]
struct Cli {
    #[arg(long, default_value = "iroh")]
    transport: String,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Send {
        #[arg(long)]
        direct: bool,
        #[arg(long, value_parser = parse_chunk_size)]
        chunk_size: u32,
        #[arg(long, value_parser = parse_stream_count)]
        streams: u8,
        #[arg(long, value_parser = parse_connection_count, default_value_t = 1)]
        connections: u8,
        #[arg(long)]
        file: PathBuf,
        #[arg(long)]
        identity_file: Option<PathBuf>,
    },
    Recv {
        #[arg(long)]
        invite: String,
        #[arg(long)]
        out: PathBuf,
    },
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
struct ChunkRange {
    first: u32,
    end: u32,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Manifest {
    version: String,
    session_id: [u8; 16],
    file_len: u64,
    chunk_size: u32,
    streams: u8,
    connections: u8,
    ranges: Vec<ChunkRange>,
    nonce_prefix: [u8; 8],
    binding_nonce_prefix: [u8; 8],
    kem_ciphertext: Vec<u8>,
}

#[derive(Serialize, Deserialize)]
struct AuthenticatedManifest {
    body: Manifest,
    tag: Vec<u8>,
}

#[derive(Serialize)]
struct Metric {
    schema_version: u8,
    role: &'static str,
    transport: &'static str,
    path: &'static str,
    path_start: &'static str,
    path_end: &'static str,
    experimental_protocol_version: &'static str,
    parallel_streams: u8,
    parallel_connections: u8,
    payload_key_count: u8,
    kem_sessions: u8,
    size_bytes: u64,
    bytes_transferred: u64,
    chunk_size: u32,
    pipeline_depth: u8,
    local_candidate_type: Option<String>,
    remote_candidate_type: Option<String>,
    success: bool,
    handshake_seconds: f64,
    payload_seconds: f64,
    shutdown_seconds: f64,
    path_evidence: Option<PathEvidence>,
    connection_evidence: Vec<ConnectionMetricEvidence>,
}

#[derive(Serialize)]
struct ConnectionMetricEvidence {
    connection_index: u8,
    stable_id: usize,
    local_endpoint_id: String,
    remote_endpoint_id: String,
    path: &'static str,
    path_start: &'static str,
    path_end: &'static str,
    path_evidence: Option<PathEvidence>,
}

fn parse_chunk_size(value: &str) -> std::result::Result<u32, String> {
    let size = value.parse::<u32>().map_err(|error| error.to_string())?;
    if !(1..=MAX_CHUNK_SIZE).contains(&size) {
        return Err(format!("chunk size must be 1..={MAX_CHUNK_SIZE}"));
    }
    Ok(size)
}

fn parse_stream_count(value: &str) -> std::result::Result<u8, String> {
    match value.parse::<u8>() {
        Ok(1) => Ok(1),
        Ok(4) => Ok(4),
        _ => Err("stream count must be 1 or 4".to_owned()),
    }
}

fn parse_connection_count(value: &str) -> std::result::Result<u8, String> {
    match value.parse::<u8>() {
        Ok(1) => Ok(1),
        Ok(4) => Ok(4),
        _ => Err("connection count must be 1 or 4".to_owned()),
    }
}

fn validate_geometry(streams: u8, connections: u8) -> Result<()> {
    ensure!(
        matches!((connections, streams), (1, 1 | 4) | (4, 4)),
        "supported geometry is 1 connection/1 or 4 streams, or 4 connections/4 streams"
    );
    Ok(())
}

fn make_ranges(file_len: u64, chunk_size: u32, streams: u8) -> Result<Vec<ChunkRange>> {
    ensure!(chunk_size > 0 && (1..=MAX_CHUNK_SIZE).contains(&chunk_size));
    ensure!(streams == 1 || streams == 4);
    let chunks = file_len.div_ceil(u64::from(chunk_size));
    ensure!(
        chunks <= u64::from(u32::MAX),
        "file requires too many nonce counters"
    );
    let chunks = u32::try_from(chunks)?;
    let streams_u32 = u32::from(streams);
    let base = chunks
        .checked_div(streams_u32)
        .context("invalid stream count")?;
    let remainder = chunks
        .checked_rem(streams_u32)
        .context("invalid stream count")?;
    let mut first = 0_u32;
    let mut ranges = Vec::with_capacity(usize::from(streams));
    for index in 0..streams_u32 {
        let width = base
            .checked_add(u32::from(index < remainder))
            .context("chunk range width overflow")?;
        let end = first.checked_add(width).context("chunk range overflow")?;
        ranges.push(ChunkRange { first, end });
        first = end;
    }
    ensure!(first == chunks, "chunk ranges do not cover the file");
    Ok(ranges)
}

fn validate_ranges(manifest: &Manifest) -> Result<()> {
    ensure!(
        manifest.version == VERSION,
        "unsupported experimental protocol version"
    );
    ensure!(manifest.streams == 1 || manifest.streams == 4);
    validate_geometry(manifest.streams, manifest.connections)?;
    ensure!((1..=MAX_CHUNK_SIZE).contains(&manifest.chunk_size));
    ensure!(
        manifest.nonce_prefix != manifest.binding_nonce_prefix,
        "payload and lane-binding nonce domains overlap"
    );
    let expected = make_ranges(manifest.file_len, manifest.chunk_size, manifest.streams)?;
    ensure!(
        manifest.ranges == expected,
        "manifest ranges overlap or leave a gap"
    );
    Ok(())
}

fn nonce(prefix: &[u8; 8], index: u32) -> [u8; 12] {
    let mut nonce = [0_u8; 12];
    nonce[..8].copy_from_slice(prefix);
    nonce[8..].copy_from_slice(&index.to_be_bytes());
    nonce
}

// Keep each authenticated field explicit so reviewers can see what the tag binds.
#[allow(clippy::too_many_arguments)]
fn chunk_aad(
    version: &str,
    session_id: &[u8; 16],
    digest: &[u8; 32],
    stream: u8,
    range: &ChunkRange,
    index: u32,
    offset: u64,
    len: u32,
) -> Vec<u8> {
    let mut aad = Vec::with_capacity(64);
    aad.extend_from_slice(version.as_bytes());
    aad.extend_from_slice(session_id);
    aad.extend_from_slice(digest);
    aad.push(stream);
    aad.extend_from_slice(&range.first.to_be_bytes());
    aad.extend_from_slice(&range.end.to_be_bytes());
    aad.extend_from_slice(&index.to_be_bytes());
    aad.extend_from_slice(&offset.to_be_bytes());
    aad.extend_from_slice(&len.to_be_bytes());
    aad
}

fn binding_nonce(manifest: &Manifest, lane: u8, response: bool) -> Result<[u8; 12]> {
    let base = u32::from(lane)
        .checked_mul(2)
        .context("lane-binding nonce counter overflow")?;
    let counter = base
        .checked_add(u32::from(response))
        .context("lane-binding nonce counter overflow")?;
    Ok(nonce(&manifest.binding_nonce_prefix, counter))
}

fn binding_aad(manifest: &Manifest, digest: &[u8; 32], lane: u8, domain: &[u8]) -> Result<Vec<u8>> {
    let range = manifest
        .ranges
        .get(usize::from(lane))
        .context("missing authenticated lane range")?;
    let mut aad = Vec::with_capacity(112);
    aad.extend_from_slice(manifest.version.as_bytes());
    aad.extend_from_slice(&manifest.session_id);
    aad.extend_from_slice(digest);
    aad.push(lane);
    aad.extend_from_slice(&range.first.to_be_bytes());
    aad.extend_from_slice(&range.end.to_be_bytes());
    aad.extend_from_slice(domain);
    Ok(aad)
}

fn claim_lane(seen_lanes: &AtomicU8, lane_index: u8, connections: u8) -> Result<()> {
    ensure!(
        lane_index > 0 && lane_index < connections,
        "invalid requested lane index"
    );
    let mask = 1_u8
        .checked_shl(u32::from(lane_index))
        .context("invalid lane index")?;
    ensure!(
        seen_lanes.fetch_or(mask, Ordering::AcqRel) & mask == 0,
        "duplicate lane assignment"
    );
    Ok(())
}

struct LaneConnection {
    lane_index: u8,
    connection: Connection,
    observer: native_iroh::IrohState,
    _close_on_drop: CloseOnDrop,
}

struct LaneConnectionConfig {
    deadline: SessionDeadline,
    endpoint: Endpoint,
    peer_id: EndpointId,
    manifest: Manifest,
    digest: [u8; 32],
    key_bytes: Arc<[u8; 32]>,
    seen_connection_ids: Arc<Mutex<HashSet<usize>>>,
}

async fn accept_lane_connection(
    config: LaneConnectionConfig,
    seen_lanes: Arc<AtomicU8>,
) -> Result<LaneConnection> {
    let LaneConnectionConfig {
        deadline,
        endpoint,
        peer_id,
        manifest,
        digest,
        key_bytes,
        seen_connection_ids,
    } = config;
    let incoming = cancellable_io(deadline, endpoint.accept(), "waiting for lane connection")
        .await?
        .context("endpoint closed while accepting lane connection")?;
    let connecting = incoming
        .accept()
        .context("starting lane connection handshake")?;
    let connection = cancellable_io(deadline, connecting, "lane connection handshake").await??;
    let close_on_drop = CloseOnDrop(connection.clone());
    ensure!(
        connection.remote_id() == peer_id,
        "lane connection peer differs from the primary endpoint"
    );
    let stable_id = connection.stable_id();
    ensure!(
        seen_connection_ids
            .lock()
            .map_err(|error| anyhow::anyhow!("connection ID set poisoned: {error:?}"))?
            .insert(stable_id),
        "duplicate stable connection ID"
    );
    let (send, recv) = cancellable_io(
        deadline,
        connection.accept_bi(),
        "accepting lane-binding stream",
    )
    .await??;
    let mut observer = native_iroh::state(endpoint.clone(), connection.clone(), send, recv);
    let request = recv_control(deadline, &mut observer).await?;
    let (&lane_index, ciphertext) = request
        .split_first()
        .context("missing requested lane index")?;
    claim_lane(&seen_lanes, lane_index, manifest.connections)?;
    let cipher = Aes256Gcm::new_from_slice(key_bytes.as_ref()).context("invalid AES key")?;
    let request_aad = binding_aad(&manifest, &digest, lane_index, b"receiver-lane-request")?;
    let challenge = cipher
        .decrypt(
            Nonce::from_slice(&binding_nonce(&manifest, lane_index, false)?),
            Payload {
                msg: ciphertext,
                aad: &request_aad,
            },
        )
        .map_err(|_error| anyhow::anyhow!("lane binding authentication failed"))?;
    ensure!(
        challenge.len() == 16,
        "invalid lane-binding challenge length"
    );
    let response_aad = binding_aad(&manifest, &digest, lane_index, b"sender-lane-response")?;
    let response = cipher
        .encrypt(
            Nonce::from_slice(&binding_nonce(&manifest, lane_index, true)?),
            Payload {
                msg: &challenge,
                aad: &response_aad,
            },
        )
        .map_err(|_error| anyhow::anyhow!("lane binding response encryption failed"))?;
    send_control(deadline, &mut observer, response).await?;
    Ok(LaneConnection {
        lane_index,
        connection,
        observer,
        _close_on_drop: close_on_drop,
    })
}

async fn dial_lane_connection(
    config: LaneConnectionConfig,
    lane_index: u8,
) -> Result<LaneConnection> {
    let LaneConnectionConfig {
        deadline,
        endpoint,
        peer_id,
        manifest,
        digest,
        key_bytes,
        seen_connection_ids,
    } = config;
    ensure!(lane_index > 0 && lane_index < manifest.connections);
    let connection = cancellable_io(
        deadline,
        endpoint.connect(peer_id, ALPN),
        "dialing lane connection",
    )
    .await??;
    let close_on_drop = CloseOnDrop(connection.clone());
    ensure!(
        connection.remote_id() == peer_id,
        "lane connection peer ID mismatch"
    );
    let stable_id = connection.stable_id();
    ensure!(
        seen_connection_ids
            .lock()
            .map_err(|error| anyhow::anyhow!("connection ID set poisoned: {error:?}"))?
            .insert(stable_id),
        "duplicate stable connection ID"
    );
    let (send, recv) = cancellable_io(
        deadline,
        connection.open_bi(),
        "opening lane-binding stream",
    )
    .await??;
    let mut observer = native_iroh::state(endpoint.clone(), connection.clone(), send, recv);
    let cipher = Aes256Gcm::new_from_slice(key_bytes.as_ref()).context("invalid AES key")?;
    let mut challenge = [0_u8; 16];
    OsRng.fill_bytes(&mut challenge);
    let request_aad = binding_aad(&manifest, &digest, lane_index, b"receiver-lane-request")?;
    let ciphertext = cipher
        .encrypt(
            Nonce::from_slice(&binding_nonce(&manifest, lane_index, false)?),
            Payload {
                msg: &challenge,
                aad: &request_aad,
            },
        )
        .map_err(|_error| anyhow::anyhow!("lane binding request encryption failed"))?;
    let mut request = Vec::with_capacity(ciphertext.len().saturating_add(1));
    request.push(lane_index);
    request.extend_from_slice(&ciphertext);
    send_control(deadline, &mut observer, request).await?;
    let response = recv_control(deadline, &mut observer).await?;
    let response_aad = binding_aad(&manifest, &digest, lane_index, b"sender-lane-response")?;
    let response_plaintext = cipher
        .decrypt(
            Nonce::from_slice(&binding_nonce(&manifest, lane_index, true)?),
            Payload {
                msg: &response,
                aad: &response_aad,
            },
        )
        .map_err(|_error| anyhow::anyhow!("lane binding response authentication failed"))?;
    ensure!(response_plaintext.as_slice() == challenge);
    Ok(LaneConnection {
        lane_index,
        connection,
        observer,
        _close_on_drop: close_on_drop,
    })
}

fn manifest_digest(manifest: &Manifest) -> [u8; 32] {
    let mut digest = Sha3_256::new();
    digest.update(manifest.version.as_bytes());
    digest.update(manifest.session_id);
    digest.update(manifest.file_len.to_be_bytes());
    digest.update(manifest.chunk_size.to_be_bytes());
    digest.update([manifest.streams]);
    digest.update([manifest.connections]);
    for range in &manifest.ranges {
        digest.update(range.first.to_be_bytes());
        digest.update(range.end.to_be_bytes());
    }
    digest.update(manifest.nonce_prefix);
    digest.update(manifest.binding_nonce_prefix);
    digest.update(Sha3_256::digest(&manifest.kem_ciphertext));
    digest.finalize().into()
}

async fn send_control(
    deadline: SessionDeadline,
    state: &mut native_iroh::IrohState,
    bytes: Vec<u8>,
) -> Result<()> {
    cancellable_io(deadline, state.send_vec(bytes), "control send").await?
}

async fn recv_control(
    deadline: SessionDeadline,
    state: &mut native_iroh::IrohState,
) -> Result<Vec<u8>> {
    cancellable_io(deadline, state.recv_vec(), "control receive").await?
}

async fn cancellable<F: Future>(deadline: SessionDeadline, future: F) -> Result<F::Output> {
    let remaining = deadline.remaining()?;
    tokio::select! {
        result = timeout(remaining, future) => result.context("session operation deadline elapsed"),
        signal = wait_for_cancellation() => {
            signal?;
            Err(anyhow::anyhow!("experimental transfer cancelled by signal"))
        }
    }
}

async fn cancellable_io<F: Future>(
    deadline: SessionDeadline,
    future: F,
    operation: &'static str,
) -> Result<F::Output> {
    let timeout = deadline.remaining()?.min(IO_IDLE_TIMEOUT);
    tokio::select! {
        result = timeout_future(timeout, future) => result.with_context(|| format!("{operation} idle timeout")),
        signal = wait_for_cancellation() => {
            signal?;
            Err(anyhow::anyhow!("experimental transfer cancelled by signal"))
        }
    }
}

async fn timeout_future<F: Future>(duration: Duration, future: F) -> Result<F::Output> {
    timeout(duration, future)
        .await
        .context("bounded I/O operation timed out")
}

async fn wait_for_all_direct_paths(states: &[&native_iroh::IrohState]) -> Result<()> {
    ensure!(!states.is_empty(), "no data connections to observe");
    timeout(DIRECT_PATH_TIMEOUT, async {
        let mut direct_since = vec![None; states.len()];
        loop {
            let mut all_stable = true;
            for (state, since_slot) in states.iter().zip(direct_since.iter_mut()) {
                if state.path_observation().path == "direct" {
                    let since = since_slot.get_or_insert_with(StdInstant::now);
                    if since.elapsed() < DIRECT_PATH_STABLE_FOR {
                        all_stable = false;
                    }
                } else {
                    *since_slot = None;
                    all_stable = false;
                }
            }
            if all_stable {
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await
    .context("all data connections did not reach a stable direct path")?
}

async fn wait_for_all_direct_paths_until(
    deadline: SessionDeadline,
    states: &[&native_iroh::IrohState],
) -> Result<()> {
    cancellable(deadline, wait_for_all_direct_paths(states)).await??;
    Ok(())
}

struct CloseOnDrop(Connection);

impl Drop for CloseOnDrop {
    fn drop(&mut self) {
        self.0.close(0u32.into(), b"experimental session ended");
    }
}

async fn accept_connection(
    deadline: SessionDeadline,
    endpoint: &Endpoint,
) -> Result<(Connection, SendStream, RecvStream, CloseOnDrop)> {
    let incoming = cancellable_io(
        deadline,
        endpoint.accept(),
        "waiting for experimental connection",
    )
    .await?
    .context("experimental endpoint closed")?;
    let connecting = incoming
        .accept()
        .context("starting experimental handshake")?;
    let connection = cancellable_io(deadline, connecting, "experimental handshake").await??;
    let close_on_drop = CloseOnDrop(connection.clone());
    let (send, recv) = cancellable_io(
        deadline,
        connection.accept_bi(),
        "waiting for control stream",
    )
    .await??;
    Ok((connection, send, recv, close_on_drop))
}

async fn bind_sender(identity: &Path) -> Result<Endpoint> {
    let key = native_iroh::load_or_create_key(identity)?;
    let endpoint = native_iroh::bind_endpoint_with_key(key, false, ALPN).await?;
    native_iroh::endpoint_addr(&endpoint).await?;
    Ok(endpoint)
}

async fn bind_receiver() -> Result<Endpoint> {
    native_iroh::bind_endpoint_with_key(SecretKey::generate(), false, ALPN).await
}

async fn send_file_chunk(
    deadline: SessionDeadline,
    connection: Connection,
    source: PathBuf,
    manifest: Manifest,
    digest: [u8; 32],
    key_bytes: Arc<[u8; 32]>,
    stream_index: u8,
) -> Result<u8> {
    let range = manifest
        .ranges
        .get(usize::from(stream_index))
        .context("missing stream range")?
        .clone();
    let (mut send, _recv) =
        cancellable_io(deadline, connection.open_bi(), "opening data stream").await??;
    cancellable_io(
        deadline,
        send.write_all(&[stream_index]),
        "writing data stream header",
    )
    .await??;
    let cipher = Aes256Gcm::new_from_slice(key_bytes.as_ref()).context("invalid AES key")?;
    let mut file = cancellable_io(
        deadline,
        tokio::fs::File::open(source),
        "opening source file",
    )
    .await??;
    let mut index = range.first;
    cancellable_io(
        deadline,
        file.seek(std::io::SeekFrom::Start(chunk_offset(
            index,
            manifest.chunk_size,
        )?)),
        "seeking source file",
    )
    .await??;
    while index < range.end {
        let offset = chunk_offset(index, manifest.chunk_size)?;
        let remaining = manifest.file_len.saturating_sub(offset);
        let plain_len = usize::try_from(remaining.min(u64::from(manifest.chunk_size)))?;
        let plaintext = read_exact_chunk(deadline, &mut file, plain_len).await?;
        let len = u32::try_from(plain_len)?;
        let aad = chunk_aad(
            &manifest.version,
            &manifest.session_id,
            &digest,
            stream_index,
            &range,
            index,
            offset,
            len,
        );
        let ciphertext = cipher
            .encrypt(
                Nonce::from_slice(&nonce(&manifest.nonce_prefix, index)),
                Payload {
                    msg: &plaintext,
                    aad: &aad,
                },
            )
            .map_err(|_error| anyhow::anyhow!("AES-GCM encryption failed"))?;
        cancellable_io(
            deadline,
            send.write_all(&ciphertext),
            "writing encrypted chunk",
        )
        .await??;
        index = index.checked_add(1).context("chunk index overflow")?;
    }
    send.finish()?;
    Ok(stream_index)
}

fn chunk_offset(index: u32, chunk_size: u32) -> Result<u64> {
    u64::from(index)
        .checked_mul(u64::from(chunk_size))
        .context("chunk offset overflow")
}

async fn join_tasks_or_abort<T: Send + 'static>(
    tasks: &mut JoinSet<Result<T>>,
    deadline: SessionDeadline,
) -> Result<Vec<T>> {
    join_tasks_or_abort_after(tasks, deadline).await
}

async fn join_tasks_or_abort_after<T: Send + 'static>(
    tasks: &mut JoinSet<Result<T>>,
    deadline: SessionDeadline,
) -> Result<Vec<T>> {
    let remaining = match deadline.remaining() {
        Ok(remaining) => remaining,
        Err(error) => {
            tasks.abort_all();
            while tasks.join_next().await.is_some() {}
            return Err(error);
        }
    };
    let work = timeout(remaining, async {
        let mut values = Vec::new();
        while let Some(joined) = tasks.join_next().await {
            match joined {
                Ok(Ok(value)) => values.push(value),
                Ok(Err(error)) => return Err(error),
                Err(error) => return Err(error.into()),
            }
        }
        Ok(values)
    });
    let (completed, cancellation) = tokio::select! {
        result = work => (Some(result), None),
        signal = wait_for_cancellation() => (None, Some(signal)),
    };
    if let Some(signal) = cancellation {
        tasks.abort_all();
        while tasks.join_next().await.is_some() {}
        signal?;
        return Err(anyhow::anyhow!("experimental transfer cancelled by signal"));
    }
    let completed = completed.context("stream join ended without completion or cancellation")?;
    match completed {
        Ok(Ok(values)) => Ok(values),
        Ok(Err(error)) => {
            tasks.abort_all();
            while tasks.join_next().await.is_some() {}
            Err(error)
        }
        Err(_) => {
            tasks.abort_all();
            while tasks.join_next().await.is_some() {}
            Err(anyhow::anyhow!(
                "experimental data stream exceeded its deadline"
            ))
        }
    }
}

#[cfg(unix)]
async fn wait_for_cancellation() -> Result<()> {
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {
        result = tokio::signal::ctrl_c() => result.context("waiting for Ctrl-C"),
        _ = terminate.recv() => Ok(()),
    }
}

#[cfg(not(unix))]
async fn wait_for_cancellation() -> Result<()> {
    tokio::signal::ctrl_c().await.context("waiting for Ctrl-C")
}

struct ReceiveStreamConfig {
    deadline: SessionDeadline,
    connection: Connection,
    assigned_lane: u8,
    output: PathBuf,
    manifest: Manifest,
    digest: [u8; 32],
    key_bytes: Arc<[u8; 32]>,
    seen_streams: Arc<AtomicU8>,
}

async fn receive_stream(config: ReceiveStreamConfig) -> Result<u8> {
    let ReceiveStreamConfig {
        deadline,
        connection,
        assigned_lane,
        output,
        manifest,
        digest,
        key_bytes,
        seen_streams,
    } = config;
    let (_send, mut recv) =
        cancellable_io(deadline, connection.accept_bi(), "accepting data stream").await??;
    let mut stream_id = [0_u8; 1];
    cancellable_io(
        deadline,
        recv.read_exact(&mut stream_id),
        "reading data stream header",
    )
    .await??;
    let stream_index = stream_id[0];
    validate_stream_assignment(
        manifest.connections,
        assigned_lane,
        stream_index,
        manifest.streams,
    )?;
    let mask = 1_u8
        .checked_shl(u32::from(stream_index))
        .context("invalid stream index")?;
    ensure!(
        seen_streams.fetch_or(mask, Ordering::AcqRel) & mask == 0,
        "duplicate stream index"
    );
    let range = manifest
        .ranges
        .get(usize::from(stream_index))
        .context("invalid stream index")?
        .clone();
    let cipher = Aes256Gcm::new_from_slice(key_bytes.as_ref()).context("invalid AES key")?;
    let mut file = cancellable_io(
        deadline,
        OpenOptions::new().write(true).read(true).open(output),
        "opening partial output lane",
    )
    .await??;
    for index in range.first..range.end {
        let offset = chunk_offset(index, manifest.chunk_size)?;
        let remaining = manifest.file_len.saturating_sub(offset);
        let plain_len = usize::try_from(remaining.min(u64::from(manifest.chunk_size)))?;
        let encrypted_len = plain_len
            .checked_add(TAG_BYTES)
            .context("ciphertext length overflow")?;
        let mut ciphertext = vec![0_u8; encrypted_len];
        cancellable_io(
            deadline,
            recv.read_exact(&mut ciphertext),
            "reading encrypted chunk",
        )
        .await??;
        let len = u32::try_from(plain_len)?;
        let aad = chunk_aad(
            &manifest.version,
            &manifest.session_id,
            &digest,
            stream_index,
            &range,
            index,
            offset,
            len,
        );
        let plaintext = cipher
            .decrypt(
                Nonce::from_slice(&nonce(&manifest.nonce_prefix, index)),
                Payload {
                    msg: &ciphertext,
                    aad: &aad,
                },
            )
            .map_err(|_error| anyhow::anyhow!("AES-GCM authentication failed for chunk {index}"))?;
        ensure!(
            plaintext.len() == plain_len,
            "decrypted chunk has unexpected length"
        );
        cancellable_io(
            deadline,
            file.seek(std::io::SeekFrom::Start(offset)),
            "seeking output file",
        )
        .await??;
        cancellable_io(deadline, file.write_all(&plaintext), "writing output file").await??;
    }
    cancellable_io(deadline, file.flush(), "flushing output file").await??;
    ensure_stream_eof(deadline, &mut recv).await?;
    Ok(stream_index)
}

fn hmac(token: &[u8; 16], domain: &[u8], data: &[u8]) -> Vec<u8> {
    let mut input = Vec::new();
    input.extend_from_slice(domain);
    input.extend_from_slice(data);
    MacState::new(token).tag(&input).to_vec()
}

fn verify_hmac(token: &[u8; 16], domain: &[u8], data: &[u8], tag: &[u8]) -> Result<()> {
    let mut input = Vec::new();
    input.extend_from_slice(domain);
    input.extend_from_slice(data);
    ensure!(
        MacState::new(token).verify(&input, tag),
        "experimental control authentication failed"
    );
    Ok(())
}

fn path_fields(
    start: &PathObservation,
    end: &PathObservation,
) -> (&'static str, &'static str, &'static str) {
    let path = if start.path == end.path {
        start.path
    } else if start.path == "unknown" || end.path == "unknown" {
        "unknown"
    } else {
        "mixed"
    };
    (path, start.path, end.path)
}

fn aggregate_connection_path(connections: &[ConnectionMetricEvidence]) -> &'static str {
    if connections
        .iter()
        .all(|connection| connection.path == "direct")
    {
        "direct"
    } else if connections
        .iter()
        .any(|connection| connection.path == "unknown")
    {
        "unknown"
    } else {
        "mixed"
    }
}

fn connection_metric(
    endpoint: &Endpoint,
    connection: &Connection,
    connection_index: u8,
    start: &PathObservation,
    end: &PathObservation,
    evidence: Option<PathEvidence>,
) -> ConnectionMetricEvidence {
    let (path, path_start, path_end) = path_fields(start, end);
    ConnectionMetricEvidence {
        connection_index,
        stable_id: connection.stable_id(),
        local_endpoint_id: endpoint.id().to_string(),
        remote_endpoint_id: connection.remote_id().to_string(),
        path,
        path_start,
        path_end,
        path_evidence: evidence,
    }
}

fn sort_and_validate_lanes(
    mut lanes: Vec<LaneConnection>,
    connections: u8,
) -> Result<Vec<LaneConnection>> {
    lanes.sort_by_key(|lane| lane.lane_index);
    let expected_lanes = connections
        .checked_sub(1)
        .context("invalid connection count")?;
    ensure!(lanes.len() == usize::from(expected_lanes));
    for (offset, lane) in lanes.iter().enumerate() {
        let expected = u8::try_from(offset.checked_add(1).context("lane index overflow")?)?;
        ensure!(
            lane.lane_index == expected,
            "missing or duplicate lane binding"
        );
    }
    Ok(lanes)
}

fn connection_coverage_mask(connections: u8) -> Result<u8> {
    1_u8.checked_shl(u32::from(connections))
        .and_then(|mask| mask.checked_sub(1))
        .context("invalid connection count mask")
}

fn validate_stream_assignment(
    connections: u8,
    assigned_lane: u8,
    stream_index: u8,
    streams: u8,
) -> Result<()> {
    ensure!(stream_index < streams, "invalid stream index");
    if connections == 4 {
        ensure!(
            stream_index == assigned_lane,
            "data stream arrived on the wrong lane"
        );
    }
    Ok(())
}

async fn read_exact_chunk<R: AsyncRead + Unpin>(
    deadline: SessionDeadline,
    reader: &mut R,
    length: usize,
) -> Result<Vec<u8>> {
    let mut bytes = vec![0_u8; length];
    cancellable_io(deadline, reader.read_exact(&mut bytes), "reading payload").await??;
    Ok(bytes)
}

async fn ensure_stream_eof<R: AsyncRead + Unpin>(
    deadline: SessionDeadline,
    reader: &mut R,
) -> Result<()> {
    let mut extra = [0_u8; 1];
    ensure!(
        cancellable_io(deadline, reader.read(&mut extra), "checking payload end").await?? == 0,
        "unexpected trailing stream bytes"
    );
    Ok(())
}

async fn sender(
    deadline: SessionDeadline,
    source: PathBuf,
    identity: PathBuf,
    chunk_size: u32,
    streams: u8,
    connections: u8,
    direct: bool,
) -> Result<()> {
    ensure!(
        direct,
        "only direct invite mode is supported by this example"
    );
    let metadata = fs::metadata(&source).context("cannot stat source")?;
    ensure!(metadata.is_file(), "source must be a regular file");
    validate_geometry(streams, connections)?;
    let file_len = metadata.len();
    let ranges = make_ranges(file_len, chunk_size, streams)?;
    let handshake_start = Instant::now();
    let endpoint = cancellable(deadline, bind_sender(&identity)).await??;
    let invite = DirectInvite::generate(endpoint.id().to_string());
    println!("Direct invite: {invite}");
    eprintln!(
        "Applied experimental mode: version={VERSION} streams={streams} connections={connections} payload_keys=1 kem_sessions=1"
    );
    let (connection, send, recv, _close_on_drop) =
        cancellable(deadline, accept_connection(deadline, &endpoint)).await??;
    let mut control = native_iroh::state(endpoint.clone(), connection.clone(), send, recv);
    let hello = recv_control(deadline, &mut control).await?;
    ensure!(hello.len() >= 48, "truncated experimental hello");
    let session_bytes = hello
        .get(..16)
        .context("truncated experimental session ID")?;
    let session_id: [u8; 16] = session_bytes.try_into()?;
    let public_key_end = hello
        .len()
        .checked_sub(32)
        .context("truncated hello authentication")?;
    let public_key = hello
        .get(16..public_key_end)
        .context("truncated experimental public key")?;
    let authenticated_hello = hello
        .get(..public_key_end)
        .context("truncated authenticated hello")?;
    let hello_tag = hello
        .get(public_key_end..)
        .context("truncated hello authentication")?;
    verify_hmac(&invite.token, b"hello", authenticated_hello, hello_tag)?;
    let mut kem = KemState::new();
    type EncodedPublicKey = ml_kem::Encoded<<ml_kem::MlKem768 as KemCore>::EncapsulationKey>;
    let encoded = EncodedPublicKey::try_from(public_key)
        .map_err(|_error| anyhow::anyhow!("invalid ML-KEM public key length"))?;
    kem.set_public_key_bytes(&encoded)?;
    let encapsulated = kem.encapsulate()?;
    let key_bytes: Arc<[u8; 32]> = Arc::new(encapsulated.shared_secret.as_slice().try_into()?);
    let mut nonce_prefix = [0_u8; 8];
    let mut binding_nonce_prefix = [0_u8; 8];
    OsRng.fill_bytes(&mut nonce_prefix);
    loop {
        OsRng.fill_bytes(&mut binding_nonce_prefix);
        if binding_nonce_prefix != nonce_prefix {
            break;
        }
    }
    let body = Manifest {
        version: VERSION.to_owned(),
        session_id,
        file_len,
        chunk_size,
        streams,
        connections,
        ranges,
        nonce_prefix,
        binding_nonce_prefix,
        kem_ciphertext: encapsulated.ciphertext.as_slice().to_vec(),
    };
    validate_ranges(&body)?;
    let body_bytes = serde_json::to_vec(&body)?;
    let manifest = AuthenticatedManifest {
        body: body.clone(),
        tag: hmac(&invite.token, b"manifest", &body_bytes),
    };
    let digest = manifest_digest(&body);
    send_control(deadline, &mut control, serde_json::to_vec(&manifest)?).await?;

    let seen_lanes = Arc::new(AtomicU8::new(1));
    let seen_connection_ids = Arc::new(Mutex::new(HashSet::from([connection.stable_id()])));
    let mut lane_tasks = JoinSet::new();
    for _ in 1..connections {
        let endpoint = endpoint.clone();
        let primary_remote_id = connection.remote_id();
        let manifest = body.clone();
        let key_bytes = Arc::clone(&key_bytes);
        let seen_lanes = Arc::clone(&seen_lanes);
        let seen_connection_ids = Arc::clone(&seen_connection_ids);
        lane_tasks.spawn(async move {
            accept_lane_connection(
                LaneConnectionConfig {
                    deadline,
                    endpoint,
                    peer_id: primary_remote_id,
                    manifest,
                    digest,
                    key_bytes,
                    seen_connection_ids,
                },
                seen_lanes,
            )
            .await
        });
    }
    let mut lanes = sort_and_validate_lanes(
        join_tasks_or_abort(&mut lane_tasks, deadline).await?,
        connections,
    )?;
    ensure!(
        seen_lanes.load(Ordering::Acquire) == connection_coverage_mask(connections)?,
        "one or more lane bindings were missing"
    );
    let mut path_states: Vec<&native_iroh::IrohState> = vec![&control];
    path_states.extend(lanes.iter().map(|lane| &lane.observer));
    wait_for_all_direct_paths_until(deadline, &path_states).await?;

    let mut path_starts = Vec::with_capacity(usize::from(connections));
    path_starts.push(control.path_observation());
    control.begin_payload_observation();
    for lane in &mut lanes {
        path_starts.push(lane.observer.path_observation());
        lane.observer.begin_payload_observation();
    }
    let ready = recv_control(deadline, &mut control).await?;
    verify_hmac(&invite.token, b"ready", &digest, &ready)?;
    send_control(
        deadline,
        &mut control,
        hmac(&invite.token, b"observers-ready", &digest),
    )
    .await?;
    let go = recv_control(deadline, &mut control).await?;
    verify_hmac(&invite.token, b"payload-go", &digest, &go)?;
    let handshake_seconds = handshake_start.elapsed().as_secs_f64();
    let payload_start = Instant::now();
    let mut tasks = JoinSet::new();
    for stream_index in 0..streams {
        let stream_connection = match (connections, stream_index) {
            (1, _) | (_, 0) => connection.clone(),
            (4, _) => lanes
                .get(usize::from(
                    stream_index
                        .checked_sub(1)
                        .context("invalid non-primary stream index")?,
                ))
                .context("missing data lane connection")?
                .connection
                .clone(),
            _ => anyhow::bail!("unsupported connection/stream geometry"),
        };
        tasks.spawn(send_file_chunk(
            deadline,
            stream_connection,
            source.clone(),
            body.clone(),
            digest,
            Arc::clone(&key_bytes),
            stream_index,
        ));
    }
    let _ = join_tasks_or_abort(&mut tasks, deadline).await?;
    ensure!(
        fs::metadata(&source)?.len() == file_len,
        "source length changed during transfer"
    );
    let payload_seconds = payload_start.elapsed().as_secs_f64();
    control.end_payload_observation();
    let mut path_ends = Vec::with_capacity(usize::from(connections));
    path_ends.push(control.path_observation());
    for lane in &mut lanes {
        lane.observer.end_payload_observation();
        path_ends.push(lane.observer.path_observation());
    }
    let primary_start = path_starts.first().context("missing primary path start")?;
    let primary_end = path_ends.first().context("missing primary path end")?;
    let fin = hmac(&invite.token, b"fin", &digest);
    let shutdown_start = Instant::now();
    send_control(deadline, &mut control, fin).await?;
    let ack = recv_control(deadline, &mut control).await?;
    verify_hmac(&invite.token, b"ack", &digest, &ack)?;
    send_control(
        deadline,
        &mut control,
        hmac(&invite.token, b"commit", &digest),
    )
    .await?;
    let done = recv_control(deadline, &mut control).await?;
    verify_hmac(&invite.token, b"done", &digest, &done)?;
    let shutdown_seconds = shutdown_start.elapsed().as_secs_f64();
    let path_evidence = cancellable(deadline, control.take_path_evidence()).await?;
    let mut connection_evidence = Vec::with_capacity(usize::from(connections));
    connection_evidence.push(connection_metric(
        &endpoint,
        &connection,
        0,
        primary_start,
        primary_end,
        path_evidence.clone(),
    ));
    for lane in &mut lanes {
        let evidence = cancellable(deadline, lane.observer.take_path_evidence()).await?;
        let lane_index = lane.lane_index;
        let lane_start = path_starts
            .get(usize::from(lane_index))
            .context("missing lane path start")?;
        let lane_end = path_ends
            .get(usize::from(lane_index))
            .context("missing lane path end")?;
        connection_evidence.push(connection_metric(
            &endpoint,
            &lane.connection,
            lane_index,
            lane_start,
            lane_end,
            evidence,
        ));
    }
    let (_, path_start_name, path_end_name) = path_fields(primary_start, primary_end);
    let path = aggregate_connection_path(&connection_evidence);
    write_metric(Metric {
        schema_version: 1,
        role: "sender",
        transport: "iroh",
        path,
        path_start: path_start_name,
        path_end: path_end_name,
        experimental_protocol_version: VERSION,
        parallel_streams: streams,
        parallel_connections: connections,
        payload_key_count: 1,
        kem_sessions: 1,
        size_bytes: file_len,
        bytes_transferred: file_len,
        chunk_size,
        pipeline_depth: 1,
        local_candidate_type: primary_start.local_candidate_type.clone(),
        remote_candidate_type: primary_start.remote_candidate_type.clone(),
        success: true,
        handshake_seconds,
        payload_seconds,
        shutdown_seconds,
        path_evidence,
        connection_evidence,
    })?;
    drop(lanes);
    drop(_close_on_drop);
    cancellable(deadline, endpoint.close()).await?;
    Ok(())
}

async fn receiver(deadline: SessionDeadline, invite_text: String, output: PathBuf) -> Result<()> {
    let handshake_start = Instant::now();
    ensure!(
        !output.exists(),
        "output already exists: {}",
        output.display()
    );
    let part = part_path(&output);
    let invite = DirectInvite::parse(&invite_text)?;
    let peer_id: EndpointId = invite
        .sender_id
        .parse()
        .context("invalid invite sender ID")?;
    let endpoint = cancellable(deadline, bind_receiver()).await??;
    let connection = cancellable_io(
        deadline,
        endpoint.connect(peer_id, ALPN),
        "connecting primary endpoint",
    )
    .await??;
    let _close_on_drop = CloseOnDrop(connection.clone());
    let (send, recv) = cancellable_io(
        deadline,
        connection.open_bi(),
        "opening primary control stream",
    )
    .await??;
    let mut control = native_iroh::state(endpoint.clone(), connection.clone(), send, recv);
    let mut kem = KemState::new();
    kem.generate_keypair();
    let mut session_id = [0_u8; 16];
    OsRng.fill_bytes(&mut session_id);
    let public_key = kem.public_key_bytes()?;
    let hello_capacity = public_key
        .len()
        .checked_add(48)
        .context("hello capacity overflow")?;
    let mut hello = Vec::with_capacity(hello_capacity);
    hello.extend_from_slice(&session_id);
    hello.extend_from_slice(public_key.as_slice());
    let hello_tag = hmac(&invite.token, b"hello", &hello);
    hello.extend_from_slice(&hello_tag);
    send_control(deadline, &mut control, hello).await?;
    let encoded: AuthenticatedManifest =
        serde_json::from_slice(&recv_control(deadline, &mut control).await?)?;
    validate_ranges(&encoded.body)?;
    ensure!(encoded.body.session_id == session_id, "session ID mismatch");
    let body_bytes = serde_json::to_vec(&encoded.body)?;
    verify_hmac(&invite.token, b"manifest", &body_bytes, &encoded.tag)?;
    let digest = manifest_digest(&encoded.body);
    let ciphertext =
        ml_kem::Ciphertext::<ml_kem::MlKem768>::try_from(encoded.body.kem_ciphertext.as_slice())
            .map_err(|_error| anyhow::anyhow!("invalid ML-KEM ciphertext length"))?;
    let shared = kem.decapsulate(&ciphertext)?;
    let key_bytes: Arc<[u8; 32]> = Arc::new(shared.as_slice().try_into()?);
    let (file, mut partial) = create_partial_output(&part)?;
    cancellable_io(
        deadline,
        file.set_len(encoded.body.file_len),
        "sizing partial output",
    )
    .await??;
    drop(file);

    let seen_connection_ids = Arc::new(Mutex::new(HashSet::from([connection.stable_id()])));
    let mut lane_tasks = JoinSet::new();
    for lane_index in 1..encoded.body.connections {
        let endpoint = endpoint.clone();
        let manifest = encoded.body.clone();
        let key_bytes = Arc::clone(&key_bytes);
        let seen_connection_ids = Arc::clone(&seen_connection_ids);
        lane_tasks.spawn(async move {
            dial_lane_connection(
                LaneConnectionConfig {
                    deadline,
                    endpoint,
                    peer_id,
                    manifest,
                    digest,
                    key_bytes,
                    seen_connection_ids,
                },
                lane_index,
            )
            .await
        });
    }
    let mut lanes = sort_and_validate_lanes(
        join_tasks_or_abort(&mut lane_tasks, deadline).await?,
        encoded.body.connections,
    )?;
    let mut path_states: Vec<&native_iroh::IrohState> = vec![&control];
    path_states.extend(lanes.iter().map(|lane| &lane.observer));
    wait_for_all_direct_paths_until(deadline, &path_states).await?;

    let mut path_starts = Vec::with_capacity(usize::from(encoded.body.connections));
    path_starts.push(control.path_observation());
    control.begin_payload_observation();
    for lane in &mut lanes {
        path_starts.push(lane.observer.path_observation());
        lane.observer.begin_payload_observation();
    }
    send_control(
        deadline,
        &mut control,
        hmac(&invite.token, b"ready", &digest),
    )
    .await?;
    let observers_ready = recv_control(deadline, &mut control).await?;
    verify_hmac(&invite.token, b"observers-ready", &digest, &observers_ready)?;
    send_control(
        deadline,
        &mut control,
        hmac(&invite.token, b"payload-go", &digest),
    )
    .await?;
    let handshake_seconds = handshake_start.elapsed().as_secs_f64();
    let payload_start = Instant::now();
    let mut tasks = JoinSet::new();
    let seen_streams = Arc::new(AtomicU8::new(0));
    for lane_index in 0..encoded.body.streams {
        let stream_connection = match (encoded.body.connections, lane_index) {
            (1, _) | (_, 0) => connection.clone(),
            (4, _) => lanes
                .get(usize::from(
                    lane_index
                        .checked_sub(1)
                        .context("invalid non-primary stream index")?,
                ))
                .context("missing data lane connection")?
                .connection
                .clone(),
            _ => anyhow::bail!("unsupported connection/stream geometry"),
        };
        tasks.spawn(receive_stream(ReceiveStreamConfig {
            deadline,
            connection: stream_connection,
            assigned_lane: lane_index,
            output: part.clone(),
            manifest: encoded.body.clone(),
            digest,
            key_bytes: Arc::clone(&key_bytes),
            seen_streams: Arc::clone(&seen_streams),
        }));
    }
    let received = join_tasks_or_abort(&mut tasks, deadline).await?;
    let received_streams: std::collections::HashSet<_> = received.into_iter().collect();
    ensure!(
        received_streams.len() == usize::from(encoded.body.streams),
        "duplicate stream index"
    );
    let expected_mask = 1_u16
        .checked_shl(u32::from(encoded.body.streams))
        .and_then(|mask| mask.checked_sub(1))
        .context("invalid expected stream mask")?;
    ensure!(
        u16::from(seen_streams.load(Ordering::Acquire)) == expected_mask,
        "one or more data streams were missing"
    );
    let payload_seconds = payload_start.elapsed().as_secs_f64();
    control.end_payload_observation();
    let mut path_ends = Vec::with_capacity(usize::from(encoded.body.connections));
    path_ends.push(control.path_observation());
    for lane in &mut lanes {
        lane.observer.end_payload_observation();
        path_ends.push(lane.observer.path_observation());
    }
    let primary_start = path_starts.first().context("missing primary path start")?;
    let primary_end = path_ends.first().context("missing primary path end")?;
    let shutdown_start = Instant::now();
    let fin = recv_control(deadline, &mut control).await?;
    verify_hmac(&invite.token, b"fin", &digest, &fin)?;
    send_control(deadline, &mut control, hmac(&invite.token, b"ack", &digest)).await?;
    let commit = recv_control(deadline, &mut control).await?;
    verify_hmac(&invite.token, b"commit", &digest, &commit)?;
    promote_partial_output(&mut partial, &output)?;
    send_control(
        deadline,
        &mut control,
        hmac(&invite.token, b"done", &digest),
    )
    .await?;
    let shutdown_seconds = shutdown_start.elapsed().as_secs_f64();
    let path_evidence = cancellable(deadline, control.take_path_evidence()).await?;
    let mut connection_evidence = Vec::with_capacity(usize::from(encoded.body.connections));
    connection_evidence.push(connection_metric(
        &endpoint,
        &connection,
        0,
        primary_start,
        primary_end,
        path_evidence.clone(),
    ));
    for lane in &mut lanes {
        let evidence = cancellable(deadline, lane.observer.take_path_evidence()).await?;
        let lane_index = lane.lane_index;
        let lane_start = path_starts
            .get(usize::from(lane_index))
            .context("missing lane path start")?;
        let lane_end = path_ends
            .get(usize::from(lane_index))
            .context("missing lane path end")?;
        connection_evidence.push(connection_metric(
            &endpoint,
            &lane.connection,
            lane_index,
            lane_start,
            lane_end,
            evidence,
        ));
    }
    let (_, path_start_name, path_end_name) = path_fields(primary_start, primary_end);
    let path = aggregate_connection_path(&connection_evidence);
    write_metric(Metric {
        schema_version: 1,
        role: "receiver",
        transport: "iroh",
        path,
        path_start: path_start_name,
        path_end: path_end_name,
        experimental_protocol_version: VERSION,
        parallel_streams: encoded.body.streams,
        parallel_connections: encoded.body.connections,
        payload_key_count: 1,
        kem_sessions: 1,
        size_bytes: encoded.body.file_len,
        bytes_transferred: encoded.body.file_len,
        chunk_size: encoded.body.chunk_size,
        pipeline_depth: 1,
        local_candidate_type: primary_start.local_candidate_type.clone(),
        remote_candidate_type: primary_start.remote_candidate_type.clone(),
        success: true,
        handshake_seconds,
        payload_seconds,
        shutdown_seconds,
        path_evidence,
        connection_evidence,
    })?;
    eprintln!(
        "Applied experimental mode: version={VERSION} streams={} connections={} payload_keys=1 kem_sessions=1",
        encoded.body.streams, encoded.body.connections
    );
    drop(lanes);
    drop(_close_on_drop);
    cancellable(deadline, endpoint.close()).await?;
    Ok(())
}

fn part_path(output: &Path) -> PathBuf {
    let mut path = output.as_os_str().to_owned();
    path.push(".shared-key-part");
    PathBuf::from(path)
}

fn create_partial_output(path: &Path) -> Result<(tokio::fs::File, PartialOutput)> {
    let file = fs::OpenOptions::new()
        .create_new(true)
        .read(true)
        .write(true)
        .open(path)?;
    Ok((
        tokio::fs::File::from_std(file),
        PartialOutput::new(path.to_path_buf()),
    ))
}

fn promote_partial_output(partial: &mut PartialOutput, output: &Path) -> Result<()> {
    fs::hard_link(&partial.path, output).context(
        "cannot atomically promote authenticated output without overwriting an existing file",
    )?;
    if let Err(error) = fs::remove_file(&partial.path) {
        drop(fs::remove_file(output));
        return Err(error).context("cannot remove authenticated partial output after promotion");
    }
    partial.commit();
    Ok(())
}

struct PartialOutput {
    path: PathBuf,
    committed: bool,
}

impl PartialOutput {
    fn new(path: PathBuf) -> Self {
        Self {
            path,
            committed: false,
        }
    }

    fn commit(&mut self) {
        self.committed = true;
    }
}

impl Drop for PartialOutput {
    fn drop(&mut self) {
        if !self.committed {
            drop(fs::remove_file(&self.path));
        }
    }
}

fn write_metric(metric: Metric) -> Result<()> {
    let Ok(path) = std::env::var("RUSTYTRANSFER_METRICS_JSONL") else {
        return Ok(());
    };
    use std::io::Write;
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    serde_json::to_writer(&mut file, &metric)?;
    file.write_all(b"\n")?;
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    ensure!(
        cli.transport == "iroh",
        "only --transport iroh is supported by this example"
    );
    let deadline = SessionDeadline::new();
    match cli.command {
        Command::Send {
            direct,
            chunk_size,
            streams,
            connections,
            file,
            identity_file,
        } => {
            let identity_file = match identity_file {
                Some(path) => path,
                None => native_iroh::default_identity_path()?,
            };
            sender(
                deadline,
                file,
                identity_file,
                chunk_size,
                streams,
                connections,
                direct,
            )
            .await
        }
        Command::Recv { invite, out } => receiver(deadline, invite, out).await,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct ActiveTask(Arc<AtomicU8>);

    impl Drop for ActiveTask {
        fn drop(&mut self) {
            self.0.fetch_sub(1, Ordering::AcqRel);
        }
    }

    fn test_path(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!("rustytransfer-{name}-{}", uuid::Uuid::new_v4()))
    }

    #[test]
    fn ranges_are_contiguous_and_cover_each_chunk_once() -> Result<()> {
        let ranges = make_ranges(10_001, 4096, 4)?;
        assert_eq!(
            ranges,
            vec![
                ChunkRange { first: 0, end: 1 },
                ChunkRange { first: 1, end: 2 },
                ChunkRange { first: 2, end: 3 },
                ChunkRange { first: 3, end: 3 }
            ]
        );
        let manifest = Manifest {
            version: VERSION.to_owned(),
            session_id: [7; 16],
            file_len: 10_001,
            chunk_size: 4096,
            streams: 4,
            connections: 1,
            ranges,
            nonce_prefix: [2; 8],
            binding_nonce_prefix: [3; 8],
            kem_ciphertext: vec![],
        };
        validate_ranges(&manifest)
    }

    #[test]
    fn chunk_nonces_are_unique_across_disjoint_stream_ranges() -> Result<()> {
        let ranges = make_ranges(9_000, 1024, 4)?;
        let mut nonces = std::collections::HashSet::new();
        for range in ranges {
            for index in range.first..range.end {
                ensure!(nonces.insert(nonce(&[3; 8], index)));
            }
        }
        assert_eq!(nonces.len(), 9);
        Ok(())
    }

    #[test]
    fn stream_offset_and_aad_changes_reject_cross_stream_replay() -> Result<()> {
        let manifest = Manifest {
            version: VERSION.to_owned(),
            session_id: [7; 16],
            file_len: 1024,
            chunk_size: 1024,
            streams: 1,
            connections: 1,
            ranges: vec![ChunkRange { first: 0, end: 1 }],
            nonce_prefix: [2; 8],
            binding_nonce_prefix: [3; 8],
            kem_ciphertext: vec![],
        };
        let key = [9_u8; 32];
        let cipher = Aes256Gcm::new_from_slice(&key)?;
        let nonce_bytes = nonce(&manifest.nonce_prefix, 0);
        let digest = manifest_digest(&manifest);
        let range = manifest.ranges.first().context("missing test range")?;
        let aad = chunk_aad(
            &manifest.version,
            &manifest.session_id,
            &digest,
            0,
            range,
            0,
            0,
            3,
        );
        let ciphertext = cipher
            .encrypt(
                Nonce::from_slice(&nonce_bytes),
                Payload {
                    msg: b"abc",
                    aad: &aad,
                },
            )
            .map_err(|_| anyhow::anyhow!("encrypt failed"))?;
        for tampered_aad in [
            chunk_aad(
                &manifest.version,
                &manifest.session_id,
                &digest,
                1,
                range,
                0,
                0,
                3,
            ),
            chunk_aad(
                &manifest.version,
                &manifest.session_id,
                &digest,
                0,
                range,
                0,
                1,
                3,
            ),
            chunk_aad(
                "other-version",
                &manifest.session_id,
                &digest,
                0,
                range,
                0,
                0,
                3,
            ),
        ] {
            assert!(
                cipher
                    .decrypt(
                        Nonce::from_slice(&nonce_bytes),
                        Payload {
                            msg: &ciphertext,
                            aad: &tampered_aad
                        }
                    )
                    .is_err()
            );
        }
        let tampered_session = [8_u8; 16];
        let session_aad = chunk_aad(
            &manifest.version,
            &tampered_session,
            &digest,
            0,
            range,
            0,
            0,
            3,
        );
        assert!(
            cipher
                .decrypt(
                    Nonce::from_slice(&nonce_bytes),
                    Payload {
                        msg: &ciphertext,
                        aad: &session_aad
                    }
                )
                .is_err()
        );
        let tampered_manifest = Manifest {
            file_len: 1025,
            ..manifest.clone()
        };
        let changed_digest = manifest_digest(&tampered_manifest);
        let changed_aad = chunk_aad(
            &manifest.version,
            &manifest.session_id,
            &changed_digest,
            0,
            range,
            0,
            0,
            3,
        );
        assert!(
            cipher
                .decrypt(
                    Nonce::from_slice(&nonce_bytes),
                    Payload {
                        msg: &ciphertext,
                        aad: &changed_aad
                    }
                )
                .is_err()
        );
        Ok(())
    }

    #[test]
    fn invalid_manifest_range_gap_and_overlap_are_rejected() {
        let mut manifest = Manifest {
            version: VERSION.to_owned(),
            session_id: [0; 16],
            file_len: 3,
            chunk_size: 1,
            streams: 4,
            connections: 1,
            ranges: vec![ChunkRange { first: 0, end: 1 }; 4],
            nonce_prefix: [0; 8],
            binding_nonce_prefix: [1; 8],
            kem_ciphertext: vec![],
        };
        assert!(validate_ranges(&manifest).is_err());
        manifest.ranges = vec![
            ChunkRange { first: 0, end: 1 },
            ChunkRange { first: 1, end: 2 },
            ChunkRange { first: 2, end: 3 },
            ChunkRange { first: 3, end: 3 },
        ];
        assert!(validate_ranges(&manifest).is_ok());
    }

    #[test]
    fn chunk_counter_limit_is_enforced() {
        assert!(make_ranges(u64::from(u32::MAX) + 1, 1, 1).is_err());
    }

    #[test]
    fn v2_geometry_nonce_domains_and_binding_aad_are_authenticated() -> Result<()> {
        validate_geometry(1, 1)?;
        validate_geometry(4, 1)?;
        validate_geometry(4, 4)?;
        assert!(validate_geometry(1, 4).is_err());
        assert!(validate_geometry(2, 4).is_err());
        let mut manifest = Manifest {
            version: VERSION.to_owned(),
            session_id: [7; 16],
            file_len: 4096,
            chunk_size: 1024,
            streams: 4,
            connections: 4,
            ranges: make_ranges(4096, 1024, 4)?,
            nonce_prefix: [2; 8],
            binding_nonce_prefix: [3; 8],
            kem_ciphertext: vec![1, 2, 3],
        };
        validate_ranges(&manifest)?;
        let mut binding_nonces = std::collections::HashSet::new();
        for lane in 1..manifest.connections {
            ensure!(binding_nonces.insert(binding_nonce(&manifest, lane, false)?));
            ensure!(binding_nonces.insert(binding_nonce(&manifest, lane, true)?));
        }
        for payload_index in 0..4 {
            ensure!(!binding_nonces.contains(&nonce(&manifest.nonce_prefix, payload_index)));
        }

        let key = [9_u8; 32];
        let cipher = Aes256Gcm::new_from_slice(&key)?;
        let digest = manifest_digest(&manifest);
        let lane = 1;
        let request_aad = binding_aad(&manifest, &digest, lane, b"receiver-lane-request")?;
        let request = cipher
            .encrypt(
                Nonce::from_slice(&binding_nonce(&manifest, lane, false)?),
                Payload {
                    msg: b"fresh challenge",
                    aad: &request_aad,
                },
            )
            .map_err(|_error| anyhow::anyhow!("test request encryption failed"))?;
        ensure!(
            cipher
                .decrypt(
                    Nonce::from_slice(&binding_nonce(&manifest, lane, false)?),
                    Payload {
                        msg: &request,
                        aad: &request_aad,
                    },
                )
                .is_ok()
        );
        let changed_lane = binding_aad(&manifest, &digest, 2, b"receiver-lane-request")?;
        let changed_domain = binding_aad(&manifest, &digest, lane, b"sender-lane-response")?;
        let changed_session = Manifest {
            session_id: [8; 16],
            ..manifest.clone()
        };
        let changed_session_aad =
            binding_aad(&changed_session, &digest, lane, b"receiver-lane-request")?;
        let changed_manifest = Manifest {
            connections: 1,
            ..manifest.clone()
        };
        let changed_manifest_digest = manifest_digest(&changed_manifest);
        let changed_manifest_aad = binding_aad(
            &manifest,
            &changed_manifest_digest,
            lane,
            b"receiver-lane-request",
        )?;
        for aad in [
            changed_lane,
            changed_domain.clone(),
            changed_session_aad,
            changed_manifest_aad,
        ] {
            assert!(
                cipher
                    .decrypt(
                        Nonce::from_slice(&binding_nonce(&manifest, lane, false)?),
                        Payload {
                            msg: &request,
                            aad: &aad
                        },
                    )
                    .is_err()
            );
        }
        let response = cipher
            .encrypt(
                Nonce::from_slice(&binding_nonce(&manifest, lane, true)?),
                Payload {
                    msg: b"fresh challenge",
                    aad: &changed_domain,
                },
            )
            .map_err(|_error| anyhow::anyhow!("test response encryption failed"))?;
        assert!(
            cipher
                .decrypt(
                    Nonce::from_slice(&binding_nonce(&manifest, lane, true)?),
                    Payload {
                        msg: &response,
                        aad: &request_aad,
                    },
                )
                .is_err()
        );

        let seen = AtomicU8::new(1);
        claim_lane(&seen, 1, 4)?;
        assert!(claim_lane(&seen, 1, 4).is_err());
        manifest.binding_nonce_prefix = manifest.nonce_prefix;
        assert!(validate_ranges(&manifest).is_err());
        Ok(())
    }

    #[test]
    fn single_connection_streams_may_be_accepted_out_of_order() -> Result<()> {
        validate_stream_assignment(1, 0, 3, 4)?;
        validate_stream_assignment(1, 1, 0, 4)?;
        assert!(validate_stream_assignment(1, 0, 4, 4).is_err());
        assert!(validate_stream_assignment(4, 1, 0, 4).is_err());
        validate_stream_assignment(4, 1, 1, 4)
    }

    #[test]
    fn control_mac_binds_its_domain_and_manifest_contents() -> Result<()> {
        let token = [5_u8; 16];
        let body = b"manifest-body";
        let tag = hmac(&token, b"manifest", body);
        verify_hmac(&token, b"manifest", body, &tag)?;
        assert!(verify_hmac(&token, b"hello", body, &tag).is_err());
        assert!(verify_hmac(&token, b"manifest", b"changed-body", &tag).is_err());
        Ok(())
    }

    #[tokio::test]
    async fn short_payload_read_is_rejected() -> Result<()> {
        let (mut writer, mut reader) = tokio::io::duplex(8);
        writer.write_all(b"abc").await?;
        writer.shutdown().await?;
        assert!(
            read_exact_chunk(SessionDeadline::new(), &mut reader, 4)
                .await
                .is_err()
        );
        Ok(())
    }

    #[tokio::test]
    async fn trailing_payload_bytes_are_rejected() -> Result<()> {
        let (mut writer, mut reader) = tokio::io::duplex(8);
        writer.write_all(b"abcd").await?;
        writer.shutdown().await?;
        let _ = read_exact_chunk(SessionDeadline::new(), &mut reader, 3).await?;
        assert!(
            ensure_stream_eof(SessionDeadline::new(), &mut reader)
                .await
                .is_err()
        );
        Ok(())
    }

    #[tokio::test]
    async fn failed_stream_aborts_and_joins_siblings() -> Result<()> {
        let mut tasks = JoinSet::new();
        tasks.spawn(async {
            tokio::time::sleep(Duration::from_secs(30)).await;
            Ok::<_, anyhow::Error>(())
        });
        tasks.spawn(async { Err::<(), _>(anyhow::anyhow!("injected stream failure")) });
        assert!(
            join_tasks_or_abort(&mut tasks, SessionDeadline::new())
                .await
                .is_err()
        );
        assert!(tasks.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn stalled_streams_time_out_abort_and_join() -> Result<()> {
        let mut tasks = JoinSet::new();
        tasks.spawn(async {
            tokio::time::sleep(Duration::from_secs(30)).await;
            Ok::<_, anyhow::Error>(())
        });
        assert!(
            join_tasks_or_abort_after(&mut tasks, SessionDeadline::after(Duration::from_millis(1)))
                .await
                .is_err()
        );
        assert!(tasks.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn shared_deadline_aborts_and_drains_sibling_tasks_before_return() -> Result<()> {
        let active = Arc::new(AtomicU8::new(0));
        let writes = Arc::new(AtomicU8::new(0));
        let mut tasks = JoinSet::new();
        for _ in 0..2 {
            let active = Arc::clone(&active);
            let writes = Arc::clone(&writes);
            tasks.spawn(async move {
                active.fetch_add(1, Ordering::AcqRel);
                let _guard = ActiveTask(active);
                loop {
                    writes.fetch_add(1, Ordering::AcqRel);
                    tokio::time::sleep(Duration::from_millis(1)).await;
                }
                #[allow(unreachable_code)]
                Ok::<(), anyhow::Error>(())
            });
        }
        let deadline = SessionDeadline::after(Duration::from_millis(25));
        assert!(join_tasks_or_abort(&mut tasks, deadline).await.is_err());
        assert!(tasks.is_empty());
        assert_eq!(active.load(Ordering::Acquire), 0);
        assert!(writes.load(Ordering::Acquire) > 0);
        let completed_writes = writes.load(Ordering::Acquire);
        tokio::time::sleep(Duration::from_millis(5)).await;
        assert_eq!(writes.load(Ordering::Acquire), completed_writes);
        Ok(())
    }

    #[tokio::test]
    async fn owned_partial_is_removed_on_drop() -> Result<()> {
        let path = test_path("owned-part");
        let (mut file, partial) = create_partial_output(&path)?;
        file.write_all(b"partial").await?;
        drop(file);
        drop(partial);
        assert!(!path.exists());
        Ok(())
    }

    #[tokio::test]
    async fn preexisting_partial_is_never_claimed_or_removed() -> Result<()> {
        let path = test_path("preexisting-part");
        fs::write(&path, b"keep")?;
        assert!(create_partial_output(&path).is_err());
        assert_eq!(fs::read(&path)?, b"keep");
        fs::remove_file(path)?;
        Ok(())
    }

    #[tokio::test]
    async fn promotion_never_overwrites_an_existing_output() -> Result<()> {
        let partial_path = test_path("partial");
        let output_path = test_path("output");
        let (mut file, mut partial) = create_partial_output(&partial_path)?;
        file.write_all(b"complete").await?;
        file.flush().await?;
        drop(file);
        fs::write(&output_path, b"keep")?;
        assert!(promote_partial_output(&mut partial, &output_path).is_err());
        drop(partial);
        assert_eq!(fs::read(&output_path)?, b"keep");
        assert!(!partial_path.exists());
        fs::remove_file(output_path)?;
        Ok(())
    }
}
