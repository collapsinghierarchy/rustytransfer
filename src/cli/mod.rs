use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand, ValueEnum};
use serde::Serialize;
mod args;
mod commands;
mod contacts;
mod invite;
use args::{Cli, Command, ContactCommand};
use commands::{DirectSendOptions, recv_cmd, send_cmd};
use invite::DirectInvite;
mod metrics;
use metrics::write_transfer_metric;
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

use crate::cli_ui::graphics::{UserCancelled, pick_file_tui};
use crate::cli_ui::graphics::{bytes_bar, spinner};
use crate::error::{ErrorCode, TransferError, TransferErrorKind};

use crate::rendezvous;
use crate::rendezvous::error::RendezvousError;
use crate::transfer::{self, TransferConfig, TransferTransport};
use crate::transport::iroh::{
    accept_direct, bind_direct_sender, connect_answerer as connect_iroh_answerer, connect_direct,
    connect_offerer_with_ready as connect_iroh_offerer_with_ready, default_identity_path,
};
use crate::transport::webrtc::{connect_answerer, connect_offerer_with_ready};
use crate::transport::{DataTransport, PathObservation};

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

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
pub async fn main() -> ExitCode {
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
        Command::Contacts { .. } => "contacts",
    };
    let starts_direct = matches!(&cli.cmd, Command::Send { direct: true, .. })
        || matches!(
            &cli.cmd,
            Command::Recv {
                invite: Some(_),
                ..
            }
        );
    let initial_transport = if starts_direct {
        Transport::Iroh
    } else {
        cli.transport.unwrap_or(Transport::Webrtc)
    };
    let mut run = RunContext::new(role, initial_transport);
    let result = match cli.cmd {
        Command::Send {
            direct,
            identity_file,
            password,
            file,
            pick,
            chunk_size,
        } => {
            Box::pin(send_cmd(
                cli.transport,
                DirectSendOptions {
                    direct,
                    identity_file,
                },
                password,
                file,
                pick,
                chunk_size,
                &mut run,
            ))
            .await
        }
        Command::Recv { code, invite, out } => {
            Box::pin(recv_cmd(cli.transport, code, invite, out, &mut run)).await
        }
        Command::Contacts { command } => contacts::run(command),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            if run.role == "contacts" {
                eprintln!("{error}");
                return ExitCode::from(2);
            }
            let transfer_error = error.downcast_ref::<TransferError>();
            let unavailable_code = error
                .downcast_ref::<RendezvousError>()
                .is_some_and(|error| matches!(error, RendezvousError::CodeUnavailable));
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

async fn connect_sender<F: FnOnce()>(
    transport: Transport,
    app_id: &str,
    on_ready: F,
) -> Result<DataTransport> {
    match transport {
        Transport::Webrtc => Ok(DataTransport::WebRtc(
            Box::pin(connect_offerer_with_ready(app_id, on_ready)).await?,
        )),
        Transport::Iroh => Ok(DataTransport::Iroh(
            Box::pin(connect_iroh_offerer_with_ready(app_id, on_ready)).await?,
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
