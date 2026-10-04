#!/usr/bin/env python3
"""Measure Croc and Rustytransfer between WSL and one Oracle host."""

import argparse
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from run_croc_baseline import CapturedOutput, git_output, parse_time, selected_auto_path, time_command
from summarize import payload_profile_rejection_reason


TIME_FORMAT = '{"user_cpu_seconds":%U,"system_cpu_seconds":%S,"max_rss_kib":%M}'
SHARE_CODE = re.compile(r"Share this code:\s*([0-9]{4}-[A-Z]{5})")
DIRECT_INVITE = re.compile(r"Direct invite:\s*(rt1:[^\s]+)")
DIRECT_INVITE_TOKEN = re.compile(r"rt1:[^\s]+")
REMOTE_REDACT_SCRIPT = (
    "from pathlib import Path; import re, sys; "
    "path = Path(sys.argv[1]); "
    "text = path.read_text(encoding='utf-8', errors='replace'); "
    "path.write_text(re.sub(r'rt1:\\S+', '<redacted>', text), encoding='utf-8')"
)
REMOTE_REDACT_SECRET_SCRIPT = (
    "from pathlib import Path; import sys; "
    "path = Path(sys.argv[1]); "
    "text = path.read_text(encoding='utf-8', errors='replace'); "
    "path.write_text(text.replace(sys.argv[2], '<redacted>'), encoding='utf-8')"
)
PAYLOAD_MARKERS = ("payload_start", "sender_payload_end", "receiver_payload_end")
METRIC_FIELDS = (
    "schema_version",
    "commit",
    "working_tree_dirty",
    "role",
    "transport",
    "transport_mode",
    "build_id",
    "sender_binary_sha256",
    "receiver_binary_sha256",
    "direction",
    "host_pair",
    "storage_class",
    "pairing_mode",
    "profile_mode",
    "source_staging",
    "path",
    "path_start",
    "path_end",
    "path_evidence",
    "payload_profile",
    "stream_window_bytes",
    "experimental_protocol_version",
    "parallel_streams",
    "parallel_connections",
    "connection_evidence",
    "payload_key_count",
    "kem_sessions",
    "direct_route_verified_both",
    "local_candidate_type",
    "remote_candidate_type",
    "size_bytes",
    "chunk_size",
    "pipeline_depth",
    "handshake_seconds",
    "payload_seconds",
    "shutdown_seconds",
    "wall_seconds",
    "effective_mib_per_second",
    "sender_cpu_seconds",
    "receiver_cpu_seconds",
    "sender_max_rss_kib",
    "receiver_max_rss_kib",
    "source_sha256",
    "received_sha256",
    "success",
    "bytes_transferred",
    "run_index",
    "warmup",
    "measurement_scope",
)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def ssh_command(args, command):
    return [
        args.ssh,
        "-i",
        str(args.ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        f"{args.user}@{args.host}",
        command,
    ]


def remote(args, command, *, capture_output=True):
    result = subprocess.run(
        ssh_command(args, command),
        check=False,
        capture_output=capture_output,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() if result.stderr else ""
        raise RuntimeError(f"remote command failed ({result.returncode}): {detail}")
    return result.stdout.strip() if result.stdout is not None else ""


def remote_sha256(args, path):
    output = remote(args, f"sha256sum -- {remote_quote(path)}")
    digest = output.split(maxsplit=1)[0] if output else ""
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise RuntimeError(f"remote sha256sum returned an invalid digest for {path}")
    return digest.lower()


def rust_provenance(args, size_mib=64):
    local_hash = args.local_rusty_sha256
    remote_hash = args.remote_rusty_sha256
    if args.direction == "wsl-to-oracle":
        sender_hash, receiver_hash = local_hash, remote_hash
    else:
        sender_hash, receiver_hash = remote_hash, local_hash
    return {
        "build_id": args.build_id,
        "sender_binary_sha256": sender_hash,
        "receiver_binary_sha256": receiver_hash,
        "direction": args.direction,
        "host_pair": f"WSL/{args.user}@{args.host}",
        "storage_class": args.storage_class,
        "profile_mode": "payload-profile" if payload_profile_enabled(args) else "standard",
        "stream_window_bytes": getattr(args, "stream_window_bytes", None),
        "source_staging": "pre-staged" if remote_staged_input(args, args.direction == "oracle-to-wsl", size_mib) else "per-trial",
    }


def croc_provenance(args, size_mib=64):
    local_hash = args.local_croc_sha256
    remote_hash = args.remote_croc_sha256
    if args.direction == "wsl-to-oracle":
        sender_hash, receiver_hash = local_hash, remote_hash
    else:
        sender_hash, receiver_hash = remote_hash, local_hash
    return {
        "build_id": f"croc-{getattr(args, 'croc_version', '11.5.3')}",
        "sender_binary_sha256": sender_hash,
        "receiver_binary_sha256": receiver_hash,
        "direction": args.direction,
        "host_pair": f"WSL/{args.user}@{args.host}",
        "storage_class": args.storage_class,
        "pairing_mode": "croc-secret",
        "profile_mode": "standard",
        "stream_window_bytes": None,
        "source_staging": "pre-staged" if remote_staged_input(args, args.direction == "oracle-to-wsl", size_mib) else "per-trial",
    }


def remote_quote(value):
    return shlex.quote(str(value))


def payload_profile_enabled(args):
    return bool(getattr(args, "payload_profile", False))


def configure_profile_env(environment, args):
    environment.pop("RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE", None)
    if payload_profile_enabled(args):
        environment["RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE"] = "1"


def configure_stream_window_env(environment, args):
    environment.pop("RUSTYTRANSFER_BENCH_STREAM_WINDOW_BYTES", None)
    stream_window_bytes = getattr(args, "stream_window_bytes", None)
    if stream_window_bytes is not None:
        environment["RUSTYTRANSFER_BENCH_STREAM_WINDOW_BYTES"] = str(stream_window_bytes)


def append_rows(path, rows):
    with path.open("a", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, sort_keys=True) + "\n")


def redact(path, secret):
    if not secret or not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(text.replace(secret, "<redacted>"), encoding="utf-8")


def redact_direct_invites(path):
    if not path or not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(DIRECT_INVITE_TOKEN.sub("<redacted>", text), encoding="utf-8")


def redact_remote_direct_invites(args, path):
    remote(
        args,
        f"if test -f {remote_quote(path)}; then "
        f"python3 -c {remote_quote(REMOTE_REDACT_SCRIPT)} {remote_quote(path)}; fi",
    )


def wait_for_share_code(process, log_path, timeout_seconds=120):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        match = SHARE_CODE.search(text)
        if match:
            return match.group(1)
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"Rustytransfer sender exited before publishing a code ({code})")
        time.sleep(0.05)
    raise RuntimeError("Rustytransfer did not publish a share code within 120 seconds")


def wait_for_direct_invite(process, log_path, timeout_seconds=120):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        match = DIRECT_INVITE.search(text)
        if match:
            return match.group(1)
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"Rustytransfer sender exited before publishing an invite ({code})")
        time.sleep(0.05)
    raise RuntimeError("Rustytransfer did not publish a direct invite within 120 seconds")


def wait_for_remote_authorization(args, process, remote_log, mode, timeout_seconds=120):
    deadline = time.monotonic() + timeout_seconds
    pattern = DIRECT_INVITE if mode == "invite" else SHARE_CODE
    description = "direct invite" if mode == "invite" else "share code"
    while time.monotonic() < deadline:
        text = remote(args, f"if test -f {remote_quote(remote_log)}; then cat -- {remote_quote(remote_log)}; fi")
        match = pattern.search(text)
        if match:
            return match.group(1)
        code = process.poll()
        if code is not None:
            raise RuntimeError(
                f"Oracle Rustytransfer sender exited before publishing a {description} ({code})"
            )
        time.sleep(0.1)
    raise RuntimeError(f"Oracle Rustytransfer did not publish a {description} within 120 seconds")


