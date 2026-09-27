use crate::output::ResumeOutput;
use crate::*;

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
    receive_file_with_auth(
        transport,
        Authentication::Pake(password),
        output_path,
        on_progress,
    )
    .await
}

/// Run the receiver side of one encrypted transfer authenticated by a direct
/// shared token. The two PAKE messages are omitted; the token authenticates
/// the existing KEM exchange.
///
/// # Errors
///
/// Returns an error when authentication or decryption fails, the peer violates
/// the protocol, the output file cannot be written, or the transport cannot
/// complete the transfer and shutdown sequence.
pub async fn receive_file_direct<T, F>(
    transport: &mut T,
    token: &[u8; 16],
    output_path: &Path,
    on_progress: F,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    F: FnMut(u64, u64) + Send,
{
    receive_file_with_auth(
        transport,
        Authentication::Direct(token),
        output_path,
        on_progress,
    )
    .await
}

#[derive(Clone, Copy)]
enum Authentication<'a> {
    Pake(&'a [u8]),
    Direct(&'a [u8; 16]),
}

async fn receive_file_with_auth<T, F>(
    transport: &mut T,
    authentication: Authentication<'_>,
    output_path: &Path,
    on_progress: F,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    F: FnMut(u64, u64) + Send,
{
    let mut context = TransferContext::new();
    let mut resume_output = match ResumeOutput::open(output_path).await {
        Ok(output) => output,
        Err(error) => {
            let mut error = context.error(TransferErrorKind::DestinationIo(error));
            if let Err(issue) = abort_transport(transport).await {
                error.add_cleanup_issue(issue);
            }
            return Err(error);
        }
    };
    let result = receive_file_inner(
        transport,
        authentication,
        output_path,
        on_progress,
        &mut context,
        &mut resume_output,
    )
    .await;
    match result {
        Ok(metrics) => Ok(metrics),
        Err(mut error) => {
            if let Err(issue) = resume_output.flush().await {
                error.add_cleanup_issue(format!("partial output flush failed: {issue}"));
            }
            if let Err(issue) = resume_output.sync_data().await {
                error.add_cleanup_issue(format!("partial output sync failed: {issue}"));
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
    authentication: Authentication<'_>,
    output_path: &Path,
    mut on_progress: F,
    context: &mut TransferContext,
    resume_output: &mut ResumeOutput,
) -> std::result::Result<TransferMetrics, TransferError>
where
    T: TransferTransport,
    F: FnMut(u64, u64) + Send,
{
    let offer = resume_output.candidate();
    let mut receiver = match authentication {
        Authentication::Pake(password) => ReceiverFsm::new(password.to_vec(), offer),
        Authentication::Direct(token) => ReceiverFsm::new_direct(token, offer),
    };
    context.phase = match authentication {
        Authentication::Pake(_) => Phase::Pake,
        Authentication::Direct(_) => Phase::KemAuth,
    };
    context.completion = CompletionState::InProgress;
    let handshake_started = Instant::now();

    let auth_kem = match authentication {
        Authentication::Pake(_) => {
            let pake_start = required_output(
                "receiver PAKE_START",
                receiver
                    .step("PAKE_START", None)
                    .map_err(|error| context.protocol(error))?,
            )
            .map_err(|_source| {
                context.error(TransferErrorKind::Internal("receiver PAKE_START missing"))
            })?;
            send_with_timeout(transport, pake_start, HANDSHAKE_TIMEOUT, *context).await?;

            let pake_answer = receive_with_timeout(transport, HANDSHAKE_TIMEOUT, *context).await?;
            required_output(
                "receiver AUTH_KEM",
                receiver
                    .step("PAKE_ANSWER", Some(pake_answer))
                    .map_err(|error| context.protocol(error))?,
            )
            .map_err(|_source| {
                context.error(TransferErrorKind::Internal("receiver AUTH_KEM missing"))
            })?
        }
        Authentication::Direct(_) => required_output(
            "receiver AUTH_KEM",
            receiver
                .step("DIRECT_AUTH", None)
                .map_err(|error| context.protocol(error))?,
        )
        .map_err(|_source| {
            context.error(TransferErrorKind::Internal("receiver AUTH_KEM missing"))
        })?,
    };
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
    let resume_offset = receiver.resume_offset();
    let handshake_seconds = handshake_started.elapsed().as_secs_f64();

    resume_output
        .select(resume_offset, receiver.resume_digest())
        .await
        .map_err(|error| context.error(TransferErrorKind::DestinationIo(error)))?;
    context.phase = Phase::Payload;
    on_progress(total, resume_offset);
    let payload_started = Instant::now();
    while receiver.bytes_received() < receiver.remaining_len() {
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
        resume_output
            .write_all(&plaintext)
            .await
            .map_err(|error| context.error(TransferErrorKind::DestinationIo(error)))?;
        context.bytes_transferred = receiver.bytes_received();
        let absolute_position = resume_offset
            .checked_add(receiver.bytes_received())
            .ok_or_else(|| context.error(TransferErrorKind::Internal("progress overflow")))?;
        on_progress(total, absolute_position);
    }
    resume_output
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
        return Err(context.protocol(StepError::InvalidTransition(
            "unexpected final acknowledgement".into(),
        )));
    }
    context.completion = CompletionState::ProtocolConfirmed;
    if !matches!(receiver.state, State::Success(_)) {
        return Err(context.error(TransferErrorKind::Internal("receiver did not complete")));
    }
    let path_end = transport.observe_path().await.ok().flatten();
    context.phase = Phase::Finalize;
    resume_output
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
        bytes_transferred: receiver.bytes_received(),
        file_size: total,
        chunk_size: receiver.chunk_size(),
        handshake_seconds,
        payload_seconds,
        shutdown_seconds: shutdown_started.elapsed().as_secs_f64(),
        path_end,
        cleanup_issues,
    })
}
