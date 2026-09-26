use anyhow::{Context, Result, anyhow, ensure};
use iroh::{
    Endpoint, EndpointAddr, SecretKey,
    endpoint::{Connection, RecvStream, SendStream, presets},
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
const IROH_ALPN: &[u8] = b"rustytransfer/1";

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
    let mut builder = Endpoint::builder(presets::N0)
        .secret_key(SecretKey::generate())
        .alpns(vec![IROH_ALPN.to_vec()]);

    #[cfg(not(target_arch = "wasm32"))]
    if std::env::var("RUSTYTRANSFER_BENCH_RELAY_ONLY").is_ok_and(|value| value == "1") {
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
    let ws = WsRoomTransport::new(app_id.to_string(), "A".to_string());
    let (mut write, mut read) = ws.connect_room().await?;

    let endpoint = bind_endpoint().await?;
    let addr = endpoint_addr(&endpoint).await?;
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