def wait_for_pair(sender, receiver, timeout_seconds, sender_log, receiver_log):
    deadline = time.monotonic() + timeout_seconds
    while sender.poll() is None or receiver.poll() is None:
        for process, other, log_path in (
            (sender, receiver, sender_log),
            (receiver, sender, receiver_log),
        ):
            code = process.poll()
            if code is not None and code != 0:
                if other.poll() is None:
                    other.kill()
                raise RuntimeError(f"endpoint exited with status {code}; see {log_path}")
        if time.monotonic() > deadline:
            if sender.poll() is None:
                sender.kill()
            if receiver.poll() is None:
                receiver.kill()
            raise RuntimeError(
                f"transfer timed out; see logs {sender_log} and {receiver_log}"
            )
        time.sleep(0.05)
    if sender.returncode != 0 or receiver.returncode != 0:
        raise RuntimeError(
            f"endpoint failure ({sender.returncode}, {receiver.returncode}); "
            f"see logs {sender_log} and {receiver_log}"
        )


def read_time(path):
    return parse_time(path)


def read_remote_json(args, path):
    text = remote(args, f"cat -- {remote_quote(path)}")
    return json.loads(text)


def verify_remote_file(args, path, expected_size, expected_hash):
    output = remote(
        args,
        f"stat -c %s -- {remote_quote(path)} && sha256sum -- {remote_quote(path)}",
    )
    lines = output.splitlines()
    if len(lines) < 2:
        raise RuntimeError(f"remote size/hash check returned incomplete output for {path}")
    received_size = int(lines[0])
    received_hash = lines[1].split()[0]
    if received_size != expected_size or received_hash != expected_hash:
        raise RuntimeError(
            f"remote file mismatch at {path}: size={received_size}, sha256={received_hash}"
        )
    return received_hash


def verify_local_file(path, expected_size, expected_hash):
    received_size = path.stat().st_size
    received_hash = sha256_file(path)
    if received_size != expected_size or received_hash != expected_hash:
        raise RuntimeError(
            f"local file mismatch at {path}: size={received_size}, sha256={received_hash}"
        )
    return received_hash


