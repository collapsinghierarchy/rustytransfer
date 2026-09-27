use super::*;

pub(super) fn write_transfer_metric(
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
