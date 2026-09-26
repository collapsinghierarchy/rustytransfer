use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand, ValueEnum};
use serde::Serialize;
use std::{
    fs::OpenOptions,
    io::Write,
    path::PathBuf,
    process::Command as ProcessCommand,
    process::ExitCode,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::time::{sleep, timeout};

use rustytransfer::cli_ui::graphics::{UserCancelled, pick_file_tui};
use rustytransfer::cli_ui::graphics::{bytes_bar, spinner};
use rustytransfer::error::{ErrorCode, TransferError, TransferErrorKind};

use rustytransfer::rendezvous;
use rustytransfer::transfer::{self, TransferConfig, TransferTransport};
use rustytransfer::transport::errors::TransportError;
use rustytransfer::transport::iroh::{
    connect_answerer as connect_iroh_answerer, connect_offerer as connect_iroh_offerer,
};
use rustytransfer::transport::webrtc::{connect_answerer, connect_offerer};
use rustytransfer::transport::{DataTransport, PathObservation};

const NET_TIMEOUT: Duration = Duration::from_secs(90);

// plaintext chunk size (ciphertext will be +16 bytes for GCM tag)
const DEFAULT_CHUNK_SIZE: u32 = 8 * 1024;
const DEFAULT_IROH_CHUNK_SIZE: u32 = 256 * 1024;
#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
enum Transport {
    Webrtc,
    Iroh,
}

impl Transport {
    fn label(self) -> &'static str {
        match self {
            Self::Webrtc => "WebRTC",
            Self::Iroh => "Iroh",
        }
    }

    fn schema_name(self) -> &'static str {
        match self {
            Self::Webrtc => "webrtc",
            Self::Iroh => "iroh",
        }
    }

    fn default_chunk_size(self) -> u32 {
        match self {
            Self::Webrtc => DEFAULT_CHUNK_SIZE,
            Self::Iroh => DEFAULT_IROH_CHUNK_SIZE,
        }
    }
}

#[derive(Serialize)]
struct TransferMetric {
    schema_version: u32,
    run_id: String,
    commit: Option<String>,
    working_tree_dirty: Option<bool>,
    role: &'static str,
    transport: &'static str,
    path: &'static str,
    path_start: &'static str,
    path_end: &'static str,
    local_candidate_type: Option<String>,
    remote_candidate_type: Option<String>,
    size_bytes: u64,
    chunk_size: u32,
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
    source_sha256: Option<String>,
    received_sha256: Option<String>,
    success: bool,
    bytes_transferred: u64,
    completion_state: &'static str,
    cleanup_success: bool,
    phase: Option<String>,
    error_code: Option<String>,
}

struct TransferTiming {
    handshake_seconds: f64,
    payload_seconds: f64,
    shutdown_seconds: f64,
}

struct MetricPaths {
    start: PathObservation,
    end: PathObservation,
}

struct MetricOutcome {
    run_id: String,
    success: bool,
    bytes_transferred: u64,
    completion_state: &'static str,
    cleanup_success: bool,
    phase: Option<String>,
    error_code: Option<String>,
}

fn write_transfer_metric(
    role: &'static str,
    transport: &Transport,
    paths: MetricPaths,
    size_bytes: u64,
    chunk_size: u32,
    timing: TransferTiming,
    outcome: MetricOutcome,
) -> Result<()> {
    let Some(metrics_path) = std::env::var_os("RUSTYTRANSFER_METRICS_JSONL") else {
        return Ok(());
    };

    let commit = ProcessCommand::new("git")
        .args(["rev-parse", "HEAD"])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|commit| commit.trim().to_owned());
    let working_tree_dirty = ProcessCommand::new("git")
        .args(["status", "--porcelain"])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| !output.stdout.is_empty());
    // Clippy baseline: this performance metric intentionally uses f64 throughput values.
    let payload_mib = size_bytes as f64 / (1024.0 * 1024.0);
    let path = if paths.start.path == paths.end.path {
        paths.start.path
    } else if paths.start.path == "unknown" || paths.end.path == "unknown" {
        "unknown"
    } else {
        "mixed"
    };
    let wall_seconds = timing.handshake_seconds + timing.payload_seconds + timing.shutdown_seconds;
    let effective_mib_per_second = if wall_seconds > 0.0 {
        payload_mib / wall_seconds
    } else {
        0.0
    };
    let metric = TransferMetric {
        schema_version: 1,
        run_id: outcome.run_id,
        commit,
        working_tree_dirty,
        role,
        transport: transport.schema_name(),
        path,
        path_start: paths.start.path,
        path_end: paths.end.path,
        local_candidate_type: paths.start.local_candidate_type,
        remote_candidate_type: paths.start.remote_candidate_type,
        size_bytes,
        chunk_size,
        pipeline_depth: 1,
        handshake_seconds: timing.handshake_seconds,
        payload_seconds: timing.payload_seconds,
        shutdown_seconds: timing.shutdown_seconds,
        wall_seconds,
        effective_mib_per_second,
        sender_cpu_seconds: None,
        receiver_cpu_seconds: None,
        sender_max_rss_kib: None,
        receiver_max_rss_kib: None,
        source_sha256: None,
        received_sha256: None,
        success: outcome.success,
        bytes_transferred: outcome.bytes_transferred,
        completion_state: outcome.completion_state,
        cleanup_success: outcome.cleanup_success,
        phase: outcome.phase,
        error_code: outcome.error_code,
    };

    let mut output = OpenOptions::new()
        .create(true)
        .append(true)
        .open(metrics_path)
        .context("failed to open performance JSONL output")?;
    serde_json::to_writer(&mut output, &metric)
        .context("failed to serialize performance JSONL record")?;
    writeln!(output).context("failed to finish performance JSONL record")?;
    Ok(())
}

