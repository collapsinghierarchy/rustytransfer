#!/usr/bin/env python3
"""Run Croc 11.5.3 auto and forced-relay transfers against a local relay."""

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path


TIMEOUT_SECONDS = 900
RUN_ORDER = ("auto", "relay", "relay", "auto", "auto", "relay", "relay", "auto", "auto", "relay")
OUTPUT_MARKERS = {
    "payload_start": "start sending data!",
    "sender_payload_end": "done piping",
    "receiver_payload_end": "finished receiving!",
}


class CapturedOutput:
    def __init__(self, stream, log_path):
        self.events = {}
        self.error = None
        self.thread = threading.Thread(
            target=self._copy, args=(stream, log_path), daemon=True
        )
        self.thread.start()

    def _copy(self, stream, log_path):
        trailing = ""
        try:
            with log_path.open("wb") as log:
                while chunk := stream.read1(4096):
                    log.write(chunk)
                    log.flush()
                    text = trailing + chunk.decode("utf-8", errors="replace")
                    observed_at = time.monotonic()
                    for event, marker in OUTPUT_MARKERS.items():
                        if event not in self.events and marker in text:
                            self.events[event] = observed_at
                    trailing = text[-256:]
        except OSError as error:
            self.error = error

    def wait(self):
        self.thread.join()
        if self.error is not None:
            raise self.error


