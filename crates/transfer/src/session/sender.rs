use crate::*;
use sha3::{Digest, Sha3_256};
use std::io::SeekFrom;
use tokio::io::{AsyncSeek, AsyncSeekExt};

async fn source_prefix_digest<R: AsyncRead + AsyncSeek + Unpin>(
    source: &mut R,
    offset: u64,
) -> io::Result<[u8; 32]> {
    source.seek(SeekFrom::Start(0)).await?;
    let mut remaining = offset;
    let mut hasher = Sha3_256::new();
    let mut buffer = vec![0_u8; 64 * 1024];
    while remaining > 0 {
        let count = usize::try_from(remaining.min(64 * 1024))
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error.to_string()))?;
        let read_buffer = buffer.get_mut(..count).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "source prefix buffer range is invalid",
            )
        })?;
        let bytes_read = source.read(read_buffer).await?;
        if bytes_read == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "source ended before the requested resume offset",
            ));
        }
        let hashed_bytes = buffer.get(..bytes_read).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "source prefix read range is invalid",
            )
        })?;
        hasher.update(hashed_bytes);
        let read = u64::try_from(bytes_read)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error.to_string()))?;
        remaining = remaining.saturating_sub(read);
    }
    source.seek(SeekFrom::Start(offset)).await?;
    Ok(hasher.finalize().into())
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
    R: AsyncRead + AsyncSeek + Unpin + Send,
    F: FnMut(u64, u64) + Send,
{
    send_file_with_auth(
        transport,
        source,
        file_len,
        Authentication::Pake(password),
        config,
        on_progress,
        payload_profile_enabled_from_env(),
    )
    .await
}