#[derive(Parser, Debug)]
#[command(name = "rustytransfer")]
#[command(about = "Encrypted file transfer over WebRTC or Iroh")]
struct Cli {
    #[arg(long, global = true, value_enum, default_value = "webrtc")]
    transport: Transport,

    #[arg(long, global = true, default_value_t = false)]
    verbose: bool,

    #[command(subcommand)]
    cmd: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Send a file. Prints a share code NNNN-ABCDE (digits = rendezvous, letters = password).
    Send {
        /// Optional override (must be 5 uppercase letters). If omitted, generated automatically.
        #[arg(long)]
        password: Option<String>,

        /// If omitted and --pick is not set, a TUI file picker opens.
        #[arg(long)]
        file: Option<PathBuf>,

        /// Force opening the TUI file picker (ignores --file)
        #[arg(long, default_value_t = false)]
        pick: bool,

        /// Plaintext chunk size for streaming SMT. Defaults to 8 KiB for WebRTC and 256 KiB for Iroh.
        #[arg(long, value_name = "BYTES")]
        chunk_size: Option<u32>,
    },

    /// Receive a file using a share code NNNN-ABCDE.
    Recv {
        /// Share code printed by sender (NNNN-ABCDE)
        #[arg(long)]
        code: String,

        #[arg(long)]
        out: PathBuf,
    },
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> ExitCode {
    let cli = match Cli::try_parse() {
        Ok(cli) => cli,
        Err(error) => {
            let code = error.exit_code();
            let _print_result = error.print();
            return ExitCode::from(u8::try_from(code).unwrap_or(2));
        }
    };
    let verbose = cli.verbose;
    let role = match &cli.cmd {
        Command::Send { .. } => "sender",
        Command::Recv { .. } => "receiver",
    };
    let mut run = RunContext::new(role, cli.transport);
    let result = match cli.cmd {
        Command::Send {
            password,
            file,
            pick,
            chunk_size,
        } => {
            Box::pin(send_cmd(
                cli.transport,
                password,
                file,
                pick,
                chunk_size,
                &mut run,
            ))
            .await
        }
        Command::Recv { code, out } => Box::pin(recv_cmd(cli.transport, code, out, &mut run)).await,
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let transfer_error = error.downcast_ref::<TransferError>();
            let unavailable_code = error
                .downcast_ref::<TransportError>()
                .is_some_and(|error| matches!(error, TransportError::RendezvousCodeUnavailable));
            if error.downcast_ref::<UserCancelled>().is_some()
                || transfer_error.is_some_and(|error| error.code() == ErrorCode::Cancelled)
            {
                let code = transfer_error
                    .map(|error| error.code().to_string())
                    .unwrap_or_else(|| "RTY-CANCELLED-001".to_owned());
                eprintln!("Cancelled. [{code}]");
                let _metric_result = write_transfer_metric(
                    run.role,
                    &run.transport,
                    MetricPaths {
                        start: unknown_path(),
                        end: unknown_path(),
                    },
                    run.size_bytes,
                    run.chunk_size,
                    TransferTiming {
                        handshake_seconds: run.started.elapsed().as_secs_f64(),
                        payload_seconds: 0.0,
                        shutdown_seconds: 0.0,
                    },
                    MetricOutcome {
                        run_id: run.run_id.clone(),
                        success: false,
                        bytes_transferred: transfer_error
                            .map_or(run.bytes_transferred, |error| error.bytes_transferred),
                        completion_state: transfer_error
                            .map_or("in_progress", |error| error.completion.as_str()),
                        cleanup_success: transfer_error
                            .is_none_or(|error| error.cleanup_issues.is_empty()),
                        phase: Some(
                            transfer_error
                                .map(|error| error.phase().to_string())
                                .unwrap_or_else(|| "cancelled".to_owned()),
                        ),
                        error_code: Some(code),
                    },
                );
                return ExitCode::from(130);
            }
            let code = transfer_error
                .map(|error| error.code().to_string())
                .unwrap_or_else(|| {
                    if unavailable_code {
                        "RTY-RENDEZVOUS-410".to_owned()
                    } else {
                        run.error_code.to_owned()
                    }
                });
            let exit_code = transfer_error
                .map(TransferError::exit_code)
                .unwrap_or(run.exit_code);
            let phase = transfer_error
                .map(|error| error.phase().to_string())
                .unwrap_or_else(|| run.phase.to_owned());
            let size_bytes = run.size_bytes;
            let message = transfer_error
                .map(|error| error.code().user_message())
                .unwrap_or_else(|| {
                    if unavailable_code {
                        "Der Share-Code ist abgelaufen, unbekannt oder bereits verwendet."
                    } else {
                        safe_phase_message(run.phase)
                    }
                });
            eprintln!("{message} [{code}]");
            if verbose {
                if let Some(error) = transfer_error {
                    let cause = match &error.kind {
                        TransferErrorKind::Config(_) => "configuration",
                        TransferErrorKind::Protocol(_) => "protocol",
                        TransferErrorKind::Transport(_) => "transport",
                        TransferErrorKind::Timeout => "timeout",
                        TransferErrorKind::SourceIo(_) => "source I/O",
                        TransferErrorKind::DestinationIo(_) => "destination I/O",
                        TransferErrorKind::CommitIo(_) => "commit I/O",
                        TransferErrorKind::Cancelled => "cancelled",
                        TransferErrorKind::Internal(_) => "internal",
                        _ => "unknown",
                    };
                    eprintln!(
                        "  phase={}, cause={}, bytes_transferred={}, completion={}, cleanup_issues={}",
                        error.phase(),
                        cause,
                        error.bytes_transferred,
                        error.completion.as_str(),
                        error.cleanup_issues.len()
                    );
                } else {
                    eprintln!("  phase={phase}, cause=setup");
                }
            }
            let _metric_result = write_transfer_metric(
                run.role,
                &run.transport,
                MetricPaths {
                    start: unknown_path(),
                    end: unknown_path(),
                },
                size_bytes,
                run.chunk_size,
                TransferTiming {
                    handshake_seconds: run.started.elapsed().as_secs_f64(),
                    payload_seconds: 0.0,
                    shutdown_seconds: 0.0,
                },
                MetricOutcome {
                    run_id: run.run_id.clone(),
                    success: false,
                    bytes_transferred: transfer_error
                        .map_or(run.bytes_transferred, |error| error.bytes_transferred),
                    completion_state: transfer_error
                        .map_or("in_progress", |error| error.completion.as_str()),
                    cleanup_success: transfer_error
                        .is_none_or(|error| error.cleanup_issues.is_empty()),
                    phase: Some(phase),
                    error_code: Some(code),
                },
            );
            ExitCode::from(exit_code)
        }
    }
}

