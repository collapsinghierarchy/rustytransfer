use anyhow::{Result, anyhow};
use std::{
    io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};
use tokio::{
    fs::{File, OpenOptions},
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    time::{Instant, timeout},
};

use crate::{
    error::{CompletionState, Phase, TransferError, TransferErrorKind},
    protocol::{
        fsm::{Role, State},
        receiver::ReceiverFsm,
        sender::SenderFsm,
    },
    transport::DataTransport,
};

const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(90);
const CHUNK_TIMEOUT: Duration = Duration::from_secs(90);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(90);
const FIN_ACK: &[u8] = b"FIN_ACK";
const GCM_TAG_LEN: usize = 16;
pub const MAX_CHUNK_SIZE: usize = 1024 * 1024;
const CLEANUP_TIMEOUT: Duration = Duration::from_secs(5);
static NEXT_TEMP_FILE: AtomicU64 = AtomicU64::new(0);

/// Transfer settings shared by the CLI and local transfer tests.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TransferConfig {
    pub chunk_size: usize,
}

impl Default for TransferConfig {
    fn default() -> Self {
        Self {
            chunk_size: 8 * 1024,
        }
    }
}

impl TransferConfig {
    fn validate(self, file_len: u64) -> std::result::Result<u32, TransferError> {
        let context = TransferContext::new();
        if self.chunk_size == 0 || self.chunk_size > MAX_CHUNK_SIZE {
            return Err(context.error(TransferErrorKind::Config(
                "chunk_size must be between 1 and 1048576 bytes",
            )));
        }
        let chunk_size = u32::try_from(self.chunk_size).map_err(|_source| {
            context.error(TransferErrorKind::Config(
                "chunk_size exceeds the protocol limit",
            ))
        })?;
        let max_file_len = u64::from(chunk_size)
            .checked_mul(u64::from(u32::MAX))
            .ok_or_else(|| context.error(TransferErrorKind::Config("chunk limit overflow")))?;
        if file_len > max_file_len {
            return Err(context.error(TransferErrorKind::Config(
                "file requires more chunks than the nonce counter allows",
            )));
        }
        Ok(chunk_size)
    }
}

#[derive(Clone, Copy)]
struct TransferContext {
    phase: Phase,
    bytes_transferred: u64,
    completion: CompletionState,
}

impl TransferContext {
    const fn new() -> Self {
        Self {
            phase: Phase::Input,
            bytes_transferred: 0,
            completion: CompletionState::NotStarted,
        }
    }

    fn error(self, kind: TransferErrorKind) -> TransferError {
        TransferError::new(kind, self.phase, self.bytes_transferred, self.completion)
    }

    fn protocol(self, error: crate::protocol::fsm::StepError) -> TransferError {
        self.error(TransferErrorKind::Protocol(error))
    }

    fn transport(self, error: anyhow::Error) -> TransferError {
        self.error(TransferErrorKind::Transport(error))
    }
}

struct TempOutput {
    path: Option<PathBuf>,
}

impl TempOutput {
    const fn new() -> Self {
        Self { path: None }
    }

    async fn create(&mut self, destination: &Path) -> io::Result<File> {
        if tokio::fs::try_exists(destination).await? {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "destination already exists",
            ));
        }
        let name = destination.file_name().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "destination has no file name")
        })?;
        for _ in 0..16 {
            let sequence = NEXT_TEMP_FILE.fetch_add(1, Ordering::Relaxed);
            let mut temp_name = std::ffi::OsString::from(".");
            temp_name.push(name);
            temp_name.push(format!(
                ".rustytransfer-{}-{sequence}.part",
                std::process::id()
            ));
            let path = destination.with_file_name(temp_name);
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&path)
                .await
            {
                Ok(file) => {
                    self.path = Some(path);
                    return Ok(file);
                }
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "could not reserve a temporary output file",
        ))
    }

    async fn commit(&mut self, destination: &Path) -> io::Result<()> {
        let path = self.path.as_ref().ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "temporary output file is missing")
        })?;
        // The temporary file is in the same directory. A hard link publishes it
        // atomically and fails when the destination already exists.
        tokio::fs::hard_link(path, destination).await?;
        match tokio::fs::remove_file(path).await {
            Ok(()) => self.path = None,
            Err(error) => eprintln!(
                "warning: committed file, but could not remove temporary copy {}: {error}",
                path.display()
            ),
        }
        Ok(())
    }

    async fn cleanup(&mut self) -> io::Result<()> {
        if let Some(path) = self.path.as_ref() {
            match tokio::fs::remove_file(path).await {
                Ok(()) => {
                    self.path = None;
                    Ok(())
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    self.path = None;
                    Ok(())
                }
                Err(error) => Err(error),
            }
        } else {
            Ok(())
        }
    }
}

