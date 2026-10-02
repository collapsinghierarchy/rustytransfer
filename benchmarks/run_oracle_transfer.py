#!/usr/bin/env python3
"""Measure Croc 11.5.3 and Rustytransfer from WSL to one Oracle host."""

import argparse
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

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


def rust_provenance(args):
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
        "source_staging": "pre-staged" if remote_staged_input(args, args.direction == "oracle-to-wsl", 64) else "per-trial",
    }


def croc_provenance(args):
    return {
        "build_id": "croc-11.5.3",
        "sender_binary_sha256": args.local_croc_sha256,
        "receiver_binary_sha256": args.remote_croc_sha256,
        "direction": "wsl-to-oracle",
        "host_pair": f"WSL/{args.user}@{args.host}",
        "storage_class": args.storage_class,
        "pairing_mode": "croc-secret",
        "profile_mode": "standard",
        "source_staging": "per-trial",
    }


def remote_quote(value):
    return shlex.quote(str(value))


def payload_profile_enabled(args):
    return bool(getattr(args, "payload_profile", False))


def configure_profile_env(environment, args):
    environment.pop("RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE", None)
    if payload_profile_enabled(args):
        environment["RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE"] = "1"


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
    env_command = (
        f"env -u RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE {profile_env}"
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
    command = [args.remote_rusty, "--transport", "iroh", "send"]
    if args.rusty_auth == "invite":
        command.append("--direct")
    command.extend(["--file", source_path])
    if args.rusty_auth == "pake":
        command.extend(["--password", "ABCDE"])
    return remote_process_command(
        args, run_dir, command, metrics_path, time_path, log_path, pid_path
    )


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
    direct_route_verified_both = verified_direct_evidence(sender_metrics) and verified_direct_evidence(receiver_metrics)
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
    sender_env["RUSTYTRANSFER_METRICS_JSONL"] = str(sender_metrics_path)
    path_env = (
        "RUSTYTRANSFER_BENCH_WAIT_DIRECT"
        if args.rusty_path == "direct"
        else "RUSTYTRANSFER_BENCH_RELAY_ONLY"
    )
    sender_env[path_env] = "1"
    sender_env["RUSTYTRANSFER_BENCH_PATH_EVIDENCE"] = "1"
    sender_command = [
        str(args.rusty_sender),
        "--transport",
        "iroh",
        "send",
    ]
    if args.rusty_auth == "invite":
        sender_command.append("--direct")
    sender_command.extend([
        "--file",
        str(source),
    ])
    if args.rusty_auth == "pake":
        sender_command.extend(["--password", "ABCDE"])

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
    if sender_metric["path"] != receiver_metric["path"]:
        raise RuntimeError(
            f"sender/receiver selected different paths: "
            f"{sender_metric['path']} / {receiver_metric['path']}"
        )
    path = sender_metric["path"]
    if path not in ("direct", "relay"):
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
        {**rust_provenance(args), "pairing_mode": args.rusty_auth},
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
    if sender_metric["path"] != receiver_metric["path"]:
        raise RuntimeError(
            f"sender/receiver selected different paths: "
            f"{sender_metric['path']} / {receiver_metric['path']}"
        )
    path = sender_metric["path"]
    if path not in ("direct", "relay"):
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
        {**rust_provenance(args), "pairing_mode": args.rusty_auth},
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


def croc_rows(args, source_hash, received_hash, expected_size, wall, sender_time, receiver_time, path, mode, sender_metric, receiver_metric, run_index, warmup):
    payload_start = sender_metric.events.get("payload_start")
    payload_ends = [
        sender_metric.events.get("sender_payload_end"),
        receiver_metric.events.get("receiver_payload_end"),
    ]
    payload_ends = [item for item in payload_ends if item is not None]
    if payload_start is None or not payload_ends:
        raise RuntimeError("Croc output lacked phase markers; inspect the preserved log files")
    started = args._trial_started
    handshake = payload_start - started
    payload = max(payload_ends) - payload_start
    shutdown = wall - handshake - payload
    if min(handshake, payload, shutdown) < 0:
        raise RuntimeError("Croc returned invalid phase timings")

    resources = resource_values(sender_time, receiver_time)
    rows = []
    for role in ("sender", "receiver"):
        row = {
            "schema_version": 1,
            "commit": git_output("rev-parse", "HEAD"),
            "working_tree_dirty": bool(git_output("status", "--porcelain")),
            "role": role,
            "transport": "croc",
            **croc_provenance(args),
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
            "measurement_scope": "separate sender and receiver processes (WSL to Oracle)",
            **resources,
        }
        rows.append({key: row[key] for key in METRIC_FIELDS if key in row})
    return rows


def run_croc(args, source, size_mib, expected_size, source_hash, log_root, run_index, warmup, mode, expected_path):
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
    parser.add_argument("--croc", type=Path, help="local Croc 11.5.3 binary")
    parser.add_argument("--remote-croc")
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
        "--rusty-only",
        action="store_true",
        help="measure Rustytransfer alone; use this when Croc cannot select the same path",
    )
    parser.add_argument("--timeout", type=int, default=900)
    return parser


def validate_local_args(parser, args):
    if args.runs < 1:
        parser.error("--runs must be positive")
    if not args.build_id.strip():
        parser.error("--build-id cannot be empty")
    if not args.storage_class.strip():
        parser.error("--storage-class cannot be empty")
    if args.direction == "oracle-to-wsl" and not args.rusty_only:
        parser.error("Oracle-to-WSL currently requires --rusty-only; Croc reverse comparison is unavailable")
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
        if "11.5.3" not in remote_version:
            raise RuntimeError(f"unexpected Oracle Croc version: {remote_version}")
        local_version = subprocess.run(
            [str(args.croc), "--version"], check=True, capture_output=True, text=True
        ).stdout
        if "11.5.3" not in local_version:
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