struct RunContext {
    run_id: String,
    role: &'static str,
    transport: Transport,
    phase: &'static str,
    error_code: &'static str,
    exit_code: u8,
    size_bytes: u64,
    bytes_transferred: u64,
    chunk_size: u32,
    started: Instant,
}

impl RunContext {
    fn new(role: &'static str, transport: Transport) -> Self {
        Self {
            run_id: format!(
                "{:016x}{:016x}",
                rand::random::<u64>(),
                rand::random::<u64>()
            ),
            role,
            transport,
            phase: "startup",
            error_code: "RTY-INTERNAL-001",
            exit_code: 70,
            size_bytes: 0,
            bytes_transferred: 0,
            chunk_size: transport.default_chunk_size(),
            started: Instant::now(),
        }
    }

    fn set_phase(&mut self, phase: &'static str, error_code: &'static str) {
        self.phase = phase;
        self.error_code = error_code;
        self.exit_code = match phase {
            "validation" => 2,
            "rendezvous" | "redeem" => 3,
            "connect" => 4,
            "file" => 7,
            "transfer" => 6,
            _ => 70,
        };
    }
}

fn safe_phase_message(phase: &str) -> &'static str {
    let phase = phase.to_ascii_lowercase().replace([' ', '-'], "_");
    match phase.as_str() {
        "file_picker" => "File selection failed.",
        "file" | "output" | "filesystem" => "File access failed.",
        "rendezvous" | "redeem" => "Could not contact the rendezvous service.",
        "connect" | "connection" | "path" | "transport" => {
            "Could not establish the transfer connection."
        }
        "transfer" | "handshake" | "payload" | "shutdown" => "The transfer failed.",
        "validation" => "The supplied options are invalid.",
        "metrics" => "The transfer metrics could not be written.",
        _ => "The operation failed.",
    }
}

