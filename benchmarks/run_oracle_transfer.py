#!/usr/bin/env python3
"""Measure Croc 11.5.3 and Rustytransfer from WSL to one Oracle host."""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

from run_croc_baseline import CapturedOutput, git_output, parse_time, selected_auto_path, time_command


TIME_FORMAT = '{"user_cpu_seconds":%U,"system_cpu_seconds":%S,"max_rss_kib":%M}'
SHARE_CODE = re.compile(r"Share this code:\s*([0-9]{4}-[A-Z]{5})")
PAYLOAD_MARKERS = ("payload_start", "sender_payload_end", "receiver_payload_end")
METRIC_FIELDS = (
    "schema_version",
    "commit",
    "working_tree_dirty",
    "role",
    "transport",
    "transport_mode",
    "path",
    "path_start",
    "path_end",
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


def remote_quote(value):
    return shlex.quote(str(value))


def append_rows(path, rows):
    with path.open("a", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, sort_keys=True) + "\n")


def redact(path, secret):
    if not secret or not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(text.replace(secret, "<redacted>"), encoding="utf-8")


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


def resource_values(local_time, remote_time):
    sender_cpu = local_time["user_cpu_seconds"] + local_time["system_cpu_seconds"]
    receiver_cpu = remote_time["user_cpu_seconds"] + remote_time["system_cpu_seconds"]
    return {
        "sender_cpu_seconds": sender_cpu,
        "receiver_cpu_seconds": receiver_cpu,
        "sender_max_rss_kib": local_time["max_rss_kib"],
        "receiver_max_rss_kib": remote_time["max_rss_kib"],
    }


