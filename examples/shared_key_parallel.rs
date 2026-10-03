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
    fs,
    future::Future,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU8, Ordering},
    },
    time::Instant,
};
use tokio::{
    fs::OpenOptions,
    io::{AsyncRead, AsyncReadExt, AsyncSeekExt, AsyncWriteExt},
    task::JoinSet,
    time::{Duration, timeout},
};

const ALPN: &[u8] = b"rustytransfer/bench-parallel/1";
const VERSION: &str = "shared-key-parallel/1";
const MAX_CHUNK_SIZE: u32 = 1_048_576;
const TAG_BYTES: usize = 16;
const IO_IDLE_TIMEOUT: Duration = Duration::from_secs(30);
const DIRECT_PATH_TIMEOUT: Duration = Duration::from_secs(30);
const DIRECT_PATH_STABLE_FOR: Duration = Duration::from_millis(500);

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
    ranges: Vec<ChunkRange>,
    nonce_prefix: [u8; 8],
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
    ensure!((1..=MAX_CHUNK_SIZE).contains(&manifest.chunk_size));
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

fn manifest_digest(manifest: &Manifest) -> [u8; 32] {
    let mut digest = Sha3_256::new();
    digest.update(manifest.version.as_bytes());
    digest.update(manifest.session_id);
    digest.update(manifest.file_len.to_be_bytes());
    digest.update(manifest.chunk_size.to_be_bytes());
    digest.update([manifest.streams]);
    for range in &manifest.ranges {
        digest.update(range.first.to_be_bytes());
        digest.update(range.end.to_be_bytes());
    }
    digest.update(manifest.nonce_prefix);
    digest.update(Sha3_256::digest(&manifest.kem_ciphertext));
    digest.finalize().into()
}

async fn send_control(state: &mut native_iroh::IrohState, bytes: Vec<u8>) -> Result<()> {
    cancellable(timeout(IO_IDLE_TIMEOUT, state.send_vec(bytes)))
        .await?
        .context("control send idle timeout")?
}

async fn recv_control(state: &mut native_iroh::IrohState) -> Result<Vec<u8>> {
    cancellable(timeout(IO_IDLE_TIMEOUT, state.recv_vec()))
        .await?
        .context("control receive idle timeout")?
}

async fn cancellable<F: Future>(future: F) -> Result<F::Output> {
    tokio::select! {
        result = future => Ok(result),
        signal = wait_for_cancellation() => {
            signal?;
            Err(anyhow::anyhow!("experimental transfer cancelled by signal"))
        }
    }
}