/// Run the sender side of one encrypted transfer authenticated by a direct
/// shared token. The two PAKE messages are omitted; the token authenticates
/// the existing KEM exchange.
///
/// # Errors
///
/// Returns an error when the configuration is invalid, the source cannot be
/// read, authentication or encryption fails, the peer violates the protocol,
/// or the transport cannot complete the transfer and shutdown sequence.
pub async fn send_file_direct<T, R, F>(
    transport: &mut T,
    source: R,
    file_len: u64,
    token: &[u8; 16],
    config: TransferConfig,
    on_progress: F,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    R: AsyncRead + AsyncSeek + Unpin + Send,
    F: FnMut(u64, u64) + Send,
{
    send_file_with_auth(
        transport,
        source,
        file_len,
        Authentication::Direct(token),
        config,
        on_progress,
        payload_profile_enabled_from_env(),
    )
    .await
}

#[derive(Clone, Copy)]
enum Authentication<'a> {
    Pake(&'a [u8]),
    Direct(&'a [u8; 16]),
}

async fn send_file_with_auth<T, R, F>(
    transport: &mut T,
    source: R,
    file_len: u64,
    authentication: Authentication<'_>,
    config: TransferConfig,
    on_progress: F,
    profile_enabled: bool,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    R: AsyncRead + AsyncSeek + Unpin + Send,
    F: FnMut(u64, u64) + Send,
{
    let mut context = TransferContext::new();
    context.payload_profile_enabled = profile_enabled;
    let result = send_file_inner(
        transport,
        source,
        file_len,
        authentication,
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

#[cfg(test)]
pub(crate) async fn send_file_with_profile<T, R, F>(
    transport: &mut T,
    source: R,
    file_len: u64,
    password: &[u8],
    config: TransferConfig,
    on_progress: F,
    profile_enabled: bool,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    R: AsyncRead + AsyncSeek + Unpin + Send,
    F: FnMut(u64, u64) + Send,
{
    send_file_with_auth(
        transport,
        source,
        file_len,
        Authentication::Pake(password),
        config,
        on_progress,
        profile_enabled,
    )
    .await
}

async fn send_file_inner<T, R, F>(
    transport: &mut T,
    mut source: R,
    file_len: u64,
    authentication: Authentication<'_>,
    config: TransferConfig,
    mut on_progress: F,
    context: &mut TransferContext,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    R: AsyncRead + AsyncSeek + Unpin + Send,
    F: FnMut(u64, u64) + Send,
{
    let chunk_size = config.validate(file_len)?;
    let mut sender = match authentication {
        Authentication::Pake(password) => SenderFsm::new(password.to_vec(), file_len, chunk_size),
        Authentication::Direct(token) => SenderFsm::new_direct(token, file_len, chunk_size),
    };
    context.phase = match authentication {
        Authentication::Pake(_) => Phase::Pake,
        Authentication::Direct(_) => Phase::KemAuth,
    };
    context.completion = CompletionState::InProgress;
    let handshake_started = Instant::now();

    if matches!(authentication, Authentication::Pake(_)) {
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
    }

    context.phase = Phase::KemAuth;
    let auth_kem = receive_with_timeout(transport, HANDSHAKE_TIMEOUT, *context).await?;
    let offer = sender
        .authenticate_resume_offer(&auth_kem)
        .map_err(|error| context.protocol(error))?;
    let source_digest = if offer.offset <= file_len {
        source_prefix_digest(&mut source, offer.offset)
            .await
            .map_err(|error| context.error(TransferErrorKind::SourceIo(error)))?
    } else {
        Sha3_256::digest([]).into()
    };
    sender.set_source_prefix_digest(source_digest);
    let smt_header = required_output(
        "sender SMT header",
        sender
            .step("RECEIVED_AUTH_KEM", Some(auth_kem))
            .map_err(|error| context.protocol(error))?,
    )
    .map_err(|_source| context.error(TransferErrorKind::Internal("sender SMT header missing")))?;
    let resume_offset = sender.resume_offset();
    source
        .seek(SeekFrom::Start(resume_offset))
        .await
        .map_err(|error| context.error(TransferErrorKind::SourceIo(error)))?;
    context.phase = Phase::Metadata;
    send_with_timeout(transport, smt_header, HANDSHAKE_TIMEOUT, *context).await?;
    let handshake_seconds = handshake_started.elapsed().as_secs_f64();

    context.phase = Phase::Payload;
    on_progress(file_len, resume_offset);
    transport.begin_payload_observation();
    let payload_started = Instant::now();
    let mut payload_profile =
        crate::PayloadProfile::for_enabled_transfer(context.payload_profile_enabled);
    let allocation_started = payload_profile
        .as_ref()
        .map(crate::PayloadProfile::start_stage);
    let mut buffer = Vec::new();
    buffer
        .try_reserve_exact(config.chunk_size)
        .map_err(|_source| {
            context.error(TransferErrorKind::Config("chunk buffer allocation failed"))
        })?;
    buffer.resize(config.chunk_size, 0);
    if let (Some(profile), Some(started)) = (&mut payload_profile, allocation_started) {
        profile.record_sender_allocation_copy(started);
        profile.record_stage(PayloadStage::AllocationCopyEncrypt, started);
    }
    let remaining_len = sender.remaining_len();
    while sender.bytes_sent() < remaining_len {
        let remaining = remaining_len.saturating_sub(sender.bytes_sent());
        let read_limit =
            usize::try_from(remaining.min(u64::from(chunk_size))).map_err(|_error| {
                context.error(TransferErrorKind::Internal("source read size is invalid"))
            })?;
        let read_buffer = buffer.get_mut(..read_limit).ok_or_else(|| {
            context.error(TransferErrorKind::Internal(
                "source read buffer range is invalid",
            ))
        })?;
        let read_started = payload_profile
            .as_ref()
            .map(crate::PayloadProfile::start_stage);
        let bytes_read = timeout(CHUNK_TIMEOUT, source.read(read_buffer))
            .await
            .map_err(|_source| context.error(TransferErrorKind::Timeout))?
            .map_err(|error| context.error(TransferErrorKind::SourceIo(error)))?;
        if let (Some(profile), Some(started)) = (&mut payload_profile, read_started) {
            profile.record_stage(PayloadStage::SourceRead, started);
        }
        if bytes_read == 0 {
            return Err(context.error(TransferErrorKind::SourceIo(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                format!(
                    "source ended after {} of {} bytes",
                    sender.bytes_sent(),
                    remaining_len
                ),
            ))));
        }

        let crypto_started = payload_profile
            .as_ref()
            .map(crate::PayloadProfile::start_stage);
        let allocation_copy_started = payload_profile
            .as_ref()
            .map(crate::PayloadProfile::start_stage);
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
        if let (Some(profile), Some(started)) = (&mut payload_profile, allocation_copy_started) {
            profile.record_sender_allocation_copy(started);
        }
        let encrypt_started = payload_profile
            .as_ref()
            .map(crate::PayloadProfile::start_stage);
        let ciphertext = required_output(
            "sender ciphertext block",
            sender
                .step("SMT", Some(plaintext))
                .map_err(|error| context.protocol(error))?,
        )
        .map_err(|_source| {
            context.error(TransferErrorKind::Internal("sender ciphertext missing"))
        })?;
        if let (Some(profile), Some(started)) = (&mut payload_profile, encrypt_started) {
            profile.record_sender_encrypt(started);
        }
        if let (Some(profile), Some(started)) = (&mut payload_profile, crypto_started) {
            profile.record_stage(PayloadStage::AllocationCopyEncrypt, started);
        }
        context.bytes_transferred = sender.bytes_sent();
        let send_started = payload_profile
            .as_ref()
            .map(crate::PayloadProfile::start_stage);
        send_with_timeout(transport, ciphertext, CHUNK_TIMEOUT, *context).await?;
        if let (Some(profile), Some(started)) = (&mut payload_profile, send_started) {
            profile.record_stage(PayloadStage::SendWait, started);
            profile.chunk_count = profile.chunk_count.saturating_add(1);
            profile.payload_bytes = sender.bytes_sent();
        }
        let absolute_position = resume_offset
            .checked_add(sender.bytes_sent())
            .ok_or_else(|| context.error(TransferErrorKind::Internal("progress overflow")))?;
        on_progress(file_len, absolute_position);
    }
    let extra_buffer = buffer.get_mut(..1).ok_or_else(|| {
        context.error(TransferErrorKind::Internal("source probe buffer is empty"))
    })?;
    let eof_probe_started = payload_profile
        .as_ref()
        .map(crate::PayloadProfile::start_stage);
    let extra = timeout(CHUNK_TIMEOUT, source.read(extra_buffer))
        .await
        .map_err(|_source| context.error(TransferErrorKind::Timeout))?
        .map_err(|error| context.error(TransferErrorKind::SourceIo(error)))?;
    if let (Some(profile), Some(started)) = (&mut payload_profile, eof_probe_started) {
        profile.record_stage(PayloadStage::SourceRead, started);
    }
    if extra > 0 {
        return Err(context.error(TransferErrorKind::SourceIo(io::Error::new(
            io::ErrorKind::InvalidData,
            "source is longer than the advertised file length",
        ))));
    }
    transport.end_payload_observation();
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
        bytes_transferred: sender.bytes_sent(),
        file_size: file_len,
        chunk_size,
        handshake_seconds,
        payload_seconds,
        shutdown_seconds: shutdown_started.elapsed().as_secs_f64(),
        path_end,
        cleanup_issues,
        payload_profile,
    })
}
