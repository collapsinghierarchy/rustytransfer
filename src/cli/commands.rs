use super::*;
use crate::transport::errors::TransportError;

pub(super) struct DirectSendOptions {
    pub(super) direct: bool,
    pub(super) identity_file: Option<PathBuf>,
}

enum TransferAuth {
    Pake(String),
    Direct([u8; 16]),
}

#[derive(Clone)]
enum SenderReconnect {
    Pake {
        app_id: String,
        transport: Transport,
    },
    Direct {
        identity_file: PathBuf,
        sender_id: String,
        token: [u8; 16],
    },
}

#[derive(Clone)]
enum ReceiverReconnect {
    Pake {
        app_id: String,
        transport: Transport,
    },
    Direct {
        sender_id: String,
        token: [u8; 16],
    },
}

const MAX_TRANSFER_ATTEMPTS: usize = 4;
const RECONNECT_BACKOFFS: [Duration; 3] = [
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(4),
];

#[derive(Debug)]
struct PermanentReconnectError(&'static str);

impl std::fmt::Display for PermanentReconnectError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for PermanentReconnectError {}

fn reconnect_backoff(attempt: usize) -> Duration {
    RECONNECT_BACKOFFS
        .get(attempt.saturating_sub(2))
        .copied()
        .unwrap_or(Duration::from_secs(4))
}

fn can_retry_transfer(error: &TransferError) -> bool {
    error.retry_advice() == crate::error::RetryAdvice::NewSession
        && error.code() != ErrorCode::Cancelled
        && matches!(
            error.completion,
            crate::error::CompletionState::NotStarted
                | crate::error::CompletionState::InProgress
                | crate::error::CompletionState::DataCompleteUnconfirmed
        )
}

fn retryable_reconnect_error(plan_is_pake: bool, error: &anyhow::Error) -> bool {
    let permanent = error.chain().any(|cause| {
        cause.downcast_ref::<PermanentReconnectError>().is_some()
            || cause.downcast_ref::<TransportError>().is_some_and(|cause| {
                matches!(
                    cause,
                    TransportError::InvalidWebSocketMessage(_)
                        | TransportError::InvalidWebSocketFrame(_)
                        | TransportError::PeerIdentityMismatch
                )
            })
    });
    if permanent {
        return false;
    }

    if plan_is_pake {
        // PAKE reconnect setup only performs signaling and transport setup. Its
        // permanent failures are malformed signaling frames; the PAKE itself
        // runs after a session has been established and is classified separately.
        return true;
    }

    let io_errors: Vec<_> = error
        .chain()
        .filter_map(|cause| cause.downcast_ref::<std::io::Error>())
        .collect();
    if !io_errors.is_empty() {
        return io_errors.iter().any(|cause| {
            matches!(
                cause.kind(),
                std::io::ErrorKind::ConnectionAborted
                    | std::io::ErrorKind::ConnectionRefused
                    | std::io::ErrorKind::ConnectionReset
                    | std::io::ErrorKind::HostUnreachable
                    | std::io::ErrorKind::Interrupted
                    | std::io::ErrorKind::NetworkUnreachable
                    | std::io::ErrorKind::NotConnected
                    | std::io::ErrorKind::TimedOut
                    | std::io::ErrorKind::UnexpectedEof
                    | std::io::ErrorKind::WouldBlock
            )
        });
    }

    error.chain().any(|cause| {
        cause.downcast_ref::<TransportError>().is_some_and(|cause| {
            matches!(
                cause,
                TransportError::WebSocketClosed | TransportError::IrohEndpointNotOnline(_)
            )
        }) || cause
            .downcast_ref::<tokio::time::error::Elapsed>()
            .is_some()
    }) || !plan_is_pake
}

async fn wait_before_reconnect(delay: Duration) -> Result<()> {
    tokio::select! {
        () = sleep(delay) => Ok(()),
        signal = tokio::signal::ctrl_c() => {
            match signal {
                Ok(()) => Err(UserCancelled.into()),
                Err(error) => Err(anyhow::anyhow!("Ctrl+C handler failed: {error}")),
            }
        }
    }
}

async fn reconnect_or_cancel<F, T>(future: F) -> Result<T>
where
    F: std::future::Future<Output = Result<T>>,
{
    tokio::select! {
        result = future => result,
        signal = tokio::signal::ctrl_c() => {
            match signal {
                Ok(()) => Err(UserCancelled.into()),
                Err(error) => Err(anyhow::anyhow!("Ctrl+C handler failed: {error}")),
            }
        }
    }
}

async fn open_direct_sender(
    identity_file: Option<PathBuf>,
    file: &std::path::Path,
    file_len: u64,
) -> Result<(DataTransport, [u8; 16], SenderReconnect)> {
    let identity_file = match identity_file {
        Some(path) => path,
        None => default_identity_path()?,
    };
    let endpoint = bind_direct_sender(&identity_file).await?;
    let invite = DirectInvite::generate(endpoint.id().to_string());
    println!("Direct invite: {invite}");
    println!("File: {} ({} bytes)", file.display(), file_len);
    println!("Waiting for receiver to join...");
    std::io::stdout()
        .flush()
        .context("failed to print direct invite")?;
    let token = invite.token;
    let reconnect = SenderReconnect::Direct {
        identity_file,
        sender_id: invite.sender_id,
        token,
    };
    let st = timeout(NET_TIMEOUT, accept_direct(endpoint, &token))
        .await
        .context("direct receiver connect timeout")??;
    Ok((DataTransport::Iroh(st), token, reconnect))
}

async fn reconnect_sender(plan: &SenderReconnect) -> Result<DataTransport> {
    match plan {
        SenderReconnect::Pake { app_id, transport } => {
            timeout(NET_TIMEOUT, connect_sender(*transport, app_id, || {}))
                .await
                .context("offerer reconnect timeout")?
        }
        SenderReconnect::Direct {
            identity_file,
            sender_id,
            token,
        } => {
            let identity = tokio::fs::read(identity_file).await.with_context(|| {
                format!("failed to read Iroh identity: {}", identity_file.display())
            })?;
            if identity.len() != 32 {
                return Err(PermanentReconnectError(
                    "Iroh identity file must contain exactly 32 bytes",
                )
                .into());
            }
            drop(identity);
            let endpoint = bind_direct_sender(identity_file).await?;
            if endpoint.id().to_string() != *sender_id {
                return Err(TransportError::PeerIdentityMismatch.into());
            }
            let state = timeout(NET_TIMEOUT, accept_direct(endpoint, token))
                .await
                .context("direct receiver reconnect timeout")??;
            Ok(DataTransport::Iroh(state))
        }
    }
}

async fn reconnect_receiver(plan: &ReceiverReconnect) -> Result<DataTransport> {
    match plan {
        ReceiverReconnect::Pake { app_id, transport } => {
            timeout(NET_TIMEOUT, connect_receiver(*transport, app_id))
                .await
                .context("answerer reconnect timeout")?
        }
        ReceiverReconnect::Direct { sender_id, token } => {
            let state = timeout(NET_TIMEOUT, connect_direct(sender_id, token))
                .await
                .context("direct Iroh reconnect timeout")??;
            Ok(DataTransport::Iroh(state))
        }
    }
}

pub(super) async fn send_cmd(
    requested_transport: Option<Transport>,
    direct_options: DirectSendOptions,
    password: Option<String>,
    file: Option<PathBuf>,
    pick: bool,
    chunk_size: Option<u32>,
    run: &mut RunContext,
) -> Result<()> {
    let DirectSendOptions {
        direct,
        identity_file,
    } = direct_options;
    run.set_phase("validation", "RTY-INPUT-001");
    if direct && requested_transport == Some(Transport::Webrtc) {
        bail!("--direct requires Iroh; remove --transport webrtc");
    }
    if direct && password.is_some() {
        bail!("--password applies to share codes and cannot be used with --direct");
    }
    if chunk_size == Some(0) {
        bail!("--chunk-size must be greater than zero");
    }
    // Pick file first (before networking)
    run.set_phase("file_picker", "RTY-INTERNAL-001");
    let file: PathBuf = match (pick, file) {
        (false, Some(p)) => p,
        _ => tokio::task::spawn_blocking(|| pick_file_tui(None))
            .await
            .context("file picker task join failed")??,
    };

    run.set_phase("validation", "RTY-INPUT-001");
    if let Some(p) = password.as_ref()
        && (p.len() != 5 || !p.chars().all(|c| c.is_ascii_uppercase()))
    {
        bail!("--password must be exactly 5 uppercase letters (e.g. ABCDE)");
    }

    run.set_phase("file", "RTY-IO-001");
    // open file + get length
    let f = tokio::fs::File::open(&file)
        .await
        .with_context(|| format!("failed to open file: {}", file.display()))?;
    let meta = f
        .metadata()
        .await
        .with_context(|| format!("failed to stat file: {}", file.display()))?;
    let file_len = meta.len();
    run.size_bytes = file_len;

    let handshake_started = Instant::now();
    // Requesting a code checks the backend operation needed by this flow.
    // A failed request never produces a displayed PAKE code.
    let rendezvous = if direct {
        None
    } else {
        run.set_phase("rendezvous", "RTY-RENDEZVOUS-001");
        let spin = spinner("Requesting rendezvous code...");
        let response = rendezvous::request_code().await;
        spin.finish_and_clear();
        match response {
            Ok(response) => Some(response),
            Err(error) => {
                eprintln!("Rendezvous backend unavailable ({error}); switching to direct Iroh.");
                if password.is_some() {
                    eprintln!("The requested PAKE password is unused in direct mode.");
                }
                None
            }
        }
    };
    run.set_phase("connect", "RTY-CONNECT-001");
    let code_connection = if let Some(rr) = rendezvous {
        let transport = requested_transport.unwrap_or(Transport::Webrtc);
        let pw5 = password.clone().unwrap_or_else(rendezvous::gen_password_5);
        let share = rendezvous::format_share_code(&rr.code, &pw5)?;
        let spin = spinner(&format!("Connecting {}...", transport.label()));
        let mut backend_ready = false;
        let on_ready = || {
            backend_ready = true;
            println!("Share this code: {share}");
            if let Some(exp) = rr.expires_at.as_ref() {
                println!("(expires at: {exp})");
            }
            println!("File: {} ({} bytes)", file.display(), file_len);
            println!("Waiting for receiver to join...");
        };
        let connection = Box::pin(timeout(
            NET_TIMEOUT,
            connect_sender(transport, &rr.app_id, on_ready),
        ))
        .await
        .context("offerer connect timeout")
        .and_then(|value| value);
        spin.finish_and_clear();
        match connection {
            Ok(st) => Some((st, pw5, transport, rr.app_id)),
            Err(error) if !backend_ready => {
                eprintln!("Backend signaling unavailable ({error}); switching to direct Iroh.");
                if password.is_some() {
                    eprintln!("The requested PAKE password is unused in direct mode.");
                }
                None
            }
            Err(error) => return Err(error),
        }
    } else {
        None
    };
    let (mut st, auth, transport, reconnect) =
        if let Some((st, pw5, transport, app_id)) = code_connection {
            (
                st,
                TransferAuth::Pake(pw5),
                transport,
                SenderReconnect::Pake { app_id, transport },
            )
        } else {
            run.transport = Transport::Iroh;
            run.chunk_size = chunk_size.unwrap_or_else(|| Transport::Iroh.default_chunk_size());
            let (st, token, reconnect) = open_direct_sender(identity_file, &file, file_len).await?;
            (st, TransferAuth::Direct(token), Transport::Iroh, reconnect)
        };
    run.transport = transport;
    let chunk_size = chunk_size.unwrap_or_else(|| transport.default_chunk_size());
    run.chunk_size = chunk_size;
    let mut path = observe_start_path(transport, &st).await?;
    eprintln!("Selected {} data path: {}", transport.label(), path.path);
    let mut setup_handshake_seconds = handshake_started.elapsed().as_secs_f64();
    let progress = std::sync::Mutex::new(None);
    let progress_bytes = Arc::new(AtomicU64::new(0));
    let progress_counter = Arc::clone(&progress_bytes);
    run.set_phase("transfer", "RTY-PROTOCOL-001");
    let config = TransferConfig {
        chunk_size: usize::try_from(chunk_size).context("--chunk-size does not fit in usize")?,
    };
    let mut on_progress = |total, position| {
        progress_counter.store(position, Ordering::Relaxed);
        if let Ok(mut progress) = progress.lock() {
            let bar = progress.get_or_insert_with(|| bytes_bar(total, "Sending"));
            bar.set_position(position);
        }
    };
    let mut source = Some(f);
    let mut attempts = 1;
    let transfer_metrics = loop {
        let source = match source.take() {
            Some(source) => source,
            None => {
                run.set_phase("file", "RTY-IO-001");
                let source = tokio::fs::File::open(&file)
                    .await
                    .with_context(|| format!("failed to reopen file: {}", file.display()))?;
                run.set_phase("transfer", "RTY-PROTOCOL-001");
                source
            }
        };
        let transfer_result = tokio::select! {
            result = async {
                match &auth {
                    TransferAuth::Direct(token) => {
                        transfer::send_file_direct(&mut st, source, file_len, token, config, &mut on_progress).await
                    }
                    TransferAuth::Pake(password) => {
                        transfer::send_file(&mut st, source, file_len, password.as_bytes(), config, &mut on_progress).await
                    }
                }
            } => result,
            signal = tokio::signal::ctrl_c() => {
                if signal.is_ok() {
                    run.bytes_transferred = progress_bytes.load(Ordering::Relaxed);
                    let _abort_result = timeout(Duration::from_secs(5), st.abort()).await;
                    return Err(UserCancelled.into());
                }
                return Err(anyhow::anyhow!("Ctrl+C handler failed"));
            }
        };

        match transfer_result {
            Ok(metrics) => break metrics,
            Err(error) if can_retry_transfer(&error) && attempts < MAX_TRANSFER_ATTEMPTS => {
                let mut next_attempt = attempts.saturating_add(1).min(MAX_TRANSFER_ATTEMPTS);
                eprintln!(
                    "Connection interrupted after {} bytes; reconnecting (attempt {next_attempt}/{MAX_TRANSFER_ATTEMPTS}). The receiver will verify its saved prefix and resume if it matches.",
                    error.bytes_transferred
                );
                progress_bytes.store(0, Ordering::Relaxed);
                run.bytes_transferred = 0;
                if let Ok(progress) = progress.lock()
                    && let Some(bar) = progress.as_ref()
                {
                    bar.set_position(0);
                }

                loop {
                    let delay = reconnect_backoff(next_attempt);
                    eprintln!("Reconnecting in {}s...", delay.as_secs());
                    wait_before_reconnect(delay).await?;
                    run.set_phase("connect", "RTY-CONNECT-001");
                    let started = Instant::now();
                    let connected = reconnect_or_cancel(reconnect_sender(&reconnect)).await;
                    setup_handshake_seconds += started.elapsed().as_secs_f64();
                    match connected {
                        Ok(transport_state) => {
                            st = transport_state;
                            attempts = next_attempt;
                            match observe_start_path(transport, &st).await {
                                Ok(observed_path) => path = observed_path,
                                Err(error) => eprintln!(
                                    "Reconnected; continuing despite path observation failure: {error:#}"
                                ),
                            }
                            eprintln!("Reconnected; negotiating the saved prefix.");
                            run.set_phase("transfer", "RTY-PROTOCOL-001");
                            break;
                        }
                        Err(error)
                            if retryable_reconnect_error(
                                matches!(&reconnect, SenderReconnect::Pake { .. }),
                                &error,
                            ) && next_attempt < MAX_TRANSFER_ATTEMPTS =>
                        {
                            eprintln!("Reconnect attempt failed; {error:#}");
                            next_attempt =
                                next_attempt.saturating_add(1).min(MAX_TRANSFER_ATTEMPTS);
                        }
                        Err(error) => {
                            return Err(error).context("failed to reconnect transfer");
                        }
                    }
                }
            }
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("failed to transfer {}", file.display()));
            }
        }
    };
    if let Ok(mut progress) = progress.lock() {
        drop(progress.take());
    }
    let path_end = transfer_metrics.path_end.unwrap_or(path.clone());
    let path_evidence = st.take_path_evidence().await;

    if write_transfer_metric(
        "sender",
        &transport,
        MetricPaths {
            start: path,
            end: path_end,
            path_evidence,
        },
        file_len,
        transfer_metrics.chunk_size,
        TransferTiming {
            handshake_seconds: setup_handshake_seconds + transfer_metrics.handshake_seconds,
            payload_seconds: transfer_metrics.payload_seconds,
            shutdown_seconds: transfer_metrics.shutdown_seconds,
        },
        MetricOutcome {
            run_id: run.run_id.clone(),
            success: true,
            bytes_transferred: transfer_metrics.bytes_transferred,
            completion_state: "committed",
            cleanup_success: transfer_metrics.cleanup_issues.is_empty(),
            phase: None,
            error_code: None,
        },
    )
    .is_err()
    {
        eprintln!("Transfer completed; metrics were not saved. [RTY-METRICS-WRITE]");
    }
    if !transfer_metrics.cleanup_issues.is_empty() {
        eprintln!("Transfer completed; transport cleanup reported a problem.");
    }
    println!("Sent encrypted {}", file.display());
    Ok(())
}