def output_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def git_output(*args):
    try:
        result = subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def time_command(command, time_path, log_path, environment):
    wrapped = [
        "/usr/bin/time",
        "-f",
        '{"user_cpu_seconds":%U,"system_cpu_seconds":%S,"max_rss_kib":%M}',
        "-o",
        str(time_path),
        *command,
    ]
    process = subprocess.Popen(
        wrapped,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    if process.stdout is None:
        raise RuntimeError("Croc process output pipe was not created")
    return process, CapturedOutput(process.stdout, log_path)


def wait_for_relay(process, port, log_path):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Croc relay exited early; see {log_path}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Croc relay did not open 127.0.0.1:{port}; see {log_path}")


def wait_for_pair(sender, receiver, deadline, sender_log, receiver_log):
    while sender.poll() is None or receiver.poll() is None:
        if time.monotonic() > deadline:
            sender.kill()
            receiver.kill()
            raise RuntimeError(f"Croc transfer timed out; logs: {sender_log}, {receiver_log}")
        for process, other, log in (
            (sender, receiver, sender_log),
            (receiver, sender, receiver_log),
        ):
            code = process.poll()
            if code is not None and code != 0:
                if other.poll() is None:
                    other.kill()
                raise RuntimeError(f"Croc endpoint exited with {code}; see {log}")
        time.sleep(0.05)
    if sender.returncode != 0 or receiver.returncode != 0:
        raise RuntimeError(
            f"Croc endpoint failure ({sender.returncode}, {receiver.returncode}); "
            f"logs: {sender_log}, {receiver_log}"
        )


def selected_auto_path(logs):
    evidence = []
    observed_paths = []
    for line in logs.splitlines():
        lower = line.lower()
        if any(
            word in lower
            for word in (
                "selected path",
                "data path",
                "direct path",
                "relay path",
                "selected=direct",
                "selected=relay",
                "selected croc direct",
                "selected croc relay",
                "tailcat transport:",
            )
        ):
            evidence.append(line.strip()[:300])
        match = re.search(r"tailcat transport(?: summary)?: path=(direct|relay)", lower)
        if match:
            observed_paths.append(match.group(1))
        match = re.search(r"selected=(direct|relay)\b", lower)
        if match:
            observed_paths.append(match.group(1))
        match = re.search(r"selected croc (direct|relay)\b", lower)
        if match:
            observed_paths.append(match.group(1))
    if observed_paths:
        unique_paths = set(observed_paths)
        if len(unique_paths) == 1:
            return unique_paths.pop(), evidence
        return "mixed", evidence
    text = "\n".join(evidence).lower()
    return "unknown", evidence


def parse_time(path):
    return json.loads(path.read_text(encoding="utf-8").strip())


def start_relay(croc, password, relay_port, log_path):
    ports = ",".join(str(relay_port + offset) for offset in range(5))
    environment = os.environ.copy()
    environment["CROC_PASS"] = password
    log = log_path.open("wb")
    process = subprocess.Popen(
        [croc, "relay", "--host", "127.0.0.1", "--ports", ports],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    wait_for_relay(process, relay_port, log_path)
    return process, log


def run_trial(args, mode, trial, warmup, input_path, source_sha256, output_root, log_root):
    size_bytes = input_path.stat().st_size
    code = f"rtbench-{mode}-{trial}-{uuid.uuid4().hex[:10]}"
    trial_name = f"{mode}-warmup-{trial}" if warmup else f"{mode}-{trial}"
    trial_logs = log_root / trial_name
    trial_logs.mkdir(parents=True, exist_ok=False)
    receive_dir = output_root / trial_name
    receive_dir.mkdir(parents=True, exist_ok=False)
    sender_log = trial_logs / "sender.log"
    receiver_log = trial_logs / "receiver.log"
    sender_time = trial_logs / "sender.time.json"
    receiver_time = trial_logs / "receiver.time.json"

    common = [
        args.croc,
        "--relay",
        f"127.0.0.1:{args.relay_port}",
        "--pass",
        args.relay_password,
        "--no-compress",
        "--debug",
        "--disable-clipboard",
        "--ignore-stdin",
    ]
    endpoint_environment = os.environ.copy()
    endpoint_environment["CROC_SECRET"] = code
    sender_command = [
        *common,
        "send",
        "--transport",
        mode,
        str(input_path),
    ]
    receiver_command = [
        *common,
        "--yes",
        "--overwrite",
        "--out",
        str(receive_dir),
    ]

    started = time.monotonic()
    sender, sender_output = time_command(
        sender_command, sender_time, sender_log, endpoint_environment
    )
    time.sleep(0.2)
    receiver, receiver_output = time_command(
        receiver_command, receiver_time, receiver_log, endpoint_environment
    )
    try:
        wait_for_pair(
            sender,
            receiver,
            started + TIMEOUT_SECONDS,
            sender_log,
            receiver_log,
        )
    finally:
        sender_output.wait()
        receiver_output.wait()
    wall_seconds = time.monotonic() - started

    payload_start = sender_output.events.get("payload_start")
    payload_end_candidates = [
        sender_output.events.get("sender_payload_end"),
        receiver_output.events.get("receiver_payload_end"),
    ]
    payload_end_candidates = [event for event in payload_end_candidates if event is not None]
    if payload_start is None or not payload_end_candidates:
        raise RuntimeError(
            "Croc debug output did not expose payload boundaries; "
            f"see logs: {sender_log}, {receiver_log}"
        )
    payload_end = max(payload_end_candidates)
    handshake_seconds = payload_start - started
    payload_seconds = payload_end - payload_start
    shutdown_seconds = wall_seconds - handshake_seconds - payload_seconds
    if min(handshake_seconds, payload_seconds, shutdown_seconds) < 0:
        raise RuntimeError(f"invalid Croc phase timing; see logs: {sender_log}, {receiver_log}")

    received_path = receive_dir / input_path.name
    if not received_path.is_file():
        raise RuntimeError(f"Croc did not create expected output {received_path}")
    received_sha256 = output_hash(received_path)
    if received_path.stat().st_size != size_bytes or received_sha256 != source_sha256:
        raise RuntimeError(
            f"Croc output mismatch; sender log: {sender_log}, receiver log: {receiver_log}"
        )

    sender_resources = parse_time(sender_time)
    receiver_resources = parse_time(receiver_time)
    sender_text = sender_log.read_text(encoding="utf-8", errors="replace")
    receiver_text = receiver_log.read_text(encoding="utf-8", errors="replace")
    if mode == "relay":
        path = "relay"
        path_evidence = ["forced by croc send --transport relay"]
    else:
        path, path_evidence = selected_auto_path(sender_text + "\n" + receiver_text)

    mib = size_bytes / (1024 * 1024)
    rows = []
    for role in ("sender", "receiver"):
        rows.append(
            {
                "schema_version": 1,
                "commit": git_output("rev-parse", "HEAD"),
                "working_tree_dirty": (
                    bool(git_output("status", "--porcelain"))
                    if git_output("status", "--porcelain") is not None
                    else None
                ),
                "role": role,
                "transport": "croc",
                "transport_mode": mode,
                "path": path,
                "path_start": path,
                "path_end": path,
                "path_evidence": path_evidence,
                "size_bytes": size_bytes,
                "chunk_size": 0,
                "pipeline_depth": 1,
                "handshake_seconds": handshake_seconds,
                "payload_seconds": payload_seconds,
                "shutdown_seconds": shutdown_seconds,
                "wall_seconds": wall_seconds,
                "effective_mib_per_second": mib / wall_seconds,
                "sender_cpu_seconds": sender_resources["user_cpu_seconds"]
                + sender_resources["system_cpu_seconds"],
                "receiver_cpu_seconds": receiver_resources["user_cpu_seconds"]
                + receiver_resources["system_cpu_seconds"],
                "sender_max_rss_kib": sender_resources["max_rss_kib"],
                "receiver_max_rss_kib": receiver_resources["max_rss_kib"],
                "source_sha256": source_sha256,
                "received_sha256": received_sha256,
                "success": True,
                "run_index": trial,
                "warmup": warmup,
                "measurement_scope": "separate sender and receiver processes",
            }
        )
    return rows, received_path, receive_dir, wall_seconds, path, path_evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--croc", required=True, help="path to the Croc 11.5.3 executable")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="append-only JSONL file")
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--receive-root", type=Path)
    parser.add_argument("--relay-port", type=int, default=19009)
    parser.add_argument("--relay-password", default="phase0-local-benchmark")
    parser.add_argument("--mode", choices=("all", "auto", "relay"), default="all")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--runs-per-mode", type=int, default=5)
    args = parser.parse_args()
    run_limit = len(RUN_ORDER) // 2 if args.mode == "all" else 100
    if args.runs_per_mode < 1 or args.runs_per_mode > run_limit:
        raise SystemExit(f"--runs-per-mode must be between 1 and {run_limit}")
    if args.warmups < 0:
        raise SystemExit("--warmups cannot be negative")

    version = subprocess.run(
        [args.croc, "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if version != "croc version 11.5.3":
        raise SystemExit(f"expected Croc 11.5.3, found {version}")
    source_sha256 = output_hash(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    output_root = args.receive_root or args.log_dir / "received"
    output_root.mkdir(parents=True, exist_ok=True)
    relay_log = args.log_dir / "relay.log"
    relay, relay_log_handle = start_relay(
        args.croc,
        args.relay_password,
        args.relay_port,
        relay_log,
    )
    try:
        selected_modes = ("auto", "relay") if args.mode == "all" else (args.mode,)
        for mode in selected_modes:
            for warmup_index in range(args.warmups):
                _rows, received, receive_dir, *_ = run_trial(
                    args,
                    mode,
                    warmup_index,
                    True,
                    args.input,
                    source_sha256,
                    output_root,
                    args.log_dir,
                )
                received.unlink()
                receive_dir.rmdir()

        per_mode = {"auto": 0, "relay": 0}
        run_modes = (
            RUN_ORDER[: args.runs_per_mode * 2]
            if args.mode == "all"
            else (args.mode,) * args.runs_per_mode
        )
        with args.output.open("a", encoding="utf-8") as output:
            for mode in run_modes:
                per_mode[mode] += 1
                rows, received, receive_dir, wall, path, evidence = run_trial(
                    args,
                    mode,
                    per_mode[mode],
                    False,
                    args.input,
                    source_sha256,
                    output_root,
                    args.log_dir,
                )
                for row in rows:
                    output.write(json.dumps(row, sort_keys=True) + "\n")
                output.flush()
                print(
                    f"croc mode={mode} size_bytes={args.input.stat().st_size} "
                    f"trial={per_mode[mode]} wall_s={wall:.3f} path={path} "
                    f"sha256={source_sha256} evidence={evidence}"
                )
                received.unlink()
                receive_dir.rmdir()
    finally:
        relay.terminate()
        try:
            relay.wait(timeout=10)
        except subprocess.TimeoutExpired:
            relay.kill()
            relay.wait()
        relay_log_handle.close()


if __name__ == "__main__":
    main()