fn unknown_path() -> PathObservation {
    PathObservation {
        path: "unknown",
        local_candidate_type: None,
        remote_candidate_type: None,
    }
}

async fn connect_sender(transport: Transport, app_id: &str) -> Result<DataTransport> {
    match transport {
        Transport::Webrtc => Ok(DataTransport::WebRtc(
            Box::pin(connect_offerer(app_id)).await?,
        )),
        Transport::Iroh => Ok(DataTransport::Iroh(
            Box::pin(connect_iroh_offerer(app_id)).await?,
        )),
    }
}

async fn connect_receiver(transport: Transport, app_id: &str) -> Result<DataTransport> {
    match transport {
        Transport::Webrtc => Ok(DataTransport::WebRtc(
            Box::pin(connect_answerer(app_id)).await?,
        )),
        Transport::Iroh => Ok(DataTransport::Iroh(
            Box::pin(connect_iroh_answerer(app_id)).await?,
        )),
    }
}

async fn observe_start_path(
    transport: Transport,
    state: &DataTransport,
) -> Result<PathObservation> {
    let wait_for_direct = transport == Transport::Iroh
        && std::env::var("RUSTYTRANSFER_BENCH_WAIT_DIRECT").is_ok_and(|value| value == "1");
    let wait_for_relay = transport == Transport::Iroh
        && std::env::var("RUSTYTRANSFER_BENCH_RELAY_ONLY").is_ok_and(|value| value == "1");
    let expected_path = if wait_for_direct {
        Some("direct")
    } else if wait_for_relay {
        Some("relay")
    } else {
        None
    };
    let Some(expected_path) = expected_path else {
        return state.path_observation().await;
    };

    timeout(Duration::from_secs(30), async {
        loop {
            let path = state.path_observation().await?;
            if path.path == expected_path {
                return Ok(path);
            }
            sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .with_context(|| {
        format!("timed out waiting for an {expected_path} Iroh path before benchmark transfer")
    })?
}

async fn send_cmd(
    transport: Transport,
    password: Option<String>,
    file: Option<PathBuf>,
    pick: bool,
    chunk_size: Option<u32>,
    run: &mut RunContext,
) -> Result<()> {
    run.set_phase("validation", "RTY-INPUT-001");
    let chunk_size = chunk_size.unwrap_or_else(|| transport.default_chunk_size());
    run.chunk_size = chunk_size;
    if chunk_size == 0 {
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
    // password: 5 uppercase letters
    let pw5: String = match password {
        Some(p) => {
            if p.len() != 5 || !p.chars().all(|c| c.is_ascii_uppercase()) {
                bail!("--password must be exactly 5 uppercase letters (e.g. ABCDE)");
            }
            p
        }
        None => rendezvous::gen_password_5(),
    };

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
    // rendezvous: request code+app_id
    run.set_phase("rendezvous", "RTY-RENDEZVOUS-001");
    let spin = spinner("Requesting rendezvous code…");
    let rr = rendezvous::request_code().await?;
    spin.finish_and_clear();

    let share = rendezvous::format_share_code(&rr.code, &pw5)?;
    println!("Share this code: {share}");
    if let Some(exp) = rr.expires_at.as_ref() {
        println!("(expires at: {exp})");
    }

    println!("File: {} ({} bytes)", file.display(), file_len);
    println!("Waiting for receiver to join…");

    // connect
    run.set_phase("connect", "RTY-CONNECT-001");
    let spin = spinner(&format!("Connecting {}…", transport.label()));
    let mut st = Box::pin(timeout(NET_TIMEOUT, connect_sender(transport, &rr.app_id)))
        .await
        .context("offerer connect timeout")??;
    spin.finish_and_clear();
    let path = observe_start_path(transport, &st).await?;
    eprintln!("Selected {} data path: {}", transport.label(), path.path);
    let setup_handshake_seconds = handshake_started.elapsed().as_secs_f64();
    let mut progress = None;
    let progress_bytes = Arc::new(AtomicU64::new(0));
    let progress_counter = Arc::clone(&progress_bytes);
    run.set_phase("transfer", "RTY-PROTOCOL-001");
    let transfer_result = tokio::select! {
        result = transfer::send_file(
        &mut st,
        f,
        file_len,
        pw5.as_bytes(),
        TransferConfig {
            chunk_size: usize::try_from(chunk_size)
                .context("--chunk-size does not fit in usize")?,
        },
        |total, position| {
            progress_counter.store(position, Ordering::Relaxed);
            let bar = progress.get_or_insert_with(|| bytes_bar(total, "Sending"));
            bar.set_position(position);
        },
        ) => result,
        signal = tokio::signal::ctrl_c() => {
            if signal.is_ok() {
                run.bytes_transferred = progress_bytes.load(Ordering::Relaxed);
                let _abort_result = timeout(Duration::from_secs(5), st.abort()).await;
                return Err(UserCancelled.into());
            }
            return Err(anyhow::anyhow!("Ctrl+C handler failed"));
        }
    };
    let transfer_metrics =
        transfer_result.with_context(|| format!("failed to transfer {}", file.display()))?;
    if let Some(bar) = progress {
        bar.finish_and_clear();
    }
    let path_end = transfer_metrics.path_end.unwrap_or(path.clone());

    if write_transfer_metric(
        "sender",
        &transport,
        MetricPaths {
            start: path,
            end: path_end,
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

async fn recv_cmd(
    transport: Transport,
    code: String,
    out: PathBuf,
    run: &mut RunContext,
) -> Result<()> {
    run.set_phase("validation", "RTY-INPUT-001");
    let handshake_started = Instant::now();
    // parse NNNN-ABCDE
    let (code4, pw5) = rendezvous::parse_share_code(&code)?;

    // redeem NNNN -> app_id
    run.set_phase("redeem", "RTY-RENDEZVOUS-001");
    let spin = spinner("Redeeming rendezvous code…");
    let redeem = rendezvous::redeem(&code4).await?;
    spin.finish_and_clear();

    if let Some(exp) = redeem.expires_at.as_ref() {
        println!("Rendezvous redeemed (expires at: {exp})");
    }

    // connect
    run.set_phase("connect", "RTY-CONNECT-001");
    let spin = spinner(&format!("Connecting {}…", transport.label()));
    let mut st = Box::pin(timeout(
        NET_TIMEOUT,
        connect_receiver(transport, &redeem.app_id),
    ))
    .await
    .context("answerer connect timeout")??;
    spin.finish_and_clear();
    let path = observe_start_path(transport, &st).await?;
    eprintln!("Selected {} data path: {}", transport.label(), path.path);
    let setup_handshake_seconds = handshake_started.elapsed().as_secs_f64();
    let mut progress = None;
    let progress_bytes = Arc::new(AtomicU64::new(0));
    let progress_counter = Arc::clone(&progress_bytes);
    let expected_bytes = Arc::new(AtomicU64::new(0));
    let expected_counter = Arc::clone(&expected_bytes);
    run.set_phase("transfer", "RTY-PROTOCOL-001");
    let transfer_result = tokio::select! {
        result = transfer::receive_file(&mut st, pw5.as_bytes(), &out, |total, position| {
            expected_counter.store(total, Ordering::Relaxed);
            progress_counter.store(position, Ordering::Relaxed);
            let bar = progress.get_or_insert_with(|| bytes_bar(total, "Receiving"));
            bar.set_position(position);
        }) => result,
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
    let transfer_metrics =
        transfer_result.with_context(|| format!("failed to receive {}", out.display()))?;
    if let Some(bar) = progress {
        bar.finish_and_clear();
    }
    let path_end = transfer_metrics.path_end.unwrap_or(path.clone());

    if write_transfer_metric(
        "receiver",
        &transport,
        MetricPaths {
            start: path,
            end: path_end,
        },
        transfer_metrics.bytes_transferred,
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
mod tests {
    use super::{DEFAULT_CHUNK_SIZE, DEFAULT_IROH_CHUNK_SIZE, Transport};

    #[test]
    fn defaults_to_transport_tuned_chunk_sizes() {
        assert_eq!(Transport::Webrtc.default_chunk_size(), DEFAULT_CHUNK_SIZE);
        assert_eq!(
            Transport::Iroh.default_chunk_size(),
            DEFAULT_IROH_CHUNK_SIZE
        );
    }
}