def rust_rows(metrics_path, receiver_metrics, source_hash, received_hash, expected_size, wall, local_time, remote_time, run_index, warmup):
    sender_metrics = json.loads(metrics_path.read_text(encoding="utf-8").splitlines()[0])
    resources = resource_values(local_time, remote_time)
    rows = []
    for role, metric in (("sender", sender_metrics), ("receiver", receiver_metrics)):
        row = {key: metric.get(key) for key in METRIC_FIELDS}
        row.update(
            {
                "schema_version": 1,
                "commit": git_output("rev-parse", "HEAD"),
                "working_tree_dirty": bool(git_output("status", "--porcelain")),
                "role": role,
                "transport": "iroh",
                "transport_mode": None,
                "size_bytes": expected_size,
                "chunk_size": 262144,
                "pipeline_depth": 1,
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
        )
        rows.append({key: row.get(key) for key in METRIC_FIELDS})
    return rows


def prepare_local_logs(log_root, run_name):
    run_logs = log_root / run_name
    run_logs.mkdir(parents=True, exist_ok=False)
    return run_logs


def run_rusty(args, source, size_mib, expected_size, source_hash, log_root, run_index, warmup):
    tag = "warmup" if warmup else str(run_index)
    run_name = f"rustytransfer-{size_mib}mib-{tag}"
    logs = prepare_local_logs(log_root, run_name)
    run_dir = f"{args.remote_root}/{run_name}"
    remote(args, f"mkdir -- {remote_quote(run_dir)}")
    output_path = f"{run_dir}/received.bin"
    remote_metrics_path = f"{run_dir}/receiver.jsonl"
    remote_time_path = f"{run_dir}/receiver.time.json"
    sender_metrics_path = logs / "sender.jsonl"
    sender_time_path = logs / "sender.time.json"
    sender_log = logs / "sender.log"
    receiver_log = logs / "receiver.log"
    sender_env = os.environ.copy()
    sender_env["RUSTYTRANSFER_METRICS_JSONL"] = str(sender_metrics_path)
    path_env = (
        "RUSTYTRANSFER_BENCH_WAIT_DIRECT"
        if args.rusty_path == "direct"
        else "RUSTYTRANSFER_BENCH_RELAY_ONLY"
    )
    sender_env[path_env] = "1"
    sender_command = [
        str(args.rusty_sender),
        "--transport",
        "iroh",
        "send",
        "--file",
        str(source),
        "--password",
        "ABCDE",
    ]

    started = time.monotonic()
    sender, sender_output = time_command(
        sender_command, sender_time_path, sender_log, sender_env
    )
    receiver = None
    receiver_output = None
    share_code = None
    try:
        share_code = wait_for_share_code(sender, sender_log)
        remote_receiver = (
            f"env {path_env}=1 "
            f"RUSTYTRANSFER_METRICS_JSONL={remote_quote(remote_metrics_path)} "
            f"/usr/bin/time -f {remote_quote(TIME_FORMAT)} "
            f"-o {remote_quote(remote_time_path)} {remote_quote(args.remote_rusty)} "
            f"--transport iroh recv --code {remote_quote(share_code)} "
            f"--out {remote_quote(output_path)}"
        )
        receiver = subprocess.Popen(
            ssh_command(args, remote_receiver),
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
        redact(sender_log, share_code)
        redact(receiver_log, share_code)

    wall = time.monotonic() - started
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
        sender_metrics_path,
        receiver_metric,
        source_hash,
        received_hash,
        expected_size,
        wall,
        sender_time,
        receiver_time,
        run_index,
        warmup,
    )
    cleanup_remote(args, run_dir, (output_path, remote_metrics_path, remote_time_path))
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
        rows.append({key: row.get(key) for key in METRIC_FIELDS})
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
    append_rows(raw_path, rusty_warmup)
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
                if path != expected_path:
                    raise RuntimeError(
                        f"Rustytransfer path changed from {expected_path} to {path} during sweep"
                    )
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
    if expected_path != args.rusty_path:
        raise RuntimeError(
            f"Rustytransfer selected {expected_path}; requested benchmark path was {args.rusty_path}"
        )
    append_rows(raw_path, rows)

    for trial in range(1, args.runs + 1):
        rows, path = run_rusty(
            args, source, size_mib, expected_size, source_hash, log_root, trial, False
        )
        if path != expected_path:
            raise RuntimeError(
                f"Rustytransfer path changed from {expected_path} to {path} during sweep"
            )
        append_rows(raw_path, rows)
        print(
            f"size={size_mib} MiB transport=rustytransfer path={path} "
            f"trial={trial} warmup=false sha256={source_hash}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--ssh-key", required=True, type=Path)
    parser.add_argument("--croc", required=True, type=Path, help="local Croc 11.5.3 binary")
    parser.add_argument("--remote-croc", required=True)
    parser.add_argument("--rusty-sender", required=True, type=Path)
    parser.add_argument("--remote-rusty", required=True)
    parser.add_argument("--input-64", required=True, type=Path)
    parser.add_argument("--input-512", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--rusty-path", choices=("direct", "relay"), default="direct")
    parser.add_argument(
        "--rusty-only",
        action="store_true",
        help="measure Rustytransfer alone; use this when Croc cannot select the same path",
    )
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    if args.runs < 1:
        parser.error("--runs must be positive")
    for path in (args.ssh_key, args.croc, args.rusty_sender, args.input_64, args.input_512):
        if not path.is_file():
            parser.error(f"required local file is missing: {path}")
    if not args.output_dir.is_dir():
        parser.error(f"output directory must already exist: {args.output_dir}")

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
    remote(args, f"test -x {remote_quote(args.remote_croc)} && test -x {remote_quote(args.remote_rusty)} && command -v sha256sum")
    if not args.rusty_only:
        remote_version = remote(args, f"{remote_quote(args.remote_croc)} --version")
        if "11.5.3" not in remote_version:
            raise RuntimeError(f"unexpected Oracle Croc version: {remote_version}")
        local_version = subprocess.run(
            [str(args.croc), "--version"], check=True, capture_output=True, text=True
        ).stdout
        if "11.5.3" not in local_version:
            raise RuntimeError(f"unexpected WSL Croc version: {local_version.strip()}")

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