impl Drop for TempOutput {
    fn drop(&mut self) {
        if let Some(path) = self.path.take() {
            let _cleanup_result = std::fs::remove_file(path);
        }
    }
}

/// Phase timings and byte counts returned after a complete transfer.
#[derive(Debug, Clone, PartialEq)]
pub struct TransferMetrics {
    pub bytes_transferred: u64,
    pub chunk_size: u32,
    pub handshake_seconds: f64,
    pub payload_seconds: f64,
    pub shutdown_seconds: f64,
    pub path_end: Option<crate::transport::PathObservation>,
    pub cleanup_issues: Vec<&'static str>,
}

/// The message and shutdown operations required by the transfer protocol.
#[async_trait::async_trait]
pub trait TransferTransport: Send {
    async fn send_message(&mut self, data: Vec<u8>) -> Result<()>;
    async fn receive_message(&mut self) -> Result<Vec<u8>>;
    async fn close_send_half(&mut self) -> Result<()>;
    async fn finish_sending(&mut self) -> Result<()>;
    async fn finish_receiving(&mut self) -> Result<()>;
    async fn wait_for_peer_close(&mut self) -> Result<()>;
    async fn close_transport(&mut self) -> Result<()>;

    async fn abort(&mut self) -> Result<()> {
        self.close_transport().await
    }

    async fn observe_path(&mut self) -> Result<Option<crate::transport::PathObservation>> {
        Ok(None)
    }
}

#[async_trait::async_trait]
impl TransferTransport for DataTransport {
    async fn send_message(&mut self, data: Vec<u8>) -> Result<()> {
        DataTransport::send_vec(self, data).await
    }

    async fn receive_message(&mut self) -> Result<Vec<u8>> {
        DataTransport::recv_vec(self).await
    }

    async fn close_send_half(&mut self) -> Result<()> {
        DataTransport::close_send(self)
    }

    async fn finish_sending(&mut self) -> Result<()> {
        DataTransport::finish_send(self).await
    }

    async fn finish_receiving(&mut self) -> Result<()> {
        DataTransport::finish_recv(self).await
    }

    async fn wait_for_peer_close(&mut self) -> Result<()> {
        DataTransport::wait_for_peer_close(self).await
    }

    async fn close_transport(&mut self) -> Result<()> {
        DataTransport::close_transport(self).await
    }

    async fn observe_path(&mut self) -> Result<Option<crate::transport::PathObservation>> {
        DataTransport::path_observation(self).await.map(Some)
    }
}