async fn wait_for_direct_path(state: &native_iroh::IrohState) -> Result<()> {
    timeout(DIRECT_PATH_TIMEOUT, async {
        let mut direct_since = None;
        loop {
            if state.path_observation().path == "direct" {
                let since = direct_since.get_or_insert_with(Instant::now);
                if since.elapsed() >= DIRECT_PATH_STABLE_FOR {
                    return Ok(());
                }
            } else {
                direct_since = None;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await
    .context("direct payload path did not become ready")?
}

struct CloseOnDrop(Connection);

impl Drop for CloseOnDrop {
    fn drop(&mut self) {
        self.0.close(0u32.into(), b"experimental session ended");
    }
}

async fn accept_connection(endpoint: &Endpoint) -> Result<(Connection, SendStream, RecvStream)> {
    let incoming = timeout(Duration::from_secs(90), endpoint.accept())
        .await
        .context("waiting for experimental connection timed out")?
        .context("experimental endpoint closed")?;
    let connection = timeout(Duration::from_secs(90), incoming)
        .await
        .context("experimental handshake timed out")??;
    let (send, recv) = timeout(Duration::from_secs(90), connection.accept_bi())
        .await
        .context("waiting for control stream timed out")??;
    Ok((connection, send, recv))
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
    let (mut send, _recv) = timeout(IO_IDLE_TIMEOUT, connection.open_bi())
        .await
        .context("opening data stream idle timeout")??;
    timeout(IO_IDLE_TIMEOUT, send.write_all(&[stream_index]))
        .await
        .context("writing data stream header idle timeout")??;
    let cipher = Aes256Gcm::new_from_slice(key_bytes.as_ref()).context("invalid AES key")?;
    let mut file = tokio::fs::File::open(source).await?;
    let mut index = range.first;
    file.seek(std::io::SeekFrom::Start(chunk_offset(
        index,
        manifest.chunk_size,
    )?))
    .await?;
    while index < range.end {
        let offset = chunk_offset(index, manifest.chunk_size)?;
        let remaining = manifest.file_len.saturating_sub(offset);
        let plain_len = usize::try_from(remaining.min(u64::from(manifest.chunk_size)))?;
        let plaintext = read_exact_chunk(&mut file, plain_len).await?;
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
        timeout(IO_IDLE_TIMEOUT, send.write_all(&ciphertext))
            .await
            .context("writing encrypted chunk idle timeout")??;
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

async fn join_tasks_or_abort<T: Send + 'static>(tasks: &mut JoinSet<Result<T>>) -> Result<Vec<T>> {
    join_tasks_or_abort_after(tasks, Duration::from_secs(900)).await
}

async fn join_tasks_or_abort_after<T: Send + 'static>(
    tasks: &mut JoinSet<Result<T>>,
    deadline: Duration,
) -> Result<Vec<T>> {
    let work = timeout(deadline, async {
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

async fn receive_stream(
    connection: Connection,
    output: PathBuf,
    manifest: Manifest,
    digest: [u8; 32],
    key_bytes: Arc<[u8; 32]>,
    seen_streams: Arc<AtomicU8>,
) -> Result<u8> {
    let (_send, mut recv) = timeout(IO_IDLE_TIMEOUT, connection.accept_bi())
        .await
        .context("accepting data stream idle timeout")??;
    let mut stream_id = [0_u8; 1];
    timeout(IO_IDLE_TIMEOUT, recv.read_exact(&mut stream_id))
        .await
        .context("reading data stream header idle timeout")??;
    let stream_index = stream_id[0];
    ensure!(stream_index < manifest.streams, "invalid stream index");
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
    let mut file = OpenOptions::new()
        .write(true)
        .read(true)
        .open(output)
        .await?;
    for index in range.first..range.end {
        let offset = chunk_offset(index, manifest.chunk_size)?;
        let remaining = manifest.file_len.saturating_sub(offset);
        let plain_len = usize::try_from(remaining.min(u64::from(manifest.chunk_size)))?;
        let encrypted_len = plain_len
            .checked_add(TAG_BYTES)
            .context("ciphertext length overflow")?;
        let mut ciphertext = vec![0_u8; encrypted_len];
        timeout(IO_IDLE_TIMEOUT, recv.read_exact(&mut ciphertext))
            .await
            .context("reading encrypted chunk idle timeout")??;
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
        timeout(IO_IDLE_TIMEOUT, file.seek(std::io::SeekFrom::Start(offset)))
            .await
            .context("seeking output file idle timeout")??;
        timeout(IO_IDLE_TIMEOUT, file.write_all(&plaintext))
            .await
            .context("writing output file idle timeout")??;
    }
    timeout(IO_IDLE_TIMEOUT, file.flush())
        .await
        .context("flushing output file idle timeout")??;
    ensure_stream_eof(&mut recv).await?;
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

async fn read_exact_chunk<R: AsyncRead + Unpin>(reader: &mut R, length: usize) -> Result<Vec<u8>> {
    let mut bytes = vec![0_u8; length];
    timeout(IO_IDLE_TIMEOUT, reader.read_exact(&mut bytes))
        .await
        .context("reading payload idle timeout")??;
    Ok(bytes)
}

async fn ensure_stream_eof<R: AsyncRead + Unpin>(reader: &mut R) -> Result<()> {
    let mut extra = [0_u8; 1];
    ensure!(
        timeout(IO_IDLE_TIMEOUT, reader.read(&mut extra))
            .await
            .context("checking payload end idle timeout")??
            == 0,
        "unexpected trailing stream bytes"
    );
    Ok(())
}

async fn sender(
    source: PathBuf,
    identity: PathBuf,
    chunk_size: u32,
    streams: u8,
    direct: bool,
) -> Result<()> {
    ensure!(
        direct,
        "only direct invite mode is supported by this example"
    );
    let metadata = fs::metadata(&source).context("cannot stat source")?;
    ensure!(metadata.is_file(), "source must be a regular file");
    let file_len = metadata.len();
    let ranges = make_ranges(file_len, chunk_size, streams)?;
    let endpoint = cancellable(bind_sender(&identity)).await??;
    let invite = DirectInvite::generate(endpoint.id().to_string());
    println!("Direct invite: {invite}");
    eprintln!(
        "Applied experimental mode: version={VERSION} streams={streams} payload_keys=1 kem_sessions=1 same_connection=true"
    );
    let handshake_start = Instant::now();
    let (connection, send, recv) = cancellable(accept_connection(&endpoint)).await??;
    let _close_on_drop = CloseOnDrop(connection.clone());
    let mut control = native_iroh::state(endpoint.clone(), connection.clone(), send, recv);
    let hello = recv_control(&mut control).await?;
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
    OsRng.fill_bytes(&mut nonce_prefix);
    let body = Manifest {
        version: VERSION.to_owned(),
        session_id,
        file_len,
        chunk_size,
        streams,
        ranges,
        nonce_prefix,
        kem_ciphertext: encapsulated.ciphertext.as_slice().to_vec(),
    };
    validate_ranges(&body)?;
    let body_bytes = serde_json::to_vec(&body)?;
    let manifest = AuthenticatedManifest {
        body: body.clone(),
        tag: hmac(&invite.token, b"manifest", &body_bytes),
    };
    let digest = manifest_digest(&body);
    send_control(&mut control, serde_json::to_vec(&manifest)?).await?;
    cancellable(wait_for_direct_path(&control)).await??;
    let ready = recv_control(&mut control).await?;
    verify_hmac(&invite.token, b"ready", &digest, &ready)?;
    let handshake_seconds = handshake_start.elapsed().as_secs_f64();
    let path_start = control.path_observation();
    control.begin_payload_observation();
    let payload_start = Instant::now();
    let mut tasks = JoinSet::new();
    for stream_index in 0..streams {
        tasks.spawn(send_file_chunk(
            connection.clone(),
            source.clone(),
            body.clone(),
            digest,
            Arc::clone(&key_bytes),
            stream_index,
        ));
    }
    let _ = join_tasks_or_abort(&mut tasks).await?;
    ensure!(
        fs::metadata(&source)?.len() == file_len,
        "source length changed during transfer"
    );
    let payload_seconds = payload_start.elapsed().as_secs_f64();
    control.end_payload_observation();
    let path_end = control.path_observation();
    let fin = hmac(&invite.token, b"fin", &digest);
    let shutdown_start = Instant::now();
    send_control(&mut control, fin).await?;
    let ack = recv_control(&mut control).await?;
    verify_hmac(&invite.token, b"ack", &digest, &ack)?;
    send_control(&mut control, hmac(&invite.token, b"commit", &digest)).await?;
    let done = recv_control(&mut control).await?;
    verify_hmac(&invite.token, b"done", &digest, &done)?;
    let shutdown_seconds = shutdown_start.elapsed().as_secs_f64();
    let path_evidence = control.take_path_evidence().await;
    let (path, path_start_name, path_end_name) = path_fields(&path_start, &path_end);
    write_metric(Metric {
        schema_version: 1,
        role: "sender",
        transport: "iroh",
        path,
        path_start: path_start_name,
        path_end: path_end_name,
        experimental_protocol_version: VERSION,
        parallel_streams: streams,
        payload_key_count: 1,
        kem_sessions: 1,
        size_bytes: file_len,
        bytes_transferred: file_len,
        chunk_size,
        pipeline_depth: 1,
        local_candidate_type: path_start.local_candidate_type,
        remote_candidate_type: path_start.remote_candidate_type,
        success: true,
        handshake_seconds,
        payload_seconds,
        shutdown_seconds,
        path_evidence,
    })?;
    drop(_close_on_drop);
    endpoint.close().await;
    Ok(())
}

async fn receiver(invite_text: String, output: PathBuf) -> Result<()> {
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
    let endpoint = cancellable(bind_receiver()).await??;
    let connection = cancellable(timeout(
        Duration::from_secs(90),
        endpoint.connect(peer_id, ALPN),
    ))
    .await???;
    let _close_on_drop = CloseOnDrop(connection.clone());
    let (send, recv) =
        cancellable(timeout(Duration::from_secs(90), connection.open_bi())).await???;
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
    let handshake_start = Instant::now();
    send_control(&mut control, hello).await?;
    let encoded: AuthenticatedManifest =
        serde_json::from_slice(&recv_control(&mut control).await?)?;
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
    let (file, mut partial) = create_partial_output(&part).await?;
    file.set_len(encoded.body.file_len).await?;
    drop(file);
    cancellable(wait_for_direct_path(&control)).await??;
    send_control(&mut control, hmac(&invite.token, b"ready", &digest)).await?;
    let handshake_seconds = handshake_start.elapsed().as_secs_f64();
    let path_start = control.path_observation();
    control.begin_payload_observation();
    let payload_start = Instant::now();
    let mut tasks = JoinSet::new();
    let seen_streams = Arc::new(AtomicU8::new(0));
    for _ in 0..encoded.body.streams {
        tasks.spawn(receive_stream(
            connection.clone(),
            part.clone(),
            encoded.body.clone(),
            digest,
            Arc::clone(&key_bytes),
            Arc::clone(&seen_streams),
        ));
    }
    let received = join_tasks_or_abort(&mut tasks).await?;
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
    let path_end = control.path_observation();
    let shutdown_start = Instant::now();
    let fin = recv_control(&mut control).await?;
    verify_hmac(&invite.token, b"fin", &digest, &fin)?;
    send_control(&mut control, hmac(&invite.token, b"ack", &digest)).await?;
    let commit = recv_control(&mut control).await?;
    verify_hmac(&invite.token, b"commit", &digest, &commit)?;
    promote_partial_output(&mut partial, &output)?;
    send_control(&mut control, hmac(&invite.token, b"done", &digest)).await?;
    let shutdown_seconds = shutdown_start.elapsed().as_secs_f64();
    let path_evidence = control.take_path_evidence().await;
    let (path, path_start_name, path_end_name) = path_fields(&path_start, &path_end);
    write_metric(Metric {
        schema_version: 1,
        role: "receiver",
        transport: "iroh",
        path,
        path_start: path_start_name,
        path_end: path_end_name,
        experimental_protocol_version: VERSION,
        parallel_streams: encoded.body.streams,
        payload_key_count: 1,
        kem_sessions: 1,
        size_bytes: encoded.body.file_len,
        bytes_transferred: encoded.body.file_len,
        chunk_size: encoded.body.chunk_size,
        pipeline_depth: 1,
        local_candidate_type: path_start.local_candidate_type,
        remote_candidate_type: path_start.remote_candidate_type,
        success: true,
        handshake_seconds,
        payload_seconds,
        shutdown_seconds,
        path_evidence,
    })?;
    eprintln!(
        "Applied experimental mode: version={VERSION} streams={} payload_keys=1 kem_sessions=1 same_connection=true",
        encoded.body.streams
    );
    drop(_close_on_drop);
    endpoint.close().await;
    Ok(())
}

fn part_path(output: &Path) -> PathBuf {
    let mut path = output.as_os_str().to_owned();
    path.push(".shared-key-part");
    PathBuf::from(path)
}

async fn create_partial_output(path: &Path) -> Result<(tokio::fs::File, PartialOutput)> {
    let file = OpenOptions::new()
        .create_new(true)
        .read(true)
        .write(true)
        .open(path)
        .await?;
    Ok((file, PartialOutput::new(path.to_path_buf())))
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
    let transfer = async move {
        match cli.command {
            Command::Send {
                direct,
                chunk_size,
                streams,
                file,
                identity_file,
            } => {
                let identity_file = match identity_file {
                    Some(path) => path,
                    None => native_iroh::default_identity_path()?,
                };
                sender(file, identity_file, chunk_size, streams, direct).await
            }
            Command::Recv { invite, out } => receiver(invite, out).await,
        }
    };
    timeout(Duration::from_secs(900), transfer)
        .await
        .context("experimental transfer exceeded the 15 minute deadline")?
}

#[cfg(test)]
mod tests {
    use super::*;

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
            ranges,
            nonce_prefix: [2; 8],
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
            ranges: vec![ChunkRange { first: 0, end: 1 }],
            nonce_prefix: [2; 8],
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
            ranges: vec![ChunkRange { first: 0, end: 1 }; 4],
            nonce_prefix: [0; 8],
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
        assert!(read_exact_chunk(&mut reader, 4).await.is_err());
        Ok(())
    }

    #[tokio::test]
    async fn trailing_payload_bytes_are_rejected() -> Result<()> {
        let (mut writer, mut reader) = tokio::io::duplex(8);
        writer.write_all(b"abcd").await?;
        writer.shutdown().await?;
        let _ = read_exact_chunk(&mut reader, 3).await?;
        assert!(ensure_stream_eof(&mut reader).await.is_err());
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
        assert!(join_tasks_or_abort(&mut tasks).await.is_err());
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
            join_tasks_or_abort_after(&mut tasks, Duration::from_millis(1))
                .await
                .is_err()
        );
        assert!(tasks.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn owned_partial_is_removed_on_drop() -> Result<()> {
        let path = test_path("owned-part");
        let (mut file, partial) = create_partial_output(&path).await?;
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
        assert!(create_partial_output(&path).await.is_err());
        assert_eq!(fs::read(&path)?, b"keep");
        fs::remove_file(path)?;
        Ok(())
    }

    #[tokio::test]
    async fn promotion_never_overwrites_an_existing_output() -> Result<()> {
        let partial_path = test_path("partial");
        let output_path = test_path("output");
        let (mut file, mut partial) = create_partial_output(&partial_path).await?;
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
