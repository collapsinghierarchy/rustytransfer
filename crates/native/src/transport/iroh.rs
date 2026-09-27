use anyhow::{Context, Result, anyhow, ensure};
use iroh::{
    Endpoint, EndpointAddr, EndpointId, SecretKey,
    endpoint::{Connection, RecvStream, SendStream, presets},
};
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};
use tokio::io::AsyncWriteExt;
use tokio::time::{Duration, timeout};

use crate::transport::PathObservation;
use crate::transport::errors::TransportError;
use crate::transport::frames::Frame;
use crate::transport::websocket::{WsRead, WsRoomTransport, wait_for_room_full};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(90);
const ENDPOINT_READY_TIMEOUT: Duration = Duration::from_secs(10);
const ENDPOINT_CLOSE_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_MESSAGE_BYTES: usize = 1024 * 1024 + 16; // Max plaintext chunk plus AES-GCM tag.
const IROH_ALPN: &[u8] = b"rustytransfer/2";
const DIRECT_ALPN: &[u8] = b"rustytransfer/direct/2";
pub const IROH_PROBE_ALPN: &[u8] = b"rustytransfer/probe/1";

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
}

impl IrohState {
    fn new(endpoint: Endpoint, connection: Connection, send: SendStream, recv: RecvStream) -> Self {
        Self {
            endpoint,
            connection,
            send,
            recv,
        }
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

        let length = u32::try_from(data.len()).context("message length exceeds u32")?;
        self.send.write_all(&length.to_be_bytes()).await?;
        self.send.write_all(&data).await?;
        self.send.flush().await?;
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
        Ok(data)
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
    if relay_only {
        builder = builder.clear_ip_transports();
    }
    Ok(builder.bind().await?)
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