/// Run the sender side of one encrypted file transfer.
///
/// # Errors
///
/// Returns an error when the configuration is invalid, the source cannot be
/// read, authentication or encryption fails, the peer violates the protocol,
/// or the transport cannot complete the transfer and shutdown sequence.
pub async fn send_file<T, R, F>(
    transport: &mut T,
    source: R,
    file_len: u64,
    password: &[u8],
    config: TransferConfig,
    on_progress: F,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    R: AsyncRead + Unpin + Send,
    F: FnMut(u64, u64) + Send,
{
    let mut context = TransferContext::new();
    let result = send_file_inner(
        transport,
        source,
        file_len,
        password,
        config,
        on_progress,
        &mut context,
    )
    .await;
    match result {
        Ok(metrics) => Ok(metrics),
        Err(mut error) => {
            if let Err(issue) = abort_transport(transport).await {
                error.add_cleanup_issue(issue);
            }
            Err(error)
        }
    }
}

async fn receive_with_timeout<T: TransferTransport>(
    transport: &mut T,
    duration: Duration,
    context: TransferContext,
) -> std::result::Result<Vec<u8>, TransferError> {
    timeout(duration, transport.receive_message())
        .await
        .map_err(|_source| context.error(TransferErrorKind::Timeout))?
        .map_err(|error| context.transport(error))
}

async fn send_with_timeout<T: TransferTransport>(
    transport: &mut T,
    data: Vec<u8>,
    duration: Duration,
    context: TransferContext,
) -> std::result::Result<(), TransferError> {
    timeout(duration, transport.send_message(data))
        .await
        .map_err(|_source| context.error(TransferErrorKind::Timeout))?
        .map_err(|error| context.transport(error))
}

async fn abort_transport<T: TransferTransport>(
    transport: &mut T,
) -> std::result::Result<(), String> {
    match timeout(CLEANUP_TIMEOUT, transport.abort()).await {
        Ok(Ok(())) => Ok(()),
        Ok(Err(error)) => Err(format!("transport abort failed: {error}")),
        Err(_) => Err("transport abort timed out".to_owned()),
    }
}

async fn send_file_inner<T, R, F>(
    transport: &mut T,
    mut source: R,
    file_len: u64,
    password: &[u8],
    config: TransferConfig,
    mut on_progress: F,
    context: &mut TransferContext,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    R: AsyncRead + Unpin + Send,
    F: FnMut(u64, u64) + Send,
{
    let chunk_size = config.validate(file_len)?;
    let mut sender = SenderFsm::new(password.to_vec(), file_len, chunk_size);
    context.phase = Phase::Pake;
    context.completion = CompletionState::InProgress;
    let handshake_started = Instant::now();

    let pake_start = receive_with_timeout(transport, HANDSHAKE_TIMEOUT, *context).await?;
    if sender
        .step("PAKE_START", Some(pake_start))
        .map_err(|error| context.protocol(error))?
        .is_some()
    {
        return Err(context.error(TransferErrorKind::Internal(
            "sender PAKE_START unexpectedly produced output",
        )));
    }

    let pake_answer = match &mut sender.state {
        State::Pake {
            role: Role::Sender,
            pake_state,
            ..
        } => pake_state.take_outbound_msg(),
        _ => {
            return Err(context.error(TransferErrorKind::Internal(
                "sender did not enter PAKE state",
            )));
        }
    };
    send_with_timeout(transport, pake_answer, HANDSHAKE_TIMEOUT, *context).await?;

    context.phase = Phase::KemAuth;
    let auth_kem = receive_with_timeout(transport, HANDSHAKE_TIMEOUT, *context).await?;
    let smt_header = required_output(
        "sender SMT header",
        sender
            .step("RECEIVED_AUTH_KEM", Some(auth_kem))
            .map_err(|error| context.protocol(error))?,
    )
    .map_err(|_source| context.error(TransferErrorKind::Internal("sender SMT header missing")))?;
    context.phase = Phase::Metadata;
    send_with_timeout(transport, smt_header, HANDSHAKE_TIMEOUT, *context).await?;
    let handshake_seconds = handshake_started.elapsed().as_secs_f64();

    context.phase = Phase::Payload;
    on_progress(file_len, 0);
    let payload_started = Instant::now();
    let mut buffer = Vec::new();
    buffer
        .try_reserve_exact(config.chunk_size)
        .map_err(|_source| {
            context.error(TransferErrorKind::Config("chunk buffer allocation failed"))
        })?;
    buffer.resize(config.chunk_size, 0);
    loop {
        let bytes_read = timeout(CHUNK_TIMEOUT, source.read(&mut buffer))
            .await
            .map_err(|_source| context.error(TransferErrorKind::Timeout))?
            .map_err(|error| context.error(TransferErrorKind::SourceIo(error)))?;
        if bytes_read == 0 {
            break;
        }

        let capacity = bytes_read.checked_add(GCM_TAG_LEN).ok_or_else(|| {
            context.error(TransferErrorKind::Internal(
                "plaintext chunk capacity overflow",
            ))
        })?;
        let mut plaintext = Vec::new();
        plaintext.try_reserve_exact(capacity).map_err(|_source| {
            context.error(TransferErrorKind::Config(
                "plaintext chunk allocation failed",
            ))
        })?;
        let bytes = buffer.get(..bytes_read).ok_or_else(|| {
            context.error(TransferErrorKind::Internal("source read exceeded buffer"))
        })?;
        plaintext.extend_from_slice(bytes);
        let ciphertext = required_output(
            "sender ciphertext block",
            sender
                .step("SMT", Some(plaintext))
                .map_err(|error| context.protocol(error))?,
        )
        .map_err(|_source| {
            context.error(TransferErrorKind::Internal("sender ciphertext missing"))
        })?;
        context.bytes_transferred = sender.bytes_sent();
        send_with_timeout(transport, ciphertext, CHUNK_TIMEOUT, *context).await?;
        on_progress(file_len, sender.bytes_sent());
    }
    if sender.bytes_sent() != file_len {
        return Err(context.error(TransferErrorKind::SourceIo(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            format!(
                "source ended after {} of {} bytes",
                sender.bytes_sent(),
                file_len
            ),
        ))));
    }
    let payload_seconds = payload_started.elapsed().as_secs_f64();

    context.phase = Phase::Finalize;
    context.completion = CompletionState::DataCompleteUnconfirmed;
    let shutdown_started = Instant::now();
    let fin = receive_with_timeout(transport, SHUTDOWN_TIMEOUT, *context).await?;
    if sender
        .step("FIN", Some(fin))
        .map_err(|error| context.protocol(error))?
        .is_some()
    {
        return Err(context.error(TransferErrorKind::Internal(
            "sender FIN unexpectedly produced output",
        )));
    }
    if !matches!(sender.state, State::Success(_)) {
        return Err(context.error(TransferErrorKind::Internal("sender did not complete")));
    }
    send_with_timeout(transport, FIN_ACK.to_vec(), SHUTDOWN_TIMEOUT, *context).await?;
    context.completion = CompletionState::ProtocolConfirmed;
    context.phase = Phase::Shutdown;
    timeout(SHUTDOWN_TIMEOUT, transport.close_send_half())
        .await
        .map_err(|_source| context.error(TransferErrorKind::Timeout))?
        .map_err(|error| context.transport(error))?;
    timeout(SHUTDOWN_TIMEOUT, transport.finish_receiving())
        .await
        .map_err(|_source| context.error(TransferErrorKind::Timeout))?
        .map_err(|error| context.transport(error))?;
    let path_end = transport.observe_path().await.ok().flatten();
    timeout(SHUTDOWN_TIMEOUT, transport.wait_for_peer_close())
        .await
        .map_err(|_source| context.error(TransferErrorKind::Timeout))?
        .map_err(|error| context.transport(error))?;
    let mut cleanup_issues = Vec::new();
    if !matches!(
        timeout(CLEANUP_TIMEOUT, transport.close_transport()).await,
        Ok(Ok(()))
    ) {
        cleanup_issues.push("transport close failed after confirmation");
    }

    Ok(TransferMetrics {
        bytes_transferred: file_len,
        chunk_size,
        handshake_seconds,
        payload_seconds,
        shutdown_seconds: shutdown_started.elapsed().as_secs_f64(),
        path_end,
        cleanup_issues,
    })
}

/// Run the receiver side of one encrypted file transfer.
///
/// # Errors
///
/// Returns an error when authentication or decryption fails, the peer violates
/// the protocol, the output file cannot be written, or the transport cannot
/// complete the transfer and shutdown sequence.
pub async fn receive_file<T, F>(
    transport: &mut T,
    password: &[u8],
    output_path: &Path,
    on_progress: F,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    F: FnMut(u64, u64) + Send,
{
    let mut context = TransferContext::new();
    let mut temporary = TempOutput::new();
    let result = receive_file_inner(
        transport,
        password,
        output_path,
        on_progress,
        &mut context,
        &mut temporary,
    )
    .await;
    match result {
        Ok(metrics) => Ok(metrics),
        Err(mut error) => {
            if let Err(issue) = temporary.cleanup().await {
                error.add_cleanup_issue(format!("temporary output cleanup failed: {issue}"));
            }
            if let Err(issue) = abort_transport(transport).await {
                error.add_cleanup_issue(issue);
            }
            Err(error)
        }
    }
}

async fn receive_file_inner<T, F>(
    transport: &mut T,
    password: &[u8],
    output_path: &Path,
    mut on_progress: F,
    context: &mut TransferContext,
    temporary: &mut TempOutput,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    F: FnMut(u64, u64) + Send,
{
    let mut receiver = ReceiverFsm::new(password.to_vec());
    context.phase = Phase::Pake;
    context.completion = CompletionState::InProgress;
    let handshake_started = Instant::now();

    let pake_start = required_output(
        "receiver PAKE_START",
        receiver
            .step("PAKE_START", None)
            .map_err(|error| context.protocol(error))?,
    )
    .map_err(|_source| context.error(TransferErrorKind::Internal("receiver PAKE_START missing")))?;
    send_with_timeout(transport, pake_start, HANDSHAKE_TIMEOUT, *context).await?;

    let pake_answer = receive_with_timeout(transport, HANDSHAKE_TIMEOUT, *context).await?;
    let auth_kem = required_output(
        "receiver AUTH_KEM",
        receiver
            .step("PAKE_ANSWER", Some(pake_answer))
            .map_err(|error| context.protocol(error))?,
    )
    .map_err(|_source| context.error(TransferErrorKind::Internal("receiver AUTH_KEM missing")))?;
    context.phase = Phase::KemAuth;
    send_with_timeout(transport, auth_kem, HANDSHAKE_TIMEOUT, *context).await?;

    context.phase = Phase::Metadata;
    let smt_header = receive_with_timeout(transport, HANDSHAKE_TIMEOUT, *context).await?;
    if receiver
        .step("SMT", Some(smt_header))
        .map_err(|error| context.protocol(error))?
        .is_some()
    {
        return Err(context.error(TransferErrorKind::Internal(
            "receiver SMT header unexpectedly produced output",
        )));
    }
    let total = receiver.file_len();
    let handshake_seconds = handshake_started.elapsed().as_secs_f64();

    let mut output = temporary
        .create(output_path)
        .await
        .map_err(|error| context.error(TransferErrorKind::DestinationIo(error)))?;
    context.phase = Phase::Payload;
    on_progress(total, 0);
    let payload_started = Instant::now();
    while receiver.bytes_received() < total {
        let ciphertext = receive_with_timeout(transport, CHUNK_TIMEOUT, *context).await?;
        let plaintext = required_output(
            "receiver plaintext block",
            receiver
                .step("SMT", Some(ciphertext))
                .map_err(|error| context.protocol(error))?,
        )
        .map_err(|_source| {
            context.error(TransferErrorKind::Internal("receiver plaintext missing"))
        })?;
        output
            .write_all(&plaintext)
            .await
            .map_err(|error| context.error(TransferErrorKind::DestinationIo(error)))?;
        context.bytes_transferred = receiver.bytes_received();
        on_progress(total, receiver.bytes_received());
    }
    output
        .flush()
        .await
        .map_err(|error| context.error(TransferErrorKind::DestinationIo(error)))?;
    let payload_seconds = payload_started.elapsed().as_secs_f64();

    context.phase = Phase::Finalize;
    context.completion = CompletionState::DataCompleteUnconfirmed;
    let shutdown_started = Instant::now();
    let fin = required_output(
        "receiver FIN",
        receiver
            .step("SMT", None)
            .map_err(|error| context.protocol(error))?,
    )
    .map_err(|_source| context.error(TransferErrorKind::Internal("receiver FIN missing")))?;
    send_with_timeout(transport, fin, SHUTDOWN_TIMEOUT, *context).await?;

    let fin_ack = receive_with_timeout(transport, SHUTDOWN_TIMEOUT, *context).await?;
    if fin_ack.as_slice() != FIN_ACK {
        return Err(
            context.protocol(crate::protocol::fsm::StepError::InvalidTransition(
                "unexpected final acknowledgement".into(),
            )),
        );
    }
    context.completion = CompletionState::ProtocolConfirmed;
    if !matches!(receiver.state, State::Success(_)) {
        return Err(context.error(TransferErrorKind::Internal("receiver did not complete")));
    }
    let path_end = transport.observe_path().await.ok().flatten();
    drop(output);
    context.phase = Phase::Finalize;
    temporary
        .commit(output_path)
        .await
        .map_err(|error| context.error(TransferErrorKind::CommitIo(error)))?;
    context.completion = CompletionState::Committed;
    context.phase = Phase::Shutdown;
    // The peer confirmed the full payload. Stream shutdown cannot undo the
    // committed file, so close failures are diagnostic only.
    let mut cleanup_issues = Vec::new();
    if !matches!(
        timeout(CLEANUP_TIMEOUT, transport.finish_receiving()).await,
        Ok(Ok(()))
    ) {
        cleanup_issues.push("receive stream shutdown failed after commit");
    }
    if !matches!(
        timeout(CLEANUP_TIMEOUT, transport.finish_sending()).await,
        Ok(Ok(()))
    ) {
        cleanup_issues.push("send stream shutdown failed after commit");
    }
    if !matches!(
        timeout(CLEANUP_TIMEOUT, transport.close_transport()).await,
        Ok(Ok(()))
    ) {
        cleanup_issues.push("transport close failed after commit");
    }

    Ok(TransferMetrics {
        bytes_transferred: total,
        chunk_size: receiver.chunk_size(),
        handshake_seconds,
        payload_seconds,
        shutdown_seconds: shutdown_started.elapsed().as_secs_f64(),
        path_end,
        cleanup_issues,
    })
}

fn required_output(label: &str, output: Option<Vec<u8>>) -> Result<Vec<u8>> {
    output.ok_or_else(|| anyhow!("{label}: expected an output message"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use anyhow::{Context as AnyhowContext, ensure};
    use std::{
        io::Cursor,
        path::PathBuf,
        sync::atomic::{AtomicU64, Ordering},
    };
    use tokio::sync::{mpsc, watch};

    const TEST_TIMEOUT: Duration = Duration::from_secs(15);
    static NEXT_OUTPUT: AtomicU64 = AtomicU64::new(0);

    struct MemoryTransport {
        tx: Option<mpsc::Sender<Vec<u8>>>,
        rx: mpsc::Receiver<Vec<u8>>,
        peer_closed: watch::Receiver<bool>,
        local_closed: watch::Sender<bool>,
        receive_delay: Duration,
        fin_send_delay: Duration,
        fin_ack_receive_delay: Duration,
    }

    impl MemoryTransport {
        fn pair(
            capacity: usize,
            receive_delay: Duration,
            fin_send_delay: Duration,
            fin_ack_receive_delay: Duration,
        ) -> (Self, Self) {
            let (a_to_b_tx, a_to_b_rx) = mpsc::channel(capacity);
            let (b_to_a_tx, b_to_a_rx) = mpsc::channel(capacity);
            let (a_closed_tx, a_closed_rx) = watch::channel(false);
            let (b_closed_tx, b_closed_rx) = watch::channel(false);
            (
                Self {
                    tx: Some(a_to_b_tx),
                    rx: b_to_a_rx,
                    peer_closed: b_closed_rx,
                    local_closed: a_closed_tx,
                    receive_delay: Duration::ZERO,
                    fin_send_delay: Duration::ZERO,
                    fin_ack_receive_delay: Duration::ZERO,
                },
                Self {
                    tx: Some(b_to_a_tx),
                    rx: a_to_b_rx,
                    peer_closed: a_closed_rx,
                    local_closed: b_closed_tx,
                    receive_delay,
                    fin_send_delay,
                    fin_ack_receive_delay,
                },
            )
        }

        fn close_send_half(&mut self) {
            self.tx.take();
        }
    }

    #[async_trait::async_trait]
    impl TransferTransport for MemoryTransport {
        async fn send_message(&mut self, data: Vec<u8>) -> Result<()> {
            if data == b"FIN" && !self.fin_send_delay.is_zero() {
                tokio::time::sleep(self.fin_send_delay).await;
            }
            self.tx
                .as_ref()
                .ok_or_else(|| anyhow!("local send half is closed"))?
                .send(data)
                .await
                .map_err(|error| anyhow!("peer stopped receiving: {error}"))
        }

        async fn receive_message(&mut self) -> Result<Vec<u8>> {
            let message = self
                .rx
                .recv()
                .await
                .ok_or_else(|| anyhow!("peer closed its sending half"))?;
            if message == b"FIN_ACK" && !self.fin_ack_receive_delay.is_zero() {
                tokio::time::sleep(self.fin_ack_receive_delay).await;
            }
            if !self.receive_delay.is_zero() {
                tokio::time::sleep(self.receive_delay).await;
            }
            Ok(message)
        }

        async fn close_send_half(&mut self) -> Result<()> {
            self.close_send_half();
            Ok(())
        }

        async fn finish_sending(&mut self) -> Result<()> {
            self.close_send_half();
            Ok(())
        }

        async fn finish_receiving(&mut self) -> Result<()> {
            while self.rx.recv().await.is_some() {}
            Ok(())
        }

        async fn wait_for_peer_close(&mut self) -> Result<()> {
            while !*self.peer_closed.borrow() {
                self.peer_closed
                    .changed()
                    .await
                    .context("peer close watch channel ended")?;
            }
            Ok(())
        }

        async fn close_transport(&mut self) -> Result<()> {
            self.local_closed.send_replace(true);
            Ok(())
        }
    }

    #[derive(Clone, Copy)]
    enum PayloadChange {
        Corrupt,
        Truncate,
        Abort,
    }

    struct ChangeThirdMessage<T> {
        inner: T,
        sent_messages: usize,
        change: PayloadChange,
    }

    #[async_trait::async_trait]
    impl<T: TransferTransport> TransferTransport for ChangeThirdMessage<T> {
        async fn send_message(&mut self, mut data: Vec<u8>) -> Result<()> {
            self.sent_messages += 1;
            if self.sent_messages == 3 {
                match self.change {
                    PayloadChange::Corrupt => {
                        let last = data
                            .last_mut()
                            .ok_or_else(|| anyhow!("test ciphertext was empty"))?;
                        *last ^= 0x80;
                    }
                    PayloadChange::Truncate => data.truncate(data.len().saturating_sub(1)),
                    PayloadChange::Abort => return Err(anyhow!("injected sender abort")),
                }
            }
            self.inner.send_message(data).await
        }

        async fn receive_message(&mut self) -> Result<Vec<u8>> {
            self.inner.receive_message().await
        }

        async fn close_send_half(&mut self) -> Result<()> {
            self.inner.close_send_half().await
        }

        async fn finish_sending(&mut self) -> Result<()> {
            self.inner.finish_sending().await
        }

        async fn finish_receiving(&mut self) -> Result<()> {
            self.inner.finish_receiving().await
        }

        async fn wait_for_peer_close(&mut self) -> Result<()> {
            self.inner.wait_for_peer_close().await
        }

        async fn close_transport(&mut self) -> Result<()> {
            self.inner.close_transport().await
        }

        async fn observe_path(&mut self) -> Result<Option<crate::transport::PathObservation>> {
            self.inner.observe_path().await
        }
    }

    fn output_path() -> PathBuf {
        std::env::temp_dir().join(format!(
            "rustytransfer-transfer-{}-{}.bin",
            std::process::id(),
            NEXT_OUTPUT.fetch_add(1, Ordering::Relaxed)
        ))
    }

    async fn transfer_bytes(
        data: Vec<u8>,
        chunk_size: usize,
        receive_delay: Duration,
        fin_send_delay: Duration,
        fin_ack_receive_delay: Duration,
    ) -> Result<Vec<u8>> {
        let (sender, receiver) =
            MemoryTransport::pair(1, receive_delay, fin_send_delay, fin_ack_receive_delay);
        let expected_len = u64::try_from(data.len()).context("test input length exceeds u64")?;
        let output = output_path();
        let sender_future = async move {
            let mut sender = sender;
            send_file(
                &mut sender,
                Cursor::new(data),
                expected_len,
                b"ABCDE",
                TransferConfig { chunk_size },
                |_, _| {},
            )
            .await
        };
        let output_for_receiver = output.clone();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"ABCDE", &output_for_receiver, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("in-memory transfer timed out")?;
        let _sender_metrics = sender_result?;
        let _receiver_metrics = receiver_result?;
        let bytes = tokio::fs::read(&output)
            .await
            .with_context(|| format!("failed to read test output {}", output.display()))?;
        tokio::fs::remove_file(&output).await?;
        Ok(bytes)
    }

    #[tokio::test]
    async fn memory_transfer_covers_empty_and_chunk_boundaries() -> Result<()> {
        const CHUNK: usize = 4096;
        for size in [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, CHUNK * 3 + 19] {
            let input: Vec<u8> = (0..size)
                .map(|index| u8::try_from(index % 251))
                .collect::<std::result::Result<_, _>>()
                .context("test byte pattern exceeds u8")?;
            let output = transfer_bytes(
                input.clone(),
                CHUNK,
                Duration::ZERO,
                Duration::ZERO,
                Duration::ZERO,
            )
            .await?;
            ensure!(output == input, "content mismatch for {size}-byte transfer");
        }
        Ok(())
    }

    #[tokio::test]
    async fn wrong_password_returns_an_error() -> Result<()> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let sender_future = async move {
            let mut sender = sender;
            send_file(
                &mut sender,
                Cursor::new(vec![42]),
                1,
                b"ABCDE",
                TransferConfig::default(),
                |_, _| {},
            )
            .await
        };
        let output = output_path();
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"WRONG", &output, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("wrong-password case timed out")?;
        ensure!(sender_result.is_err(), "sender accepted the wrong password");
        ensure!(
            receiver_result.is_err(),
            "receiver accepted the wrong password"
        );
        Ok(())
    }

    async fn changed_ciphertext_is_rejected(change: PayloadChange) -> Result<()> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let output = output_path();
        let sender_future = async move {
            let mut sender = ChangeThirdMessage {
                inner: sender,
                sent_messages: 0,
                change,
            };
            send_file(
                &mut sender,
                Cursor::new(vec![1, 2, 3, 4, 5, 6, 7, 8]),
                8,
                b"ABCDE",
                TransferConfig { chunk_size: 8 },
                |_, _| {},
            )
            .await
        };
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"ABCDE", &output, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("ciphertext fault case timed out")?;
        ensure!(sender_result.is_err(), "sender unexpectedly completed");
        ensure!(
            receiver_result.is_err(),
            "receiver accepted invalid ciphertext"
        );
        Ok(())
    }

    #[tokio::test]
    async fn corrupted_and_truncated_ciphertext_are_rejected() -> Result<()> {
        changed_ciphertext_is_rejected(PayloadChange::Corrupt).await?;
        changed_ciphertext_is_rejected(PayloadChange::Truncate).await?;
        changed_ciphertext_is_rejected(PayloadChange::Abort).await?;
        Ok(())
    }

    #[tokio::test]
    async fn early_source_eof_is_reported() -> Result<()> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        let output = output_path();
        let sender_future = async move {
            let mut sender = sender;
            send_file(
                &mut sender,
                Cursor::new(vec![1, 2, 3]),
                5,
                b"ABCDE",
                TransferConfig { chunk_size: 8 },
                |_, _| {},
            )
            .await
        };
        let receiver_future = async move {
            let mut receiver = receiver;
            receive_file(&mut receiver, b"ABCDE", &output, |_, _| {}).await
        };
        let (sender_result, receiver_result) = timeout(TEST_TIMEOUT, async {
            tokio::join!(sender_future, receiver_future)
        })
        .await
        .context("early-EOF case timed out")?;
        let sender_error = match sender_result {
            Ok(_) => return Err(anyhow!("sender accepted an early EOF")),
            Err(error) => error,
        };
        ensure!(
            format!("{sender_error:#}").contains("source ended after 3 of 5 bytes"),
            "sender returned an unexpected early-EOF error: {sender_error:#}"
        );
        ensure!(
            receiver_result.is_err(),
            "receiver accepted a truncated file"
        );
        Ok(())
    }

    #[tokio::test]
    async fn bounded_slow_receiver_and_delayed_fin_ack_complete() -> Result<()> {
        let input: Vec<u8> = (0..64 * 1024)
            .map(|index| u8::try_from(index % 239))
            .collect::<std::result::Result<_, _>>()
            .context("test byte pattern exceeds u8")?;
        let output = transfer_bytes(
            input.clone(),
            1024,
            Duration::from_millis(1),
            Duration::from_millis(20),
            Duration::from_millis(20),
        )
        .await?;
        ensure!(output == input, "slow-receiver content mismatch");
        Ok(())
    }

    #[tokio::test]
    async fn peer_aborts_are_reported() -> Result<()> {
        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        drop(receiver);
        let mut sender = sender;
        let sender_result = timeout(
            TEST_TIMEOUT,
            send_file(
                &mut sender,
                Cursor::new(vec![1]),
                1,
                b"ABCDE",
                TransferConfig::default(),
                |_, _| {},
            ),
        )
        .await
        .context("sender-abort case timed out")?;
        ensure!(sender_result.is_err(), "sender ignored receiver abort");

        let (sender, receiver) =
            MemoryTransport::pair(1, Duration::ZERO, Duration::ZERO, Duration::ZERO);
        drop(sender);
        let output = output_path();
        let mut receiver = receiver;
        let receiver_result = timeout(
            TEST_TIMEOUT,
            receive_file(&mut receiver, b"ABCDE", &output, |_, _| {}),
        )
        .await
        .context("receiver-abort case timed out")?;
        ensure!(receiver_result.is_err(), "receiver ignored sender abort");
        Ok(())
    }
}