pub(super) async fn recv_cmd(
    requested_transport: Option<Transport>,
    code: Option<String>,
    invite: Option<String>,
    out: PathBuf,
    run: &mut RunContext,
) -> Result<()> {
    run.set_phase("validation", "RTY-INPUT-001");
    if code.is_some() == invite.is_some() {
        bail!("provide exactly one of --code or --invite");
    }
    if invite.is_some() && requested_transport == Some(Transport::Webrtc) {
        bail!("--invite requires Iroh; remove --transport webrtc");
    }
    let handshake_started = Instant::now();
    let (mut st, transport, auth, reconnect) = if let Some(invite) = invite {
        let invite = DirectInvite::parse(&invite)?;
        run.set_phase("connect", "RTY-CONNECT-001");
        let spin = spinner("Connecting Iroh...");
        let st = timeout(
            NET_TIMEOUT,
            connect_direct(&invite.sender_id, &invite.token),
        )
        .await
        .context("direct Iroh connect timeout")??;
        spin.finish_and_clear();
        (
            DataTransport::Iroh(st),
            Transport::Iroh,
            TransferAuth::Direct(invite.token),
            ReceiverReconnect::Direct {
                sender_id: invite.sender_id,
                token: invite.token,
            },
        )
    } else {
        let code = code.ok_or_else(|| anyhow::anyhow!("missing share code"))?;
        let (code4, pw5) = rendezvous::parse_share_code(&code)?;
        run.set_phase("redeem", "RTY-RENDEZVOUS-001");
        let spin = spinner("Redeeming rendezvous code...");
        let redeem = rendezvous::redeem(&code4).await?;
        spin.finish_and_clear();
        if let Some(exp) = redeem.expires_at.as_ref() {
            println!("Rendezvous redeemed (expires at: {exp})");
        }
        let transport = requested_transport.unwrap_or(Transport::Webrtc);
        run.set_phase("connect", "RTY-CONNECT-001");
        let spin = spinner(&format!("Connecting {}...", transport.label()));
        let st = Box::pin(timeout(
            NET_TIMEOUT,
            connect_receiver(transport, &redeem.app_id),
        ))
        .await
        .context("answerer connect timeout")??;
        spin.finish_and_clear();
        (
            st,
            transport,
            TransferAuth::Pake(pw5),
            ReceiverReconnect::Pake {
                app_id: redeem.app_id,
                transport,
            },
        )
    };
    run.transport = transport;
    let mut path = observe_start_path(transport, &st).await?;
    eprintln!("Selected {} data path: {}", transport.label(), path.path);
    let mut setup_handshake_seconds = handshake_started.elapsed().as_secs_f64();
    let progress = std::sync::Mutex::new(None);
    let progress_bytes = Arc::new(AtomicU64::new(0));
    let progress_counter = Arc::clone(&progress_bytes);
    let expected_bytes = Arc::new(AtomicU64::new(0));
    let expected_counter = Arc::clone(&expected_bytes);
    run.set_phase("transfer", "RTY-PROTOCOL-001");
    let mut on_progress = |total, position| {
        expected_counter.store(total, Ordering::Relaxed);
        progress_counter.store(position, Ordering::Relaxed);
        if let Ok(mut progress) = progress.lock() {
            let bar = progress.get_or_insert_with(|| bytes_bar(total, "Receiving"));
            bar.set_position(position);
        }
    };
    let mut attempts = 1;
    let transfer_metrics = loop {
        let transfer_result = tokio::select! {
            result = async {
                match &auth {
                    TransferAuth::Direct(token) => {
                        transfer::receive_file_direct(&mut st, token, &out, &mut on_progress).await
                    }
                    TransferAuth::Pake(password) => {
                        transfer::receive_file(&mut st, password.as_bytes(), &out, &mut on_progress).await
                    }
                }
            } => result,
            signal = tokio::signal::ctrl_c() => {
                if signal.is_ok() {
                    run.size_bytes = expected_bytes.load(Ordering::Relaxed);
                    run.bytes_transferred = progress_bytes.load(Ordering::Relaxed);
                    let _abort_result = timeout(Duration::from_secs(5), st.abort()).await;
                    return Err(UserCancelled.into());
                }
                return Err(anyhow::anyhow!("Ctrl+C handler failed"));
            }
        };

        match transfer_result {
            Ok(metrics) => break metrics,
            Err(error) if can_retry_transfer(&error) && attempts < MAX_TRANSFER_ATTEMPTS => {
                let mut next_attempt = attempts.saturating_add(1).min(MAX_TRANSFER_ATTEMPTS);
                eprintln!(
                    "Connection interrupted after {} bytes; reconnecting (attempt {next_attempt}/{MAX_TRANSFER_ATTEMPTS}). The saved prefix will be verified before resume.",
                    error.bytes_transferred
                );
                progress_bytes.store(0, Ordering::Relaxed);
                expected_bytes.store(0, Ordering::Relaxed);
                run.bytes_transferred = 0;
                run.size_bytes = 0;
                if let Ok(progress) = progress.lock()
                    && let Some(bar) = progress.as_ref()
                {
                    bar.set_position(0);
                }

                loop {
                    let delay = reconnect_backoff(next_attempt);
                    eprintln!("Reconnecting in {}s...", delay.as_secs());
                    wait_before_reconnect(delay).await?;
                    run.set_phase("connect", "RTY-CONNECT-001");
                    let started = Instant::now();
                    let connected = reconnect_or_cancel(reconnect_receiver(&reconnect)).await;
                    setup_handshake_seconds += started.elapsed().as_secs_f64();
                    match connected {
                        Ok(transport_state) => {
                            st = transport_state;
                            attempts = next_attempt;
                            match observe_start_path(transport, &st).await {
                                Ok(observed_path) => path = observed_path,
                                Err(error) => eprintln!(
                                    "Reconnected; continuing despite path observation failure: {error:#}"
                                ),
                            }
                            eprintln!("Reconnected; negotiating the saved prefix.");
                            run.set_phase("transfer", "RTY-PROTOCOL-001");
                            break;
                        }
                        Err(error)
                            if retryable_reconnect_error(
                                matches!(&reconnect, ReceiverReconnect::Pake { .. }),
                                &error,
                            ) && next_attempt < MAX_TRANSFER_ATTEMPTS =>
                        {
                            eprintln!("Reconnect attempt failed; {error:#}");
                            next_attempt =
                                next_attempt.saturating_add(1).min(MAX_TRANSFER_ATTEMPTS);
                        }
                        Err(error) => {
                            return Err(error).context("failed to reconnect transfer");
                        }
                    }
                }
            }
            Err(error) => {
                return Err(error).with_context(|| format!("failed to receive {}", out.display()));
            }
        }
    };
    if let Ok(mut progress) = progress.lock() {
        drop(progress.take());
    }
    let path_end = transfer_metrics.path_end.unwrap_or(path.clone());
    let path_evidence = st.take_path_evidence().await;

    if write_transfer_metric(
        "receiver",
        &transport,
        MetricPaths {
            start: path,
            end: path_end,
            path_evidence,
        },
        transfer_metrics.file_size,
        transfer_metrics.chunk_size,
        TransferTiming {
            handshake_seconds: setup_handshake_seconds + transfer_metrics.handshake_seconds,
            payload_seconds: transfer_metrics.payload_seconds,
            shutdown_seconds: transfer_metrics.shutdown_seconds,
        },
        MetricOutcome {
            run_id: run.run_id.clone(),
            success: true,
            bytes_transferred: transfer_metrics.bytes_transferred,
            completion_state: "committed",
            cleanup_success: transfer_metrics.cleanup_issues.is_empty(),
            phase: None,
            error_code: None,
        },
    )
    .is_err()
    {
        eprintln!("Transfer completed; metrics were not saved. [RTY-METRICS-WRITE]");
    }
    if !transfer_metrics.cleanup_issues.is_empty() {
        eprintln!("Transfer completed; transport cleanup reported a problem.");
    }
    println!("Wrote {}", out.display());
    Ok(())
}