def copy_to_remote(args, source, destination):
    command = [
        args.scp,
        "-i",
        str(args.ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        str(source),
        f"{args.user}@{args.host}:{destination}",
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def remote_staged_input(args, reverse, size_mib):
    if not reverse:
        return None
    return getattr(args, f"remote_input_{size_mib}", None)


def prepare_remote_source(args, source, run_source, size_mib, expected_size, source_hash):
    staged_source = remote_staged_input(args, True, size_mib)
    if staged_source:
        verify_remote_file(args, staged_source, expected_size, source_hash)
        return staged_source, False
    copy_to_remote(args, source, run_source)
    verify_remote_file(args, run_source, expected_size, source_hash)
    return run_source, True


def reverse_trial_cleanup_files(remote_source, source_is_temporary, metrics_path, time_path, log_path, pid_path):
    files = [metrics_path, time_path, log_path, pid_path]
    if source_is_temporary:
        files.insert(0, remote_source)
    return tuple(files)


def terminate_remote_process_group(args, pid_path, run_dir):
    command = (
        f"for attempt in 1 2 3 4 5 6 7 8 9 10; do "
        f"test -s {remote_quote(pid_path)} && break; sleep 0.1; done; "
        f"pid=$(cat -- {remote_quote(pid_path)} 2>/dev/null) || exit 0; "
        "case \"$pid\" in ''|*[!0-9]*) exit 0;; esac; "
        "pgid=$(ps -o pgid= -p \"$pid\" 2>/dev/null | tr -d ' '); "
        f"cwd=$(readlink -f -- /proc/\"$pid\"/cwd 2>/dev/null); "
        f"trial_dir=$(readlink -f -- {remote_quote(run_dir)} 2>/dev/null) || exit 0; "
        "test \"$pgid\" = \"$pid\" && test \"$cwd\" = \"$trial_dir\" || exit 0; "
        "if kill -0 -- \"-$pid\" 2>/dev/null; then "
        "kill -TERM -- \"-$pid\" 2>/dev/null || true; sleep 1; "
        "kill -KILL -- \"-$pid\" 2>/dev/null || true; fi"
    )
    remote(args, command)


def remote_process_command(
    args,
    run_dir,
    endpoint_command,
    metrics_path,
    time_path,
    log_path,
    pid_path,
):
    path_env = (
        "RUSTYTRANSFER_BENCH_WAIT_DIRECT"
        if args.rusty_path == "direct"
        else "RUSTYTRANSFER_BENCH_RELAY_ONLY"
    )
    command = " ".join(remote_quote(item) for item in endpoint_command)
    profile_env = (
        "RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE=1 "
        if payload_profile_enabled(args)
        else ""
    )
    stream_window_env = (
        f"RUSTYTRANSFER_BENCH_STREAM_WINDOW_BYTES={args.stream_window_bytes} "
        if getattr(args, "stream_window_bytes", None) is not None
        else ""
    )
    env_command = (
        f"env -u RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE "
        f"-u RUSTYTRANSFER_BENCH_STREAM_WINDOW_BYTES {profile_env}{stream_window_env}"
        f"{path_env}=1 RUSTYTRANSFER_BENCH_PATH_EVIDENCE=1 "
        f"RUSTYTRANSFER_METRICS_JSONL={remote_quote(metrics_path)} "
        f"/usr/bin/time -f {remote_quote(TIME_FORMAT)} "
        f"-o {remote_quote(time_path)} {command}"
    )
    child_script = (
        f"printf '%s\\n' \"$$\" > {remote_quote(pid_path)} || exit 1; "
        f"exec {env_command}"
    )
    return (
        f"cd -- {remote_quote(run_dir)} || exit 1; "
        f"setsid --wait /bin/sh -c {remote_quote(child_script)} "
        f">{remote_quote(log_path)} 2>&1 < /dev/null & "
        "setsid_pid=$!; wait \"$setsid_pid\""
    )


def remote_sender_command(args, run_dir, source_path, metrics_path, time_path, log_path, pid_path):
    command = rusty_sender_argv(args, args.remote_rusty, source_path)
    return remote_process_command(
        args, run_dir, command, metrics_path, time_path, log_path, pid_path
    )


def rusty_sender_argv(args, executable, source_path):
    command = [str(executable), "--transport", "iroh", "send"]
    if args.rusty_auth == "invite":
        command.append("--direct")
    if getattr(args, "chunk_size", None) is not None:
        command.extend(["--chunk-size", str(args.chunk_size)])
    if getattr(args, "experimental_streams", None) is not None:
        command.extend(["--streams", str(args.experimental_streams)])
    if getattr(args, "experimental_connections", None) is not None:
        command.extend(["--connections", str(args.experimental_connections)])
    command.extend(["--file", source_path])
    if args.rusty_auth == "pake":
        command.extend(["--password", "ABCDE"])
    return command


def remote_receiver_command(
    args, run_dir, output_path, authorization, metrics_path, time_path, log_path, pid_path
):
    auth_flag = "--invite" if args.rusty_auth == "invite" else "--code"
    command = [
        args.remote_rusty,
        "--transport",
        "iroh",
        "recv",
        auth_flag,
        authorization,
        "--out",
        output_path,
    ]
    return remote_process_command(
        args, run_dir, command, metrics_path, time_path, log_path, pid_path
    )


def cleanup_remote(args, run_dir, files, directories=()):
    root = Path(args.remote_root)
    paths = [Path(path) for path in (*files, *directories, run_dir)]
    for path in paths:
        try:
            path.relative_to(root)
        except ValueError as error:
            raise RuntimeError(f"refusing to remove path outside benchmark root: {path}") from error
    command = "rm -- " + " ".join(remote_quote(path) for path in files)
    if directories:
        command += " && rmdir -- " + " ".join(remote_quote(path) for path in directories)
    command += f" && rmdir -- {remote_quote(run_dir)}"
    remote(args, command)


def resource_values(sender_time, receiver_time):
    sender_cpu = sender_time["user_cpu_seconds"] + sender_time["system_cpu_seconds"]
    receiver_cpu = receiver_time["user_cpu_seconds"] + receiver_time["system_cpu_seconds"]
    return {
        "sender_cpu_seconds": sender_cpu,
        "receiver_cpu_seconds": receiver_cpu,
        "sender_max_rss_kib": sender_time["max_rss_kib"],
        "receiver_max_rss_kib": receiver_time["max_rss_kib"],
    }


def verified_direct_evidence(metric):
    evidence = metric.get("path_evidence")
    return (
        isinstance(evidence, dict)
        and evidence.get("classification") == "direct"
        and evidence.get("verified") is True
        and evidence.get("lagged") is False
        and evidence.get("missing_path_stats") is False
        and evidence.get("relay_selected") is False
        and evidence.get("relay_stream_tx") == 0
        and evidence.get("relay_stream_rx") == 0
        and (evidence.get("direct_stream_tx", 0) + evidence.get("direct_stream_rx", 0)) > 0
    )


def verified_direct_connections(metric, expected_count):
    connections = metric.get("connection_evidence")
    if not isinstance(connections, list) or len(connections) != expected_count:
        return False
    indices = []
    stable_ids = []
    for connection in connections:
        if not isinstance(connection, dict):
            return False
        indices.append(connection.get("connection_index"))
        stable_ids.append(connection.get("stable_id"))
        if connection.get("path") != "direct" or not verified_direct_evidence(
            {"path_evidence": connection.get("path_evidence")}
        ):
            return False
        if not all(
            isinstance(connection.get(field), str) and connection[field]
            for field in ("local_endpoint_id", "remote_endpoint_id")
        ):
            return False
        if connection.get("path_start") != "direct" or connection.get("path_end") != "direct":
            return False
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (*stable_ids, *indices)
    ):
        return False
    if sorted(indices) != list(range(expected_count)) or len(set(stable_ids)) != expected_count:
        return False
    ordered = sorted(connections, key=lambda item: item["connection_index"])
    primary = ordered[0]
    if metric.get("path_evidence") != primary.get("path_evidence"):
        return False
    if metric.get("path_start") != primary.get("path_start") or metric.get("path_end") != primary.get("path_end"):
        return False
    local_id = primary["local_endpoint_id"]
    remote_id = primary["remote_endpoint_id"]
    return local_id != remote_id and all(
        item["local_endpoint_id"] == local_id and item["remote_endpoint_id"] == remote_id
        for item in ordered
    )


def validate_experimental_connection_pair(sender, receiver, expected_count):
    if not verified_direct_connections(sender, expected_count) or not verified_direct_connections(
        receiver, expected_count
    ):
        return False
    sender_connections = sorted(sender["connection_evidence"], key=lambda item: item["connection_index"])
    receiver_connections = sorted(receiver["connection_evidence"], key=lambda item: item["connection_index"])
    sender_local = sender_connections[0]["local_endpoint_id"]
    sender_remote = sender_connections[0]["remote_endpoint_id"]
    receiver_local = receiver_connections[0]["local_endpoint_id"]
    receiver_remote = receiver_connections[0]["remote_endpoint_id"]
    for sent, received in zip(sender_connections, receiver_connections, strict=True):
        if (
            sent["local_endpoint_id"] != received["remote_endpoint_id"]
            or sent["remote_endpoint_id"] != received["local_endpoint_id"]
            or sent["local_endpoint_id"] != sender_local
            or sent["remote_endpoint_id"] != sender_remote
            or received["local_endpoint_id"] != receiver_local
            or received["remote_endpoint_id"] != receiver_remote
        ):
            return False
    if sender.get("path") != "direct" or receiver.get("path") != "direct":
        return False
    return True


def ensure_direct_route_evidence(rows, requested_path):
    if requested_path == "direct" and any(
        row.get("direct_route_verified_both") is not True for row in rows
    ):
        raise RuntimeError(
            "direct-only sweep stopped: payload STREAM-frame evidence was not verified "
            "as direct by both endpoints; diagnostic rows were retained"
        )


def append_and_validate_rust_rows(raw_path, rows, requested_path, actual_path):
    append_rows(raw_path, rows)
    if any(
        row.get("profile_mode") == "payload-profile"
        and payload_profile_rejection_reason(
            row.get("payload_profile"),
            row.get("bytes_transferred"),
            row.get("payload_seconds"),
        )
        is not None
        for row in rows
    ):
        raise RuntimeError(
            "payload profile was requested but an endpoint reported invalid or missing "
            "payload_profile; "
            "diagnostic rows were retained"
        )
    ensure_direct_route_evidence(rows, requested_path)
    if actual_path != requested_path:
        raise RuntimeError(
            f"Rustytransfer selected {actual_path}; requested benchmark path was {requested_path}"
        )


def rust_rows(
    sender_metrics,
    receiver_metrics,
    source_hash,
    received_hash,
    expected_size,
    wall,
    sender_time,
    receiver_time,
    run_index,
    warmup,
    provenance,
    experimental_streams=None,
    experimental_connections=None,
):
    endpoint_metrics = (("sender", sender_metrics), ("receiver", receiver_metrics))
    for role, metric in endpoint_metrics:
        if metric.get("success") is not True:
            raise RuntimeError(f"Rustytransfer {role} metric did not report success")
        if metric.get("size_bytes") != expected_size:
            raise RuntimeError(
                f"Rustytransfer {role} reported {metric.get('size_bytes')} bytes; "
                f"expected {expected_size}"
            )
        if metric.get("bytes_transferred") != expected_size:
            raise RuntimeError(
                f"Rustytransfer {role} reported {metric.get('bytes_transferred')} transferred bytes; "
                f"expected {expected_size}"
            )
        requested_streams = experimental_streams
        if requested_streams is not None:
            requested_connections = experimental_connections or 1
            expected_experimental = {
                "experimental_protocol_version": "shared-key-parallel/2",
                "parallel_streams": requested_streams,
                "parallel_connections": requested_connections,
                "payload_key_count": 1,
                "kem_sessions": 1,
            }
            for field, expected in expected_experimental.items():
                if metric.get(field) != expected:
                    raise RuntimeError(
                        f"Rustytransfer {role} reported {field}={metric.get(field)!r}; "
                        f"expected {expected!r} for the requested shared-key experiment"
                    )
    experimental_evidence_matches = (
        validate_experimental_connection_pair(
            sender_metrics, receiver_metrics, experimental_connections or 1
        )
        if experimental_streams is not None
        else True
    )
    sender_chunk_size = sender_metrics.get("chunk_size")
    receiver_chunk_size = receiver_metrics.get("chunk_size")
    if (
        not isinstance(sender_chunk_size, int)
        or isinstance(sender_chunk_size, bool)
        or sender_chunk_size <= 0
        or sender_chunk_size != receiver_chunk_size
    ):
        raise RuntimeError(
            "Rustytransfer sender and receiver reported invalid or different chunk sizes: "
            f"{sender_chunk_size} / {receiver_chunk_size}"
        )
    if wall <= 0:
        raise RuntimeError("Rustytransfer process wall time must be positive")

    resources = resource_values(sender_time, receiver_time)
    effective_mib_per_second = (sender_metrics["size_bytes"] / (1024 * 1024)) / wall
    direct_route_verified_both = (
        verified_direct_connections(sender_metrics, experimental_connections or 1)
        and verified_direct_connections(receiver_metrics, experimental_connections or 1)
        and experimental_evidence_matches
        if experimental_streams is not None
        else verified_direct_evidence(sender_metrics) and verified_direct_evidence(receiver_metrics)
    )
    rows = []
    for role, metric in (("sender", sender_metrics), ("receiver", receiver_metrics)):
        row = {key: metric[key] for key in METRIC_FIELDS if key in metric}
        row.update(
            {
                "schema_version": 1,
                "commit": git_output("rev-parse", "HEAD"),
                "working_tree_dirty": bool(git_output("status", "--porcelain")),
                "role": role,
                "transport": "iroh",
                **provenance,
                "wall_seconds": wall,
                "effective_mib_per_second": effective_mib_per_second,
                "source_sha256": source_hash,
                "received_sha256": received_hash,
                "success": True,
                "direct_route_verified_both": direct_route_verified_both,
                "run_index": run_index,
                "warmup": warmup,
                "measurement_scope": (
                    "separate sender and receiver processes "
                    f"({'WSL to Oracle' if provenance['direction'] == 'wsl-to-oracle' else 'Oracle to WSL'})"
                ),
                **resources,
            }
        )
        rows.append({key: row[key] for key in METRIC_FIELDS if key in row})
    return rows


def prepare_local_logs(log_root, run_name):
    run_logs = log_root / run_name
    run_logs.mkdir(parents=True, exist_ok=False)
    return run_logs


def run_rusty(args, source, size_mib, expected_size, source_hash, log_root, run_index, warmup):
    if args.direction == "oracle-to-wsl":
        return run_rusty_reverse(
            args, source, size_mib, expected_size, source_hash, log_root, run_index, warmup
        )

    tag = "warmup" if warmup else str(run_index)
    run_name = f"rustytransfer-wsl-to-oracle-{size_mib}mib-{tag}"
    logs = prepare_local_logs(log_root, run_name)
    run_dir = f"{args.remote_root}/{run_name}"
    remote(args, f"mkdir -- {remote_quote(run_dir)}")
    output_path = f"{run_dir}/received.bin"
    remote_metrics_path = f"{run_dir}/receiver.jsonl"
    remote_time_path = f"{run_dir}/receiver.time.json"
    remote_receiver_log = f"{run_dir}/receiver.log"
    remote_receiver_pid = f"{run_dir}/receiver.pid"
    sender_metrics_path = logs / "sender.jsonl"
    sender_time_path = logs / "sender.time.json"
    sender_log = logs / "sender.log"
    receiver_log = logs / "receiver.log"
    sender_env = os.environ.copy()
    configure_profile_env(sender_env, args)
    configure_stream_window_env(sender_env, args)
    sender_env["RUSTYTRANSFER_METRICS_JSONL"] = str(sender_metrics_path)
    path_env = (
        "RUSTYTRANSFER_BENCH_WAIT_DIRECT"
        if args.rusty_path == "direct"
        else "RUSTYTRANSFER_BENCH_RELAY_ONLY"
    )
    sender_env[path_env] = "1"
    sender_env["RUSTYTRANSFER_BENCH_PATH_EVIDENCE"] = "1"
    sender_command = rusty_sender_argv(args, args.rusty_sender, str(source))

    started = time.monotonic()
    sender, sender_output = time_command(
        sender_command, sender_time_path, sender_log, sender_env
    )
    receiver = None
    receiver_output = None
    authorization = None
    try:
        authorization = (
            wait_for_direct_invite(sender, sender_log)
            if args.rusty_auth == "invite"
            else wait_for_share_code(sender, sender_log)
        )
        remote_receiver = remote_receiver_command(
            args,
            run_dir,
            output_path,
            authorization,
            remote_metrics_path,
            remote_time_path,
            remote_receiver_log,
            remote_receiver_pid,
        )
        receiver = subprocess.Popen(
            ssh_command(args, remote_receiver),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        receiver_output = CapturedOutput(receiver.stdout, receiver_log)
        wait_for_pair(sender, receiver, args.timeout, sender_log, receiver_log)
        wall = time.monotonic() - started
    except Exception as error:
        with sender_log.open("a", encoding="utf-8") as log:
            log.write(f"\nBenchmark runner failure: {error}\n")
        raise
    finally:
        cleanup_errors = []
        if sender.poll() is None:
            sender.kill()
        if receiver is not None and receiver.poll() is None:
            receiver.kill()
        sender_output.wait()
        if receiver_output is not None:
            receiver_output.wait()
        try:
            terminate_remote_process_group(args, remote_receiver_pid, run_dir)
        except Exception as error:
            cleanup_errors.append(f"failed to terminate Oracle receiver process group: {error}")
        try:
            remote_text = remote(args, f"cat -- {remote_quote(remote_receiver_log)}")
            receiver_log.write_text(remote_text + "\n", encoding="utf-8")
        except Exception as error:
            with receiver_log.open("a", encoding="utf-8") as log:
                log.write(f"\nCould not retrieve Oracle receiver log: {error}\n")
        try:
            redact_remote_direct_invites(args, remote_receiver_log)
        except Exception as error:
            cleanup_errors.append(f"failed to redact Oracle receiver log: {error}")
        if cleanup_errors:
            with receiver_log.open("a", encoding="utf-8") as log:
                log.write("\nRemote cleanup errors: " + "; ".join(cleanup_errors) + "\n")
        redact(sender_log, authorization)
        redact(receiver_log, authorization)
        redact_direct_invites(sender_log)
        redact_direct_invites(receiver_log)

    if cleanup_errors:
        raise RuntimeError("; ".join(cleanup_errors))

    sender_time = read_time(sender_time_path)
    receiver_time = read_remote_json(args, remote_time_path)
    received_hash = verify_remote_file(args, output_path, expected_size, source_hash)
    sender_metric = json.loads(sender_metrics_path.read_text(encoding="utf-8").splitlines()[0])
    receiver_metric = read_remote_json(args, remote_metrics_path)
    if (
        sender_metric["path"] != receiver_metric["path"]
        and getattr(args, "experimental_streams", None) is None
    ):
        raise RuntimeError(
            f"sender/receiver selected different paths: "
            f"{sender_metric['path']} / {receiver_metric['path']}"
        )
    path = sender_metric["path"]
    if path not in ("direct", "relay") and getattr(args, "experimental_streams", None) is None:
        raise RuntimeError(f"Rustytransfer selected an unusable comparison path: {path}")
    rows = rust_rows(
        sender_metric,
        receiver_metric,
        source_hash,
        received_hash,
        expected_size,
        wall,
        sender_time,
        receiver_time,
        run_index,
        warmup,
        {**rust_provenance(args, size_mib), "pairing_mode": args.rusty_auth},
        getattr(args, "experimental_streams", None),
        getattr(args, "experimental_connections", None),
    )
    cleanup_remote(
        args,
        run_dir,
        (
            output_path,
            remote_metrics_path,
            remote_time_path,
            remote_receiver_log,
            remote_receiver_pid,
        ),
    )
    return rows, path


def run_rusty_reverse(args, source, size_mib, expected_size, source_hash, log_root, run_index, warmup):
    tag = "warmup" if warmup else str(run_index)
    run_name = f"rustytransfer-oracle-to-wsl-{size_mib}mib-{tag}"
    logs = prepare_local_logs(log_root, run_name)
    run_dir = f"{args.remote_root}/{run_name}"
    remote(args, f"mkdir -- {remote_quote(run_dir)}")
    run_source = f"{run_dir}/source.bin"
    remote_metrics_path = f"{run_dir}/sender.jsonl"
    remote_time_path = f"{run_dir}/sender.time.json"
    remote_log = f"{run_dir}/sender.log"
    remote_pid = f"{run_dir}/sender.pid"
    remote_source, source_is_temporary = prepare_remote_source(
        args, source, run_source, size_mib, expected_size, source_hash
    )

    receiver_output_path = logs / "received.bin"
    receiver_metrics_path = logs / "receiver.jsonl"
    receiver_time_path = logs / "receiver.time.json"
    sender_log = logs / "sender.log"
    receiver_log = logs / "receiver.log"
    path_env = (
        "RUSTYTRANSFER_BENCH_WAIT_DIRECT"
        if args.rusty_path == "direct"
        else "RUSTYTRANSFER_BENCH_RELAY_ONLY"
    )

    started = time.monotonic()
    sender = subprocess.Popen(
        ssh_command(
            args,
            remote_sender_command(
                args,
                run_dir,
                remote_source,
                remote_metrics_path,
                remote_time_path,
                remote_log,
                remote_pid,
            ),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    sender_output = CapturedOutput(sender.stdout, sender_log)
    receiver = None
    receiver_output = None
    authorization = None
    cleanup_errors = []
    try:
        authorization = wait_for_remote_authorization(
            args, sender, remote_log, args.rusty_auth
        )
        auth_flag = "--invite" if args.rusty_auth == "invite" else "--code"
        receiver_env = os.environ.copy()
        configure_profile_env(receiver_env, args)
        configure_stream_window_env(receiver_env, args)
        receiver_env[path_env] = "1"
        receiver_env["RUSTYTRANSFER_BENCH_PATH_EVIDENCE"] = "1"
        receiver_env["RUSTYTRANSFER_METRICS_JSONL"] = str(receiver_metrics_path)
        receiver_command = [
            str(args.rusty_sender),
            "--transport",
            "iroh",
            "recv",
            auth_flag,
            authorization,
            "--out",
            str(receiver_output_path),
        ]
        receiver, receiver_output = time_command(
            receiver_command, receiver_time_path, receiver_log, receiver_env
        )
        wait_for_pair(sender, receiver, args.timeout, sender_log, receiver_log)
        wall = time.monotonic() - started
    except Exception as error:
        with receiver_log.open("a", encoding="utf-8") as log:
            log.write(f"\nBenchmark runner failure: {error}\n")
        raise
    finally:
        if sender.poll() is None:
            sender.kill()
        if receiver is not None and receiver.poll() is None:
            receiver.kill()
        sender_output.wait()
        if receiver_output is not None:
            receiver_output.wait()
        try:
            terminate_remote_process_group(args, remote_pid, run_dir)
        except Exception as error:
            cleanup_errors.append(f"failed to terminate Oracle sender process group: {error}")
        try:
            remote_text = remote(args, f"cat -- {remote_quote(remote_log)}")
            sender_log.write_text(remote_text + "\n", encoding="utf-8")
        except Exception as error:
            with sender_log.open("a", encoding="utf-8") as log:
                log.write(f"\nCould not retrieve Oracle sender log: {error}\n")
        try:
            redact_remote_direct_invites(args, remote_log)
        except Exception as error:
            cleanup_errors.append(f"failed to redact Oracle sender log: {error}")
        if cleanup_errors:
            with sender_log.open("a", encoding="utf-8") as log:
                log.write("\nRemote cleanup errors: " + "; ".join(cleanup_errors) + "\n")
        redact(sender_log, authorization)
        redact(receiver_log, authorization)
        redact_direct_invites(sender_log)
        redact_direct_invites(receiver_log)

    if cleanup_errors:
        raise RuntimeError("; ".join(cleanup_errors))

    sender_time = read_remote_json(args, remote_time_path)
    receiver_time = read_time(receiver_time_path)
    received_hash = verify_local_file(
        receiver_output_path, expected_size, source_hash
    )
    sender_metric = read_remote_json(args, remote_metrics_path)
    receiver_metric = json.loads(receiver_metrics_path.read_text(encoding="utf-8").splitlines()[0])
    if (
        sender_metric["path"] != receiver_metric["path"]
        and getattr(args, "experimental_streams", None) is None
    ):
        raise RuntimeError(
            f"sender/receiver selected different paths: "
            f"{sender_metric['path']} / {receiver_metric['path']}"
        )
    path = sender_metric["path"]
    if path not in ("direct", "relay") and getattr(args, "experimental_streams", None) is None:
        raise RuntimeError(f"Rustytransfer selected an unusable comparison path: {path}")
    rows = rust_rows(
        sender_metric,
        receiver_metric,
        source_hash,
        received_hash,
        expected_size,
        wall,
        sender_time,
        receiver_time,
        run_index,
        warmup,
        {**rust_provenance(args, size_mib), "pairing_mode": args.rusty_auth},
        getattr(args, "experimental_streams", None),
        getattr(args, "experimental_connections", None),
    )
    cleanup_remote(
        args,
        run_dir,
        reverse_trial_cleanup_files(
            remote_source,
            source_is_temporary,
            remote_metrics_path,
            remote_time_path,
            remote_log,
            remote_pid,
        ),
    )
    receiver_output_path.unlink()
    return rows, path


def remote_croc_sender_argv(args, source_path):
    return [
        args.remote_croc,
        "--local",
        "--no-compress",
        "--debug",
        "--disable-clipboard",
        "--ignore-stdin",
        "send",
        "--transport",
        "auto",
        "--port",
        "9009",
        "--transfers",
        "4",
        source_path,
    ]


def remote_croc_sender_command(args, run_dir, source_path, secret, time_path, log_path, pid_path, peer_path):
    endpoint_command = remote_croc_sender_argv(args, source_path)
    env_command = (
        "env -u CROC_RELAY -u CROC_RELAY6 -u CROC_SECRET -u CROC_PASS "
        "-u SOCKS5_PROXY -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY "
        f"CROC_SECRET={remote_quote(secret)} CROC_PASS=pass123 "
        f"/usr/bin/time -f {remote_quote(TIME_FORMAT)} -o {remote_quote(time_path)} "
        + " ".join(remote_quote(item) for item in endpoint_command)
    )
    child_script = (
        f"printf '%s\\n' \"$SSH_CONNECTION\" > {remote_quote(peer_path)} || exit 1; "
        f"printf '%s\\n' \"$$\" > {remote_quote(pid_path)} || exit 1; "
        f"exec {env_command}"
    )
    return (
        f"cd -- {remote_quote(run_dir)} || exit 1; "
        f"setsid --wait /bin/sh -c {remote_quote(child_script)} "
        f">{remote_quote(log_path)} 2>&1 < /dev/null & "
        "setsid_pid=$!; wait \"$setsid_pid\""
    )


def wait_for_remote_croc_listeners(args, process, log_path, expected_ports, timeout_seconds=30):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = remote(
            args,
            f"if test -f {remote_quote(log_path)}; then cat -- {remote_quote(log_path)}; fi",
        )
        listening = {
            int(match.group(1))
            for match in re.finditer(r"starting TCP server on .*:(\d+)", text)
        }
        if expected_ports.issubset(listening):
            return text
        code = process.poll()
        if code is not None:
            raise RuntimeError(
                f"Oracle Croc sender exited before opening its local TCP listeners ({code})"
            )
        time.sleep(0.1)
    raise RuntimeError("Oracle Croc sender did not open all expected local TCP listeners")


def _croc_host_port(value):
    value = value.strip().strip("'\"),;]")
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0 or value[closing + 1 : closing + 2] != ":":
            return None
        host, port = value[1:closing], value[closing + 2 :]
    else:
        host, separator, port = value.rpartition(":")
        if not separator:
            return None
    try:
        return host.lower(), int(port)
    except ValueError:
        return None


def _canonical_ip(value):
    address = ipaddress.ip_address(value)
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.compressed.lower()


def _croc_connected_targets(text):
    addresses = re.findall(
        r"connected to ['\"]?(\[[^\]]+\]:\d+|[^\s'\"),]+)['\"]?",
        text,
    )
    return [_croc_host_port(address) for address in addresses]


def croc_direct_tcp_evidence(
    sender_text,
    receiver_text,
    ssh_connection,
    oracle_host,
    expected_peer_ip=None,
    base_port=9009,
    transfers=4,
):
    try:
        expected_host = _canonical_ip(oracle_host)
        ssh_fields = ssh_connection.split()
        source_ip = _canonical_ip(ssh_fields[0])
    except (ValueError, IndexError) as error:
        raise RuntimeError("Croc direct TCP evidence lacks a literal Oracle IP or valid SSH_CONNECTION") from error
    if expected_peer_ip is not None and _canonical_ip(expected_peer_ip) != source_ip:
        raise RuntimeError("SSH_CONNECTION source does not match the leased Croc firewall peer")
    expected_ports = set(range(base_port, base_port + transfers + 1))
    listeners = {
        int(match.group(1))
        for match in re.finditer(r"starting TCP server on .*:(\d+)", sender_text)
    }
    peer_addresses = re.findall(r"client\s+(\[[^\]]+\]:\d+|[^\s]+)\s+connected", sender_text)
    peer_ips = []
    for address in peer_addresses:
        parsed = _croc_host_port(address)
        if parsed is None:
            raise RuntimeError("Croc sender logged an unparseable accepted TCP peer")
        try:
            peer_ips.append(_canonical_ip(parsed[0]))
        except ValueError as error:
            raise RuntimeError("Croc sender logged a non-IP accepted TCP peer") from error
    sender_targets = _croc_connected_targets(sender_text)
    receiver_targets = _croc_connected_targets(receiver_text)
    expected_targets = {(expected_host, port) for port in expected_ports}
    local_targets = {("127.0.0.1", port) for port in expected_ports}
    has_forbidden_transport = re.search(r"tailcat|\bderp\b", sender_text + receiver_text, re.IGNORECASE)
    if listeners != expected_ports:
        raise RuntimeError(f"Croc sender listeners were {sorted(listeners)}, expected {sorted(expected_ports)}")
    if set(receiver_targets) != expected_targets:
        raise RuntimeError(
            "Croc receiver did not connect exclusively to Oracle control/data ports "
            f"9009-9013: {receiver_targets}"
        )
    if set(sender_targets) != local_targets:
        raise RuntimeError(
            "Croc sender did not use exclusively its localhost control/data channels "
            f"9009-9013: {sender_targets}"
        )
    if source_ip not in peer_ips:
        raise RuntimeError("Croc Oracle sender did not observe the SSH_CONNECTION WSL peer")
    loopback_ips = {"127.0.0.1", "::1"}
    if set(peer_ips) - loopback_ips - {source_ip}:
        raise RuntimeError("Croc Oracle sender accepted an unexpected remote TCP peer")
    remote_peer_count = peer_ips.count(source_ip)
    loopback_peer_count = sum(peer_ips.count(address) for address in loopback_ips)
    if remote_peer_count < transfers + 1 or loopback_peer_count < transfers + 1:
        raise RuntimeError("Croc TCP peer logs did not show the remote peer and local sender channels")
    if has_forbidden_transport:
        raise RuntimeError("Croc logs show Tailcat/DERP; direct TCP-only evidence is unavailable")
    return {
        "kind": "croc-local-direct-tcp",
        "verified": True,
        "control_target": f"{expected_host}:{base_port}",
        "data_targets": [f"{expected_host}:{port}" for port in sorted(expected_ports - {base_port})],
        "sender_local_targets": [f"127.0.0.1:{port}" for port in sorted(expected_ports)],
        "sender_listen_ports": sorted(listeners),
        "sender_remote_peer_ip": source_ip,
        "sender_remote_peer_connections": remote_peer_count,
        "sender_loopback_peer_connections": loopback_peer_count,
        "local_only": True,
        "transport_mode": "auto",
        "rationale": (
            "Croc v11.5.4 --local disables Tailcat negotiation and external relay setup; "
            "receiver --ip targets the sender endpoint directly."
        ),
    }


def croc_rows(args, source_hash, received_hash, expected_size, wall, sender_time, receiver_time, path, mode, sender_metric, receiver_metric, run_index, warmup, path_evidence=None):
    payload_start = sender_metric.events.get("payload_start")
    payload_ends = [
        sender_metric.events.get("sender_payload_end"),
        receiver_metric.events.get("receiver_payload_end"),
    ]
    payload_ends = [item for item in payload_ends if item is not None]
    handshake = payload = shutdown = None
    reverse_direct = bool(path_evidence and path_evidence.get("kind") == "croc-local-direct-tcp")
    if payload_start is not None and payload_ends:
        started = args._trial_started
        handshake = payload_start - started
        payload = max(payload_ends) - payload_start
        shutdown = wall - handshake - payload
        if min(handshake, payload, shutdown) < 0:
            raise RuntimeError("Croc returned invalid phase timings")
    elif not reverse_direct:
        raise RuntimeError("Croc output lacked phase markers; inspect the preserved log files")

    resources = resource_values(sender_time, receiver_time)
    rows = []
    for role in ("sender", "receiver"):
        row = {
            "schema_version": 1,
            "commit": git_output("rev-parse", "HEAD"),
            "working_tree_dirty": bool(git_output("status", "--porcelain")),
            "role": role,
            "transport": "croc",
            **croc_provenance(args, expected_size // (1024 * 1024)),
            "transport_mode": mode,
            "path": path,
            "path_start": path,
            "path_end": path,
            "local_candidate_type": None,
            "remote_candidate_type": None,
            "size_bytes": expected_size,
            "chunk_size": 0,
            "pipeline_depth": 1,
            "handshake_seconds": handshake,
            "payload_seconds": payload,
            "shutdown_seconds": shutdown,
            "wall_seconds": wall,
            "effective_mib_per_second": (expected_size / (1024 * 1024)) / wall,
            "source_sha256": source_hash,
            "received_sha256": received_hash,
            "success": True,
            "run_index": run_index,
            "warmup": warmup,
            "measurement_scope": (
                "separate sender and receiver processes (Oracle to WSL; Croc endpoint-local TCP "
                "listener, no Tailcat or public relay; phase markers unavailable from detached "
                "Oracle sender)"
                if reverse_direct
                else "separate sender and receiver processes"
            ),
            "path_evidence": path_evidence,
            "direct_route_verified_both": bool(path_evidence and path_evidence.get("verified")),
            **resources,
        }
        rows.append({key: row[key] for key in METRIC_FIELDS if key in row})
    return rows


def start_croc_receiver(command, time_path, log_path, environment):
    wrapped = [
        "/usr/bin/time",
        "-f",
        TIME_FORMAT,
        "-o",
        str(time_path),
        *command,
    ]
    process = subprocess.Popen(
        wrapped,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    if process.stdout is None:
        raise RuntimeError("Croc receiver output pipe was not created")
    return process, CapturedOutput(process.stdout, log_path)


def terminate_local_process_group(process, timeout_seconds=5):
    if process is None:
        return

    def group_exists():
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout_seconds
    while group_exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if group_exists():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout_seconds
        while group_exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    process.wait()
    if group_exists():
        raise RuntimeError(f"Croc receiver process group {process.pid} did not exit")


def run_croc_reverse(args, source, size_mib, expected_size, source_hash, log_root, run_index, warmup, expected_path):
    tag = "warmup" if warmup else str(run_index)
    run_name = f"croc-oracle-to-wsl-{size_mib}mib-{tag}"
    logs = prepare_local_logs(log_root, run_name)
    run_dir = f"{args.remote_root}/{run_name}"
    remote(args, f"mkdir -- {remote_quote(run_dir)}")
    run_source = f"{run_dir}/{source.name}"
    remote_time_path = f"{run_dir}/sender.time.json"
    remote_log_path = f"{run_dir}/sender.log"
    remote_pid_path = f"{run_dir}/sender.pid"
    remote_peer_path = f"{run_dir}/ssh-connection.txt"
    remote_source, source_is_temporary = prepare_remote_source(
        args, source, run_source, size_mib, expected_size, source_hash
    )

    sender_log = logs / "sender.log"
    receiver_log = logs / "receiver.log"
    receiver_time_path = logs / "receiver.time.json"
    out_dir = logs / "out"
    out_dir.mkdir()
    receiver_path = out_dir / source.name
    secret = f"rtoracle-{size_mib}-{run_index}-{uuid.uuid4().hex[:12]}"
    sender_command = remote_croc_sender_command(
        args,
        run_dir,
        remote_source,
        secret,
        remote_time_path,
        remote_log_path,
        remote_pid_path,
        remote_peer_path,
    )
    receiver_env = os.environ.copy()
    for name in (
        "CROC_RELAY",
        "CROC_RELAY6",
        "CROC_SECRET",
        "CROC_PASS",
        "SOCKS5_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        receiver_env.pop(name, None)
    receiver_env["CROC_SECRET"] = secret
    receiver_env["CROC_PASS"] = "pass123"
    receiver_command = [
        str(args.croc),
        "--yes",
        "--overwrite",
        "--no-compress",
        "--debug",
        "--disable-clipboard",
        "--ignore-stdin",
        "--ip",
        f"{args.host}:9009",
        "--out",
        str(out_dir),
    ]

    started = time.monotonic()
    args._trial_started = started
    sender = subprocess.Popen(
        ssh_command(args, sender_command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    sender_output = CapturedOutput(sender.stdout, sender_log)
    receiver = None
    receiver_output = None
    cleanup_errors = []
    try:
        wait_for_remote_croc_listeners(
            args, sender, remote_log_path, set(range(9009, 9014))
        )
        receiver, receiver_output = start_croc_receiver(
            receiver_command, receiver_time_path, receiver_log, receiver_env
        )
        wait_for_pair(sender, receiver, args.timeout, sender_log, receiver_log)
        wall = time.monotonic() - started
    except Exception as error:
        with receiver_log.open("a", encoding="utf-8") as log:
            log.write(f"\nBenchmark runner failure: {error}\n")
        raise
    finally:
        if sender.poll() is None:
            sender.kill()
        try:
            terminate_local_process_group(receiver)
        except Exception as error:
            cleanup_errors.append(f"failed to terminate local Croc receiver process group: {error}")
        try:
            sender_output.wait()
        except Exception as error:
            cleanup_errors.append(f"failed to collect Oracle Croc sender output: {error}")
        if receiver_output is not None:
            try:
                receiver_output.wait()
            except Exception as error:
                cleanup_errors.append(f"failed to collect WSL Croc receiver output: {error}")
        try:
            terminate_remote_process_group(args, remote_pid_path, run_dir)
        except Exception as error:
            cleanup_errors.append(f"failed to terminate Oracle Croc sender process group: {error}")
        try:
            remote_text = remote(args, f"cat -- {remote_quote(remote_log_path)}")
            sender_log.write_text(remote_text + "\n", encoding="utf-8")
        except Exception as error:
            with sender_log.open("a", encoding="utf-8") as log:
                log.write(f"\nCould not retrieve Oracle Croc sender log: {error}\n")
        redact(sender_log, secret)
        try:
            remote(
                args,
                f"if test -f {remote_quote(remote_log_path)}; then "
                f"python3 -c {remote_quote(REMOTE_REDACT_SECRET_SCRIPT)} "
                f"{remote_quote(remote_log_path)} {remote_quote(secret)}; fi",
            )
        except Exception as error:
            cleanup_errors.append(f"failed to redact Oracle Croc sender log: {error}")
        redact(receiver_log, secret)
        if cleanup_errors:
            with receiver_log.open("a", encoding="utf-8") as log:
                log.write("\nCleanup errors: " + "; ".join(cleanup_errors) + "\n")

    if cleanup_errors:
        raise RuntimeError("; ".join(cleanup_errors))

    sender_time = read_remote_json(args, remote_time_path)
    receiver_time = read_time(receiver_time_path)
    received_hash = verify_local_file(receiver_path, expected_size, source_hash)
    sender_text = sender_log.read_text(encoding="utf-8", errors="replace")
    receiver_text = receiver_log.read_text(encoding="utf-8", errors="replace")
    ssh_connection = remote(args, f"cat -- {remote_quote(remote_peer_path)}")
    evidence = croc_direct_tcp_evidence(
        sender_text,
        receiver_text,
        ssh_connection,
        args.host,
        getattr(args, "croc_direct_peer_ip", None),
    )
    path = "direct"
    if path != expected_path:
        raise RuntimeError(
            f"Croc selected {path} but Rustytransfer selected {expected_path}; "
            "same-path comparison is unavailable"
        )
    rows = croc_rows(
        args,
        source_hash,
        received_hash,
        expected_size,
        wall,
        sender_time,
        receiver_time,
        path,
        "auto",
        SimpleNamespace(events={}),
        receiver_output,
        run_index,
        warmup,
        evidence,
    )
    cleanup_remote(
        args,
        run_dir,
        tuple(
            path
            for path in (
                *((remote_source,) if source_is_temporary else ()),
                remote_time_path,
                remote_log_path,
                remote_pid_path,
                remote_peer_path,
            )
        ),
    )
    receiver_path.unlink()
    out_dir.rmdir()
    return rows, path


def run_croc(args, source, size_mib, expected_size, source_hash, log_root, run_index, warmup, mode, expected_path):
    if args.direction == "oracle-to-wsl":
        return run_croc_reverse(
            args, source, size_mib, expected_size, source_hash, log_root, run_index, warmup, expected_path
        )

    tag = "warmup" if warmup else str(run_index)
    run_name = f"croc-{size_mib}mib-{tag}"
    logs = prepare_local_logs(log_root, run_name)
    run_dir = f"{args.remote_root}/{run_name}"
    out_dir = f"{run_dir}/out"
    remote_time_path = f"{run_dir}/receiver.time.json"
    remote(args, f"mkdir -- {remote_quote(run_dir)} && mkdir -- {remote_quote(out_dir)}")
    received_path = f"{out_dir}/{source.name}"
    sender_time_path = logs / "sender.time.json"
    sender_log = logs / "sender.log"
    receiver_log = logs / "receiver.log"
    code = f"rtoracle-{size_mib}-{run_index}-{uuid.uuid4().hex[:12]}"
    sender_env = os.environ.copy()
    sender_env["CROC_SECRET"] = code
    sender_env["CROC_PASS"] = "pass123"
    sender_command = [
        str(args.croc),
        "--no-compress",
        "--debug",
        "--disable-clipboard",
        "--ignore-stdin",
        "send",
        "--transport",
        mode,
        str(source),
    ]
    receiver_command = (
        f"env CROC_SECRET={remote_quote(code)} CROC_PASS=pass123 "
        f"/usr/bin/time -f {remote_quote(TIME_FORMAT)} -o {remote_quote(remote_time_path)} "
        f"{remote_quote(args.remote_croc)} --yes --overwrite --no-compress --debug "
        f"--disable-clipboard --ignore-stdin --out {remote_quote(out_dir)}"
    )

    started = time.monotonic()
    args._trial_started = started
    sender, sender_output = time_command(
        sender_command, sender_time_path, sender_log, sender_env
    )
    receiver = None
    receiver_output = None
    try:
        time.sleep(0.2)
        receiver = subprocess.Popen(
            ssh_command(args, receiver_command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        receiver_output = CapturedOutput(receiver.stdout, receiver_log)
        wait_for_pair(sender, receiver, args.timeout, sender_log, receiver_log)
    finally:
        if sender.poll() is None:
            sender.kill()
        if receiver is not None and receiver.poll() is None:
            receiver.kill()
        sender_output.wait()
        if receiver_output is not None:
            receiver_output.wait()
        redact(sender_log, code)
        redact(receiver_log, code)

    wall = time.monotonic() - started
    sender_time = read_time(sender_time_path)
    receiver_time = read_remote_json(args, remote_time_path)
    received_hash = verify_remote_file(args, received_path, expected_size, source_hash)
    sender_text = sender_log.read_text(encoding="utf-8", errors="replace")
    receiver_text = receiver_log.read_text(encoding="utf-8", errors="replace")
    if mode == "relay":
        path = "relay"
    else:
        path, _evidence = selected_auto_path(sender_text + "\n" + receiver_text)
    if path != expected_path:
        raise RuntimeError(
            f"Croc selected {path} but Rustytransfer selected {expected_path}; "
            "same-path comparison is unavailable"
        )

    rows = croc_rows(
        args,
        source_hash,
        received_hash,
        expected_size,
        wall,
        sender_time,
        receiver_time,
        path,
        mode,
        sender_output,
        receiver_output,
        run_index,
        warmup,
    )
    cleanup_remote(
        args,
        run_dir,
        (received_path, remote_time_path),
        (out_dir,),
    )
    return rows, path


def run_size(args, size_mib, source, raw_path, log_root):
    expected_size = size_mib * 1024 * 1024
    if source.stat().st_size != expected_size:
        raise RuntimeError(f"input {source} has the wrong size for {size_mib} MiB")
    source_hash = sha256_file(source)
    rusty_warmup, expected_path = run_rusty(
        args, source, size_mib, expected_size, source_hash, log_root, 0, True
    )
    append_and_validate_rust_rows(raw_path, rusty_warmup, args.rusty_path, expected_path)
    mode = "auto" if expected_path == "direct" else "relay"
    croc_warmup, _ = run_croc(
        args,
        source,
        size_mib,
        expected_size,
        source_hash,
        log_root,
        0,
        True,
        mode,
        expected_path,
    )
    append_rows(raw_path, croc_warmup)

    for trial in range(1, args.runs + 1):
        order = ("rustytransfer", "croc") if trial % 2 else ("croc", "rustytransfer")
        for candidate in order:
            if candidate == "rustytransfer":
                rows, path = run_rusty(
                    args, source, size_mib, expected_size, source_hash, log_root, trial, False
                )
                append_and_validate_rust_rows(raw_path, rows, args.rusty_path, path)
            else:
                rows, path = run_croc(
                    args,
                    source,
                    size_mib,
                    expected_size,
                    source_hash,
                    log_root,
                    trial,
                    False,
                    mode,
                    expected_path,
                )
            if candidate == "croc":
                append_rows(raw_path, rows)
            print(
                f"size={size_mib} MiB transport={candidate} path={path} "
                f"trial={trial} warmup={str(False).lower()} sha256={source_hash}"
            )


def run_rusty_only_size(args, size_mib, source, raw_path, log_root):
    expected_size = size_mib * 1024 * 1024
    if source.stat().st_size != expected_size:
        raise RuntimeError(f"input {source} has the wrong size for {size_mib} MiB")
    source_hash = sha256_file(source)
    rows, expected_path = run_rusty(
        args, source, size_mib, expected_size, source_hash, log_root, 0, True
    )
    append_and_validate_rust_rows(raw_path, rows, args.rusty_path, expected_path)

    for trial in range(1, args.runs + 1):
        rows, path = run_rusty(
            args, source, size_mib, expected_size, source_hash, log_root, trial, False
        )
        append_and_validate_rust_rows(raw_path, rows, args.rusty_path, path)
        print(
            f"size={size_mib} MiB transport=rustytransfer path={path} "
            f"trial={trial} warmup=false sha256={source_hash}"
        )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--scp", default="scp")
    parser.add_argument("--ssh-key", required=True, type=Path)
    parser.add_argument("--croc", type=Path, help="local Croc binary")
    parser.add_argument("--remote-croc")
    parser.add_argument(
        "--croc-version",
        default="11.5.3",
        help="required matching Croc version for both endpoints (default: %(default)s)",
    )
    parser.add_argument("--rusty-sender", required=True, type=Path)
    parser.add_argument("--remote-rusty", required=True)
    parser.add_argument("--input-64", required=True, type=Path)
    parser.add_argument("--input-512", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--remote-input-64", help="pre-staged Oracle source for reverse 64 MiB trials")
    parser.add_argument("--remote-input-512", help="pre-staged Oracle source for reverse 512 MiB trials")
    parser.add_argument("--build-id", required=True, help="candidate/build identifier for Rustytransfer")
    parser.add_argument("--storage-class", required=True, help="input and destination storage description")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--chunk-size",
        type=int,
        help="sender payload chunk size in bytes (1 through 1048576); receiver negotiates it",
    )
    parser.add_argument(
        "--stream-window-bytes",
        type=int,
        help="opt-in Iroh stream receive window (1250000 through 5000000 bytes)",
    )
    parser.add_argument(
        "--direction",
        choices=("wsl-to-oracle", "oracle-to-wsl"),
        default="wsl-to-oracle",
    )
    parser.add_argument("--rusty-path", choices=("direct", "relay"), default="direct")
    parser.add_argument("--rusty-auth", choices=("pake", "invite"), default="pake")
    parser.add_argument(
        "--payload-profile",
        action="store_true",
        help="enable opt-in payload stage wall-time diagnostics on both endpoints",
    )
    parser.add_argument(
        "--experimental-streams",
        type=int,
        choices=(1, 4),
        help="run the test-only shared-key parallel-stream example with 1 or 4 streams",
    )
    parser.add_argument(
        "--experimental-connections",
        type=int,
        choices=(1, 4),
        help="run the test-only shared-key benchmark example with 1 or 4 independent QUIC connections",
    )
    parser.add_argument(
        "--rusty-only",
        action="store_true",
        help="measure Rustytransfer alone; use this when Croc cannot select the same path",
    )
    parser.add_argument("--timeout", type=int, default=900)
    return parser


def validate_local_args(parser, args):
    if args.runs < 1:
        parser.error("--runs must be positive")
    chunk_size = getattr(args, "chunk_size", None)
    if chunk_size is not None and not 1 <= chunk_size <= 1_048_576:
        parser.error("--chunk-size must be between 1 and 1048576 bytes")
    stream_window_bytes = getattr(args, "stream_window_bytes", None)
    if stream_window_bytes is not None and not 1_250_000 <= stream_window_bytes <= 5_000_000:
        parser.error("--stream-window-bytes must be between 1250000 and 5000000 bytes")
    experimental_streams = getattr(args, "experimental_streams", None)
    if experimental_streams not in (None, 1, 4):
        parser.error("--experimental-streams must be 1 or 4")
    if experimental_streams is not None and (not args.rusty_only or args.rusty_auth != "invite"):
        parser.error("--experimental-streams requires --rusty-only --rusty-auth invite and the shared-key benchmark example binary")
    if experimental_streams is not None and getattr(args, "chunk_size", None) is None:
        parser.error("--experimental-streams requires an explicit --chunk-size")
    if experimental_streams is not None and payload_profile_enabled(args):
        parser.error("--payload-profile is not supported by the shared-key benchmark example")
    experimental_connections = getattr(args, "experimental_connections", None)
    if experimental_connections not in (None, 1, 4):
        parser.error("--experimental-connections must be 1 or 4")
    if experimental_connections is not None and experimental_streams is None:
        parser.error("--experimental-connections requires an explicit --experimental-streams")
    if experimental_connections is not None and (
        not args.rusty_only or args.rusty_auth != "invite"
    ):
        parser.error("--experimental-connections requires --rusty-only --rusty-auth invite and the shared-key benchmark example binary")
    if experimental_connections == 4 and experimental_streams != 4:
        parser.error("four experimental connections require --experimental-streams 4")
    if not args.build_id.strip():
        parser.error("--build-id cannot be empty")
    if not args.storage_class.strip():
        parser.error("--storage-class cannot be empty")
    if args.direction == "oracle-to-wsl" and not args.rusty_only:
        if args.rusty_path != "direct":
            parser.error("Oracle-to-WSL Croc comparisons require --rusty-path direct")
        if args.croc_version != "11.5.4":
            parser.error("Oracle-to-WSL direct Croc comparison requires the source-audited --croc-version 11.5.4")
        try:
            oracle_address = ipaddress.ip_address(args.host)
        except ValueError:
            parser.error("Oracle-to-WSL Croc direct comparison requires --host to be a literal IPv4 address")
        if oracle_address.version != 4:
            parser.error("Oracle-to-WSL Croc direct comparison currently supports IPv4 only")
    has_remote_input_64 = bool(args.remote_input_64)
    has_remote_input_512 = bool(args.remote_input_512)
    if has_remote_input_64 != has_remote_input_512:
        parser.error("--remote-input-64 and --remote-input-512 must be provided together")
    if (has_remote_input_64 or has_remote_input_512) and args.direction != "oracle-to-wsl":
        parser.error("pre-staged remote inputs are only valid for --direction oracle-to-wsl")
    if has_remote_input_64:
        remote_root = posixpath.normpath(args.remote_root)
        if not remote_root.startswith("/"):
            parser.error("--remote-root must be absolute when pre-staged inputs are used")
        for remote_path in (args.remote_input_64, args.remote_input_512):
            if not remote_path.startswith("/"):
                parser.error("pre-staged remote input paths must be absolute")
            if posixpath.commonpath((remote_root, posixpath.normpath(remote_path))) == remote_root:
                parser.error("pre-staged remote inputs must be outside --remote-root")
    required = [args.ssh_key, args.rusty_sender, args.input_64, args.input_512]
    if not args.rusty_only:
        if args.croc is None:
            parser.error("--croc is required unless --rusty-only is set")
        if not args.remote_croc:
            parser.error("--remote-croc is required unless --rusty-only is set")
        required.append(args.croc)
    for path in required:
        if not path.is_file():
            parser.error(f"required local file is missing: {path}")
    if not args.output_dir.is_dir():
        parser.error(f"output directory must already exist: {args.output_dir}")


def main():
    parser = build_parser()
    args = parser.parse_args()

    validate_local_args(parser, args)

    raw_paths = {
        64: args.output_dir / "oracle-64.jsonl",
        512: args.output_dir / "oracle-512.jsonl",
    }
    if any(path.exists() for path in raw_paths.values()):
        parser.error("refusing to overwrite an existing Oracle JSONL result")
    for path in raw_paths.values():
        path.touch()
    log_root = args.output_dir / "logs"
    log_root.mkdir(exist_ok=False)

    remote(args, f"test ! -e {remote_quote(args.remote_root)} && mkdir -- {remote_quote(args.remote_root)}")
    remote(
        args,
        f"test -x {remote_quote(args.remote_rusty)} && command -v sha256sum "
        "&& command -v setsid && test -x /usr/bin/time && command -v python3",
    )
    args.local_rusty_sha256 = sha256_file(args.rusty_sender)
    args.remote_rusty_sha256 = remote_sha256(args, args.remote_rusty)
    if not args.rusty_only:
        remote(args, f"test -x {remote_quote(args.remote_croc)}")
        remote_version = remote(args, f"{remote_quote(args.remote_croc)} --version")
        if args.croc_version not in remote_version:
            raise RuntimeError(f"unexpected Oracle Croc version: {remote_version}")
        local_version = subprocess.run(
            [str(args.croc), "--version"], check=True, capture_output=True, text=True
        ).stdout
        if args.croc_version not in local_version:
            raise RuntimeError(f"unexpected WSL Croc version: {local_version.strip()}")
        args.local_croc_sha256 = sha256_file(args.croc)
        args.remote_croc_sha256 = remote_sha256(args, args.remote_croc)

    try:
        runner = run_rusty_only_size if args.rusty_only else run_size
        runner(args, 64, args.input_64, raw_paths[64], log_root)
        runner(args, 512, args.input_512, raw_paths[512], log_root)
    except Exception:
        print("Benchmark stopped; local logs and any incomplete Oracle trial are preserved.", file=sys.stderr)
        raise
    remote(args, f"rmdir -- {remote_quote(args.remote_root)}")
    print(f"Raw results: {raw_paths[64]} and {raw_paths[512]}")


if __name__ == "__main__":
    main()