#[cfg(test)]
mod reconnect_tests {
    use super::*;
    use crate::error::{CompletionState, Phase, TransferErrorKind};

    #[test]
    fn retries_only_unconfirmed_transient_transfer_failures() {
        let interrupted = TransferError::new(
            TransferErrorKind::Timeout,
            Phase::Payload,
            128,
            CompletionState::InProgress,
        );
        let protocol = TransferError::new(
            TransferErrorKind::Config("invalid test configuration"),
            Phase::Input,
            0,
            CompletionState::NotStarted,
        );
        let confirmed = TransferError::new(
            TransferErrorKind::Timeout,
            Phase::Shutdown,
            128,
            CompletionState::ProtocolConfirmed,
        );

        assert!(can_retry_transfer(&interrupted));
        assert!(!can_retry_transfer(&protocol));
        assert!(!can_retry_transfer(&confirmed));
    }

    #[test]
    fn reconnect_backoff_is_bounded_to_three_retries() {
        assert_eq!(reconnect_backoff(2), Duration::from_secs(1));
        assert_eq!(reconnect_backoff(3), Duration::from_secs(2));
        assert_eq!(reconnect_backoff(4), Duration::from_secs(4));
        assert_eq!(reconnect_backoff(5), Duration::from_secs(4));
    }
}
