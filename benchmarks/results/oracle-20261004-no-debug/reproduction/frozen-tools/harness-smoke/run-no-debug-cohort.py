#!/usr/bin/env python3
"""Run clean direct Rustytransfer/Croc cohorts with external route evidence only.

This harness intentionally does not enable application diagnostic controls. It
uses clean-endpoint-observer.py to start sanitized endpoint processes and to
sample their live sockets. It is a measurement harness, not a production tool.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import shlex
import signal
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import run_oracle_transfer as common
from croc_firewall_lease import OracleFirewallLease
from run_croc_baseline import CapturedOutput, parse_time

SIZES_MIB = (1024, 2048, 4096)
MEASURED = 5
TIMEOUT_SECONDS = 900
TIME_FORMAT = common.TIME_FORMAT
DIRECT_PATH = re.compile(r"Selected\s+Iroh\s+data\s+path:\s*direct", re.IGNORECASE)
INVITE = common.DIRECT_INVITE
OBSERVER_LOCAL = Path("/home/wasilij/rustytransfer-bench/tools/no-debug-20261004/clean-endpoint-observer.py")
OBSERVER_SHARED = Path("/mnt/c/Users/wasil/Documents/GitHub/rustytransfer/target/clean-endpoint-observer.py")
OBSERVER_REMOTE = "/home/ubuntu/rustytransfer-bench/tools/no-debug-20261004/clean-endpoint-observer.py"
WSL_EXE = Path("/mnt/c/Windows/System32/wsl.exe")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def remote(args, command: str) -> str:
    return common.remote(args, command)


def quote(value) -> str:
    return common.remote_quote(value)


def observer_snapshot(args, *, remote_side: bool, pgid: int, executable: str) -> dict:
    helper = args.remote_observer if remote_side else str(args.observer)
    command = (
        f"python3 {quote(helper)} snapshot --pgid {int(pgid)} --exe {quote(executable)}"
    )
    raw = remote(args, command) if remote_side else subprocess.check_output(
        [sys.executable, str(args.observer), "snapshot", "--pgid", str(pgid), "--exe", executable],
        text=True,
    ).strip()
    result = json.loads(raw)
    process = result.get("process")
    if process is None:
        return result
    if process.get("pid", -1) <= 0 or process.get("pgid") != pgid:
        raise RuntimeError(f"observer returned an unexpected process identity: {process}")
    if Path(process.get("exe", "")).resolve() != Path(executable).resolve():
        raise RuntimeError(f"observer matched the wrong executable: {process}")
    if process.get("debug_flags_present") or process.get("debug_env_present"):
        raise RuntimeError(f"endpoint process had debug flags/environment: {process}")
    return result


def launch_local(args, argv, run_dir: Path, executable: Path, pid_path: Path, time_path: Path,
                 log_path: Path, env: dict[str, str] | None = None):
    command = [
        "setsid", "--wait", sys.executable, str(args.observer), "launch",
        "--pid-path", str(pid_path), "--time-path", str(time_path),
        "--exe", str(executable), "--", *map(str, argv),
    ]
    process = subprocess.Popen(
        command,
        cwd=run_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:
        raise RuntimeError("local endpoint stdout pipe was not created")
    output = CapturedOutput(process.stdout, log_path)
    return process, output


def launch_remote(args, argv, run_dir: str, executable: str, pid_path: str, time_path: str,
                  log_path: str, env: dict[str, str] | None = None):
    argv_text = " ".join(quote(item) for item in argv)
    export = ""
    if env:
        export = "env " + " ".join(f"{key}={quote(value)}" for key, value in env.items()) + " "
    remote_command = (
        f"cd -- {quote(run_dir)} && exec setsid --wait {export}python3 "
        f"{quote(args.remote_observer)} launch --pid-path {quote(pid_path)} "
        f"--time-path {quote(time_path)} --exe {quote(executable)} -- {argv_text}"
    )
    process = subprocess.Popen(
        common.ssh_command(args, remote_command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:
        raise RuntimeError("remote endpoint stdout pipe was not created")
    output = CapturedOutput(process.stdout, log_path)
    return process, output


def wait_invite(process, path: Path, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        match = INVITE.search(text)
        if match:
            return match.group(1)
        if process.poll() is not None:
            raise RuntimeError(f"Rustytransfer sender exited before producing an invite ({process.returncode})")
        time.sleep(0.05)
    raise TimeoutError("Rustytransfer direct invite did not arrive before the readiness deadline")


def wait_croc_listeners(args, process, log_path: Path, pid_path: str, run_dir: str,
                        executable: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Croc sender exited before listeners were ready ({process.returncode})")
        try:
            pid_text = remote(args, f"cat -- {quote(pid_path)}")
            pgid = int(pid_text.strip())
        except (RuntimeError, ValueError):
            time.sleep(0.1)
            continue
        record = observer_snapshot(args, remote_side=True, pgid=pgid, executable=executable)
        endpoint = record.get("process")
        if endpoint is not None:
            ports = {sock["local_port"] for sock in endpoint["tcp"] if sock["state"] == "LISTEN"}
            if ports.issuperset({9009, 9010, 9011, 9012, 9013}):
                if Path(endpoint["cwd"]) != Path(run_dir):
                    raise RuntimeError(f"Croc sender is outside its scoped trial directory: {endpoint}")
                return record
        time.sleep(0.1)
    raise TimeoutError(f"Croc 9009-9013 listeners did not become ready; see {log_path}")


def wait_for_selected_direct(processes, logs, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        texts = [path.read_text(encoding="utf-8", errors="replace") if path.exists() else "" for path in logs]
        if all(DIRECT_PATH.search(text) for text in texts):
            return True
        if any(process.poll() is not None for process in processes):
            break
        time.sleep(0.05)
    raise RuntimeError("both normal Rust CLI logs did not report Selected Iroh data path: direct")


def wait_pair(sender, receiver, timeout, sender_log, receiver_log):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sender_status, receiver_status = sender.poll(), receiver.poll()
        if sender_status is not None and receiver_status is not None:
            if sender_status or receiver_status:
                raise RuntimeError(
                    f"endpoint failure sender={sender_status} receiver={receiver_status}; "
                    f"logs={sender_log},{receiver_log}"
                )
            return
        time.sleep(0.05)
    raise TimeoutError(f"transfer exceeded {timeout}s; logs={sender_log},{receiver_log}")


def watch_exit(name, process, exits, lock, complete):
    status = process.wait()
    with lock:
        exits[name] = {"status": status, "observed_at": time.monotonic()}
        if len(exits) == 2:
            complete.set()


def start_exit_watcher(name, process, exits, lock, complete):
    thread = threading.Thread(target=watch_exit, args=(name, process, exits, lock, complete), daemon=True)
    thread.start()
    return thread


def wait_watched_pair(complete, exits, timeout, sender_log, receiver_log):
    if not complete.wait(timeout):
        raise TimeoutError(f"transfer exceeded {timeout}s; logs={sender_log},{receiver_log}")
    if any(row["status"] != 0 for row in exits.values()):
        raise RuntimeError(f"endpoint failure: {exits}; logs={sender_log},{receiver_log}")
    return max(row["observed_at"] for row in exits.values())


def terminate_local(process, pid_path: Path, run_dir: Path):
    if process is None or process.poll() is not None:
        return
    if not pid_path.is_file():
        process.terminate()
        process.wait(timeout=5)
        return
    pid = int(pid_path.read_text().strip())
    if os.getpgid(pid) != pid or Path(os.readlink(f"/proc/{pid}/cwd")).resolve() != run_dir.resolve():
        raise RuntimeError("refusing to signal a local endpoint outside its exact trial process group/cwd")
    os.killpg(pid, signal.SIGTERM)
    process.wait(timeout=10)


def terminate_remote(args, process, pid_path: str, run_dir: str):
    if process is None or process.poll() is not None:
        return
    command = (
        f"pid=$(cat -- {quote(pid_path)}); "
        "case \"$pid\" in ''|*[!0-9]*) exit 2;; esac; "
        "test \"$(ps -o pgid= -p \"$pid\" | tr -d ' ')\" = \"$pid\" || exit 3; "
        f"test \"$(readlink -f -- /proc/\"$pid\"/cwd)\" = {quote(run_dir)} || exit 4; "
        "kill -TERM -- \"-$pid\"; "
        "for i in 1 2 3 4 5 6 7 8 9 10; do "
        "kill -0 -- \"-$pid\" 2>/dev/null || exit 0; sleep 0.2; done; exit 5"
    )
    remote(args, command)
    process.wait(timeout=10)


def local_udp_capture(args, peer, port, output_path):
    command = [
        str(WSL_EXE), "-d", "Ubuntu", "-u", "root", "--", "python3",
        str(args.observer), "capture-udp", "--peer", peer, "--port", str(port),
        "--output", str(output_path), "--count", "2", "--timeout", "2",
    ]
    return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def remote_udp_capture(args, peer, port, output_path):
    command = (
        f"sudo -n python3 {quote(args.remote_observer)} capture-udp --peer {quote(peer)} "
        f"--port {port} --output {quote(output_path)} --count 2 --timeout 2"
    )
    return subprocess.Popen(common.ssh_command(args, command), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def capture_wait(process, timeout=12):
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        stdout, stderr = process.communicate(timeout=3)
        raise TimeoutError(f"UDP observer timed out: {stderr.strip()}")
    if process.returncode:
        raise RuntimeError(f"UDP observer failed: {stderr.strip()} {stdout.strip()}")
    return json.loads(stdout.strip().splitlines()[-1])


def croc_socket_evidence(sender_snapshot, receiver_snapshot, oracle_peer, wsl_peer):
    sender = sender_snapshot.get("process") or {}
    receiver = receiver_snapshot.get("process") or {}
    required = {9010, 9011, 9012, 9013}
    sender_rows = [
        sock for sock in sender.get("tcp", [])
        if sock["state"] == "ESTABLISHED" and sock["local_port"] in required
        and sock["remote_ip"] == wsl_peer
    ]
    receiver_rows = [
        sock for sock in receiver.get("tcp", [])
        if sock["state"] == "ESTABLISHED" and sock["remote_port"] in required
        and sock["remote_ip"] == oracle_peer
    ]
    by_sender = {row["local_port"]: row for row in sender_rows}
    by_receiver = {row["remote_port"]: row for row in receiver_rows}
    if set(by_sender) != required or set(by_receiver) != required:
        raise RuntimeError(
            "Croc did not expose all four direct established data sockets on both endpoints: "
            f"sender={sender_rows}; receiver={receiver_rows}"
        )
    sender_control = [sock for sock in sender.get("tcp", []) if sock["state"] == "ESTABLISHED"
                      and sock["local_port"] == 9009 and sock["remote_ip"] == wsl_peer]
    receiver_control = [sock for sock in receiver.get("tcp", []) if sock["state"] == "ESTABLISHED"
                        and sock["remote_port"] == 9009 and sock["remote_ip"] == oracle_peer]
    if len(sender_control) != 1 or len(receiver_control) != 1:
        raise RuntimeError(f"Croc control socket was not established on both endpoint processes: {sender_control}; {receiver_control}")
    return {
        "kind": "live-tcp-process-sockets",
        "verified": True,
        "data_listener_ports": sorted(required),
        "control_listener_port": 9009,
        "oracle_control_socket": sender_control[0],
        "wsl_control_socket": receiver_control[0],
        "oracle_socket_tuples": [by_sender[port] for port in sorted(required)],
        "wsl_socket_tuples": [by_receiver[port] for port in sorted(required)],
        "peer_addresses": {"oracle_observed_wsl": wsl_peer, "wsl_observed_oracle": oracle_peer},
        "nat_limitation": "Full endpoint tuples are retained; source ports are not assumed equal across NAT.",
    }


def rust_socket_evidence(sender_snapshot, receiver_snapshot, captures, sender_log, receiver_log,
                         oracle_peer, wsl_peer):
    if not DIRECT_PATH.search(sender_log) or not DIRECT_PATH.search(receiver_log):
        raise RuntimeError("normal Rust CLI did not report direct data path at both endpoints")
    sender = sender_snapshot.get("process") or {}
    receiver = receiver_snapshot.get("process") or {}
    sender_ports = sorted({sock["local_port"] for sock in sender.get("udp", []) if sock["local_port"]})
    receiver_ports = sorted({sock["local_port"] for sock in receiver.get("udp", []) if sock["local_port"]})
    if not sender_ports or not receiver_ports:
        raise RuntimeError("endpoint process snapshots did not expose live UDP socket ports")
    for side, expected_peer in (("sender", wsl_peer), ("receiver", oracle_peer)):
        samples = captures.get(side, [])
        if len(samples) != 2:
            raise RuntimeError(f"expected two successful mid-payload UDP header captures at {side}: {samples}")
        for sample in samples:
            capture = sample["result"]
            port = sample["port"]
            packets = capture.get("packets", [])
            if not capture.get("complete") or capture.get("observed_packets", 0) < 2:
                raise RuntimeError(f"incomplete mid-payload UDP sample at {side}: {capture}")
            if side == "sender":
                payload_packets = [p for p in packets if p["source_port"] == port
                                   and p["destination_ip"] == expected_peer and p["ip_bytes"] >= 1000]
            else:
                payload_packets = [p for p in packets if p["source_ip"] == expected_peer
                                   and p["destination_port"] == port and p["ip_bytes"] >= 1000]
            if len(payload_packets) < 2:
                raise RuntimeError(f"UDP capture lacked two expected peer-to-peer payload packets at {side}: {capture}")
    return {
        "kind": "normal-cli-selected-direct-plus-sampled-udp",
        "verified": True,
        "sender_udp_local_ports": sender_ports,
        "receiver_udp_local_ports": receiver_ports,
        "captures": captures,
        "coverage_limit": "Normal CLI direct-path reports plus two mid-payload kernel-filtered UDP header samples per endpoint; not continuous STREAM-frame counters.",
    }


def process_metrics(path: Path):
    return parse_time(path)


def append_json_fsync(path: Path, value: dict):
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def append_rows_fsync(path: Path, rows: list[dict]):
    with path.open("a", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def remote_json(args, path):
    return json.loads(remote(args, f"cat -- {quote(path)}"))


def clean_remote(args, paths, directories):
    root = Path(args.remote_root).resolve()
    for item in [*paths, *directories]:
        if not Path(item).resolve().is_relative_to(root):
            raise RuntimeError(f"refusing remote cleanup outside benchmark root: {item}")
    if paths:
        remote(args, "rm -- " + " ".join(quote(path) for path in paths))
    if directories:
        remote(args, "rmdir -- " + " ".join(quote(path) for path in directories))


def rust_argv(source):
    return ["--transport", "iroh", "send", "--direct", "--file", str(source)]


def croc_sender_argv(source):
    return ["--local", "--no-compress", "--disable-clipboard", "--ignore-stdin",
            "send", "--transport", "auto", "--port", "9009", "--transfers", "4", str(source)]


def check_transfer_logs(transport, sender_log, receiver_log):
    sender_text = sender_log.read_text(encoding="utf-8", errors="replace")
    receiver_text = receiver_log.read_text(encoding="utf-8", errors="replace")
    for log_path, text in ((sender_log, sender_text), (receiver_log, receiver_text)):
        if re.search(r"(--debug|--verbose|--trace|RUSTYTRANSFER_BENCH_|RUSTYTRANSFER_METRICS_JSONL|CROC_BENCH_PHASE_PROFILE)", text):
            raise RuntimeError(f"debug/profile control appeared in endpoint output at {log_path}")
    if transport == "rustytransfer":
        return sender_text, receiver_text
    return sender_text, receiver_text


def run_trial(args, lease_args, size_mib, source, expected_hash, transport, trial_index,
              warmup, output_root):
    expected_size = size_mib * 1024 * 1024
    tag = "warmup" if warmup else f"{trial_index:02d}"
    trial_id = f"{size_mib}mib-{transport}-{tag}-{uuid.uuid4().hex[:8]}"
    trial_dir = output_root / "trials" / trial_id
    trial_dir.mkdir(parents=True, exist_ok=False)
    local_work = trial_dir / "endpoint-work"
    local_work.mkdir()
    local_out = trial_dir / "out"
    local_out.mkdir()
    received = local_out / source.name
    local_pid = local_work / "receiver.pid"
    local_time = local_work / "receiver.time.json"
    local_log = trial_dir / "receiver.log"
    sender_log = trial_dir / "sender.log"
    remote_dir = f"{args.remote_root}/{trial_id}"
    remote(args, f"mkdir -- {quote(remote_dir)}")
    remote_source = f"{args.remote_input_dir}/input-{size_mib}.bin"
    remote_time = f"{remote_dir}/sender.time.json"
    remote_pid = f"{remote_dir}/sender.pid"
    remote_caps = []
    local_caps = []
    sender = receiver = None
    sender_output = receiver_output = None
    exit_times = {}
    exit_lock = threading.Lock()
    pair_exited = threading.Event()
    watchers = []
    started = None
    ready_observed = receiver_launch = pair_exit = None
    snapshots = {"sender": [], "receiver": []}
    captures = {"sender": [], "receiver": []}
    lease = None
    lease_entered = False
    failure = None
    source_peer_ip = None
    try:
        if transport == "croc":
            lease = OracleFirewallLease(lease_args, trial_dir / "firewall-lease.json", lease_seconds=1200)
            lease.__enter__()
            lease_entered = True
            source_peer_ip = lease.cidr.split("/", 1)[0]
        if transport == "rustytransfer":
            sender_argv = rust_argv(remote_source)
        else:
            sender_argv = croc_sender_argv(remote_source)
        started = time.monotonic()
        sender, sender_output = launch_remote(
            args, sender_argv, remote_dir, args.remote_rusty if transport == "rustytransfer" else args.remote_croc,
            remote_pid, remote_time, sender_log,
            {"CROC_SECRET": lease_args.croc_secret, "CROC_PASS": "pass123"} if transport == "croc" else None,
        )
        watchers.append(start_exit_watcher("sender", sender, exit_times, exit_lock, pair_exited))

        if transport == "rustytransfer":
            invite = wait_invite(sender, sender_log, args.timeout)
            ready_observed = time.monotonic()
            receiver_argv = ["--transport", "iroh", "recv", "--invite", invite,
                             "--out", str(received)]
            local_exe = args.local_rusty
        else:
            initial = wait_croc_listeners(
                args, sender, sender_log, remote_pid, remote_dir, args.remote_croc, args.timeout,
            )
            snapshots["sender"].append(initial)
            ready_observed = time.monotonic()
            receiver_argv = ["--yes", "--overwrite", "--no-compress",
                             "--disable-clipboard", "--ignore-stdin", "--ip", f"{args.host}:9009",
                             "--out", str(local_out)]
            local_exe = args.local_croc

        receiver_launch = time.monotonic()
        local_env = os.environ.copy()
        if transport == "croc":
            local_env["CROC_SECRET"] = lease_args.croc_secret
            local_env["CROC_PASS"] = "pass123"
        receiver, receiver_output = launch_local(
            args, receiver_argv, local_work, local_exe, local_pid, local_time, local_log, local_env,
        )
        watchers.append(start_exit_watcher("receiver", receiver, exit_times, exit_lock, pair_exited))

        sender_pid = int(remote(args, f"cat -- {quote(remote_pid)}").strip())
        endpoint_deadline = started + args.timeout
        if transport == "croc":
            while time.monotonic() < endpoint_deadline and not pair_exited.is_set():
                sender_snapshot = observer_snapshot(
                    args, remote_side=True, pgid=sender_pid, executable=args.remote_croc,
                )
                receiver_pid = int(local_pid.read_text().strip()) if local_pid.exists() else None
                receiver_snapshot = (
                    observer_snapshot(args, remote_side=False, pgid=receiver_pid, executable=str(local_exe))
                    if receiver_pid else {"process": None}
                )
                if sender_snapshot.get("process") is not None:
                    snapshots["sender"].append(sender_snapshot)
                if receiver_snapshot.get("process") is not None:
                    snapshots["receiver"].append(receiver_snapshot)
                if sender_snapshot.get("process") and receiver_snapshot.get("process"):
                    try:
                        evidence = croc_socket_evidence(
                            sender_snapshot, receiver_snapshot, args.oracle_peer, source_peer_ip,
                        )
                        break
                    except RuntimeError:
                        pass
                time.sleep(0.25)
        else:
            for offset in args.sample_offsets:
                remaining = receiver_launch + offset - time.monotonic()
                if remaining > 0 and pair_exited.wait(remaining):
                    break
                if pair_exited.is_set():
                    break
                sender_snapshot = observer_snapshot(
                    args, remote_side=True, pgid=sender_pid, executable=args.remote_rusty,
                )
                receiver_pid = int(local_pid.read_text().strip()) if local_pid.exists() else None
                receiver_snapshot = (
                    observer_snapshot(args, remote_side=False, pgid=receiver_pid, executable=str(local_exe))
                    if receiver_pid else {"process": None}
                )
                if sender_snapshot.get("process") is None or receiver_snapshot.get("process") is None:
                    break
                snapshots["sender"].append(sender_snapshot)
                snapshots["receiver"].append(receiver_snapshot)
                sender_udp = [sock["local_port"] for sock in sender_snapshot["process"].get("udp", []) if sock["local_port"]]
                receiver_udp = [sock["local_port"] for sock in receiver_snapshot["process"].get("udp", []) if sock["local_port"]]
                if sender_udp and receiver_udp:
                    observed = time.monotonic()
                    actual_offset = observed - receiver_launch
                    local_cap_path = trial_dir / f"udp-sample-{offset:g}s-receiver.json"
                    remote_cap_path = f"{remote_dir}/udp-sample-{offset:g}s-sender.json"
                    local_caps.append(local_udp_capture(args, args.oracle_peer, receiver_udp[0], local_cap_path))
                    remote_caps.append(remote_udp_capture(args, args.wsl_public_peer, sender_udp[0], remote_cap_path))
                    captures["receiver"].append({"sample_offset_seconds": actual_offset, "path": str(local_cap_path), "port": receiver_udp[0]})
                    captures["sender"].append({"sample_offset_seconds": actual_offset, "path": remote_cap_path, "port": sender_udp[0]})

        pair_exit = wait_watched_pair(
            pair_exited, exit_times, max(1, endpoint_deadline - time.monotonic()), sender_log, local_log,
        )
        sender_output.wait()
        receiver_output.wait()
        for cap in (*local_caps, *remote_caps):
            if cap.poll() is None:
                cap.terminate()
        for cap in local_caps:
            cap.wait(timeout=3)
        for cap in remote_caps:
            cap.wait(timeout=3)
        if transport == "rustytransfer":
            wait_for_selected_direct((sender, receiver), (sender_log, local_log))
            for item in captures["receiver"]:
                item["result"] = json.loads(Path(item["path"]).read_text(encoding="utf-8"))
            for item in captures["sender"]:
                item["result"] = remote_json(args, item["path"])
            evidence = rust_socket_evidence(
                snapshots["sender"][-1], snapshots["receiver"][-1],
                captures,
                sender_log.read_text(encoding="utf-8", errors="replace"),
                local_log.read_text(encoding="utf-8", errors="replace"),
                args.oracle_peer, args.wsl_public_peer,
            )
        else:
            if "evidence" not in locals():
                raise RuntimeError("Croc direct TCP data-socket proof was not captured during payload")

        if transport == "rustytransfer":
            sender_log.write_text(common.DIRECT_INVITE_TOKEN.sub("<redacted>", sender_log.read_text(encoding="utf-8")), encoding="utf-8")
        else:
            sender_log.write_text(sender_log.read_text(encoding="utf-8").replace(lease_args.croc_secret, "<redacted>"), encoding="utf-8")
        local_log.write_text(local_log.read_text(encoding="utf-8", errors="replace").replace(
            lease_args.croc_secret if transport == "croc" else "\0", "<redacted>"), encoding="utf-8")
        sender_text, receiver_text = check_transfer_logs(transport, sender_log, local_log)

        sender_report = remote_json(args, remote_time)
        receiver_report = process_metrics(local_time)
        source_hash = expected_hash
        received_hash = common.verify_local_file(received, expected_size, source_hash)
        if transport == "croc":
            evidence["sender_snapshots"] = snapshots["sender"]
            evidence["receiver_snapshots"] = snapshots["receiver"]
        row_common = {
            "schema_version": 1,
            "transport": transport,
            "transport_mode": "normal-direct-no-debug",
            "path": "direct",
            "path_evidence": evidence,
            "size_bytes": expected_size,
            "source_sha256": source_hash,
            "received_sha256": received_hash,
            "success": True,
            "warmup": warmup,
            "run_index": trial_index,
            "wall_seconds": pair_exit - started,
            "benchmark_timing": {
                "clock": "runner-monotonic",
                "ready_observed_seconds": ready_observed - started,
                "receiver_launch_seconds": receiver_launch - started,
                "pair_exit_observed_seconds": pair_exit - started,
                "sender_exit_observed_seconds": exit_times["sender"]["observed_at"] - started,
                "receiver_exit_observed_seconds": exit_times["receiver"]["observed_at"] - started,
                "scope": "first endpoint launch through both endpoint exits; includes readiness and external observation work",
            },
            "sender_process_seconds": sender_report.get("process_seconds"),
            "receiver_process_seconds": receiver_report.get("process_seconds"),
            "sender_cpu_seconds": sender_report["user_cpu_seconds"] + sender_report["system_cpu_seconds"],
            "receiver_cpu_seconds": receiver_report["user_cpu_seconds"] + receiver_report["system_cpu_seconds"],
            "sender_max_rss_kib": sender_report["max_rss_kib"],
            "receiver_max_rss_kib": receiver_report["max_rss_kib"],
            "effective_mib_per_second": size_mib / (pair_exit - started),
            "phase_timings": None,
            "debug_flags_present": False,
            "debug_environment_present": False,
            "app_metrics_enabled": False,
            "local_binary_sha256": args.local_rusty_sha256 if transport == "rustytransfer" else args.local_croc_sha256,
            "remote_binary_sha256": args.remote_rusty_sha256 if transport == "rustytransfer" else args.remote_croc_sha256,
            "local_binary_path": str(args.local_rusty if transport == "rustytransfer" else args.local_croc),
            "remote_binary_path": args.remote_rusty if transport == "rustytransfer" else args.remote_croc,
        }
        rows = [dict(row_common, role=role) for role in ("sender", "receiver")]
        local_time_json = trial_dir / "receiver.time.json"
        local_time_json.write_text(json.dumps(receiver_report, indent=2) + "\n", encoding="utf-8")
        (trial_dir / "sender.time.json").write_text(json.dumps(sender_report, indent=2) + "\n", encoding="utf-8")
        (trial_dir / "socket-snapshots.json").write_text(json.dumps(snapshots, indent=2) + "\n", encoding="utf-8")
        if transport == "croc":
            remote_caps = []
        append_rows_fsync(args.raw_jsonl, rows)
        if received.exists():
            received.unlink()
        local_out.rmdir()
        return rows
    except Exception as error:
        failure = error
        (trial_dir / "socket-snapshots.json").write_text(json.dumps(snapshots, indent=2) + "\n", encoding="utf-8")
        args.errors_path.parent.mkdir(parents=True, exist_ok=True)
        with args.errors_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"trial": trial_id, "transport": transport, "size_mib": size_mib,
                                    "warmup": warmup, "run_index": trial_index, "error": str(error),
                                    "sender_log": str(sender_log), "receiver_log": str(local_log)}) + "\n")
        raise
    finally:
        cleanup_errors = []
        if sender is not None and sender.poll() is None:
            try:
                terminate_remote(args, sender, remote_pid, remote_dir)
            except Exception as cleanup_error:
                cleanup_errors.append(f"remote endpoint cleanup failed: {cleanup_error}")
        if receiver is not None and receiver.poll() is None:
            try:
                terminate_local(receiver, local_pid, local_work)
            except Exception as cleanup_error:
                cleanup_errors.append(f"local endpoint cleanup failed: {cleanup_error}")
        for cap in (*local_caps, *remote_caps):
            if cap.poll() is None:
                cap.terminate()
            try:
                cap.wait(timeout=3)
            except Exception as cleanup_error:
                cleanup_errors.append(f"external UDP observer did not stop: {cleanup_error}")
        for output in (sender_output, receiver_output):
            if output is not None:
                try:
                    process = sender if output is sender_output else receiver
                    if process is None or process.poll() is not None:
                        output.wait()
                except Exception:
                    pass
        for process, label in ((sender, "remote"), (receiver, "local")):
            if process is not None and process.poll() is None:
                cleanup_errors.append(f"{label} endpoint remains live after scoped cleanup attempt")
        if transport == "rustytransfer":
            common.redact_direct_invites(sender_log)
            common.redact_direct_invites(local_log)
        else:
            common.redact(sender_log, lease_args.croc_secret)
            common.redact(local_log, lease_args.croc_secret)
        if lease is not None and lease_entered:
            try:
                lease.__exit__(type(failure) if failure else None, failure, failure.__traceback__ if failure else None)
            except Exception as cleanup_error:
                cleanup_errors.append(f"firewall cleanup failed: {cleanup_error}")
        # Endpoint logs and socket evidence stay local for audit. Remove only
        # generated remote transient files after successful verification.
        if pair_exit is not None:
            try:
                remote_files = [remote_time, remote_pid]
                if transport == "rustytransfer":
                    remote_files.extend(item["path"] for item in captures["sender"])
                clean_remote(args, remote_files, [remote_dir])
            except Exception as cleanup_error:
                cleanup_errors.append(f"remote trial artifact cleanup failed: {cleanup_error}")
        if cleanup_errors:
            for cleanup_error in cleanup_errors:
                append_json_fsync(args.errors_path, {"trial": trial_id, "cleanup_error": cleanup_error})
            if failure is not None:
                failure.add_note("cleanup issues: " + "; ".join(cleanup_errors))
            else:
                raise RuntimeError("; ".join(cleanup_errors))


def summarize(rows):
    grouped = {}
    for row in rows:
        if row.get("warmup"):
            continue
        grouped.setdefault((row["transport"], row["size_bytes"]), []).append(row)
    result = []
    for (transport, size_bytes), group in sorted(grouped.items()):
        rates = [row["effective_mib_per_second"] for row in group if row["role"] == "sender"]
        result.append({
            "transport": transport,
            "size_bytes": size_bytes,
            "measured_runs": len(rates),
            "arithmetic_mean_mib_per_second": statistics.mean(rates),
            "median_mib_per_second": statistics.median(rates),
            "min_mib_per_second": min(rates),
            "max_mib_per_second": max(rates),
            "mad_mib_per_second": statistics.median(abs(value-statistics.median(rates)) for value in rates),
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="141.147.1.21")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--scp", default="scp")
    parser.add_argument("--ssh-key", required=True, type=Path)
    parser.add_argument("--observer", type=Path, default=OBSERVER_LOCAL)
    parser.add_argument("--remote-observer", default=OBSERVER_REMOTE)
    parser.add_argument("--endpoint-cwd", required=True, type=Path)
    parser.add_argument("--remote-root", default="/home/ubuntu/rustytransfer-bench/no-debug-cohort")
    parser.add_argument("--remote-input-dir", required=True)
    parser.add_argument("--local-input-dir", required=True, type=Path)
    parser.add_argument("--local-rusty", required=True, type=Path)
    parser.add_argument("--remote-rusty", required=True)
    parser.add_argument("--local-croc", required=True, type=Path)
    parser.add_argument("--remote-croc", required=True)
    parser.add_argument("--local-rusty-sha256", required=True)
    parser.add_argument("--remote-rusty-sha256", required=True)
    parser.add_argument("--local-croc-sha256", required=True)
    parser.add_argument("--remote-croc-sha256", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS)
    parser.add_argument("--sizes-mib", "--sizes", dest="sizes_mib", default="1024,2048,4096", help="comma-separated subset of 512,1024,2048,4096")
    parser.add_argument("--runs", "--measured", dest="runs", type=int, default=MEASURED, help="measured pairs per tool and size (1..5)")
    parser.add_argument("--sample-offsets", default="3,10", help="Rust UDP header samples in seconds after receiver launch")
    args = parser.parse_args()
    if args.output_root.exists():
        parser.error("output root must be new")
    if not args.ssh_key.is_file() or not args.observer.is_file() or not OBSERVER_SHARED.is_file() or not args.endpoint_cwd.is_dir():
        parser.error("SSH key, observer helper, or native endpoint working directory is missing")
    if not args.local_input_dir.is_dir() or not args.local_rusty.is_file() or not args.local_croc.is_file():
        parser.error("local fixture or endpoint binary is missing")
    if not 1 <= args.timeout <= TIMEOUT_SECONDS:
        parser.error(f"timeout must be 1..{TIMEOUT_SECONDS} seconds")
    try:
        args.sizes_mib = tuple(int(item) for item in args.sizes_mib.split(","))
        args.sample_offsets = tuple(float(item) for item in args.sample_offsets.split(","))
    except ValueError:
        parser.error("sizes and sample offsets must be comma-separated numbers")
    if not args.sizes_mib or len(set(args.sizes_mib)) != len(args.sizes_mib) or any(size not in (512, *SIZES_MIB) for size in args.sizes_mib):
        parser.error("sizes must be a unique comma-separated subset of 512,1024,2048,4096 MiB")
    if not 1 <= args.runs <= MEASURED:
        parser.error(f"runs must be within 1..{MEASURED}")
    if not args.sample_offsets or tuple(sorted(set(args.sample_offsets))) != args.sample_offsets or any(offset <= 0 for offset in args.sample_offsets):
        parser.error("sample offsets must be strictly increasing positive seconds")
    for name, value in (("local Rust", args.local_rusty_sha256), ("remote Rust", args.remote_rusty_sha256),
                        ("local Croc", args.local_croc_sha256), ("remote Croc", args.remote_croc_sha256)):
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            parser.error(f"{name} SHA256 must be 64 lowercase hex characters")
    root = Path(args.remote_root)
    if posixpath.commonpath(("/home/ubuntu/rustytransfer-bench", posixpath.normpath(args.remote_root))) != "/home/ubuntu/rustytransfer-bench":
        parser.error("remote root must remain inside the benchmark home")
    args.output_root.mkdir(parents=True)
    (args.output_root / "trials").mkdir()
    args.raw_jsonl = args.output_root / "raw.jsonl"
    args.errors_path = args.output_root / "errors.jsonl"
    args.errors_path.touch(exist_ok=False)
    args.remote_observer = str(args.remote_observer)
    remote(args, f"test -x {quote(args.remote_rusty)} && test -x {quote(args.remote_croc)} && test -f {quote(args.remote_observer)} && mkdir -p -- {quote(args.remote_root)}")
    for local, remote_path, expected_hash, label in (
        (args.local_rusty, args.remote_rusty, args.local_rusty_sha256, "local Rust"),
        (args.local_croc, args.remote_croc, args.local_croc_sha256, "local Croc"),
    ):
        if sha256_file(local) != expected_hash:
            parser.error(f"{label} executable hash differs from frozen expected hash")
    remote_hashes = {
        args.remote_rusty: common.remote_sha256(args, args.remote_rusty),
        args.remote_croc: common.remote_sha256(args, args.remote_croc),
    }
    if remote_hashes[args.remote_rusty] != args.remote_rusty_sha256 or remote_hashes[args.remote_croc] != args.remote_croc_sha256:
        parser.error("remote endpoint binary hash differs from frozen expected hash")
    try:
        args.oracle_peer = str(ipaddress.IPv4Address(args.host))
        args.wsl_public_peer = remote(args, "printenv SSH_CONNECTION").split()[0]
        args.wsl_public_peer = str(ipaddress.IPv4Address(args.wsl_public_peer))
    except (ValueError, IndexError) as error:
        parser.error(f"benchmark host/SSH peer must expose literal IPv4 addresses: {error}")
    versions = {
        "local_rusty": subprocess.check_output([str(args.local_rusty), "--version"], text=True).strip(),
        "local_croc": subprocess.check_output([str(args.local_croc), "--version"], text=True).strip(),
        "remote_rusty": remote(args, f"{quote(args.remote_rusty)} --version"),
        "remote_croc": remote(args, f"{quote(args.remote_croc)} --version"),
    }
    if "croc version 11.5.4" not in versions["local_croc"] or "croc version 11.5.4" not in versions["remote_croc"]:
        parser.error(f"expected official Croc 11.5.4 assets, got {versions}")
    remote_inputs = {size: f"{args.remote_input_dir}/input-{size}.bin" for size in args.sizes_mib}
    fixture_hashes = {}
    for size in args.sizes_mib:
        path = args.local_input_dir / f"input-{size}.bin"
        expected_size = size * 1024 * 1024
        if not path.is_file() or path.stat().st_size != expected_size:
            parser.error(f"local fixture size mismatch: {path}")
        digest = sha256_file(path)
        remote_out = remote(args, f"stat -c %s -- {quote(remote_inputs[size])} && sha256sum -- {quote(remote_inputs[size])}")
        remote_lines = remote_out.splitlines()
        if int(remote_lines[0]) != expected_size or remote_lines[1].split()[0] != digest:
            parser.error(f"pre-staged fixture mismatch at {size} MiB")
        fixture_hashes[str(size)] = {"sha256": digest, "bytes": expected_size,
                                     "local": str(path), "remote": remote_inputs[size]}

    inventory = {
        "schema_version": 1,
        "measurement": "clean direct Rustytransfer vs official Croc; no debug/profile controls",
        "direction": "Oracle-to-WSL",
        "host": args.host,
        "sizes_mib": list(args.sizes_mib),
        "warmups_per_tool_size": 1,
        "measured_per_tool_size": args.runs,
        "order": "warmup both transports; alternate candidate first on measured trials",
        "timeout_seconds": args.timeout,
        "local_binary_sha256": {"rustytransfer": args.local_rusty_sha256, "croc": args.local_croc_sha256},
        "remote_binary_sha256": {"rustytransfer": args.remote_rusty_sha256, "croc": args.remote_croc_sha256},
        "versions": versions,
        "fixtures": fixture_hashes,
        "observer_helper": str(args.observer),
        "remote_observer_helper": args.remote_observer,
        "observer_sha256": {
            "local_native": sha256_file(args.observer),
            "local_shared": sha256_file(OBSERVER_SHARED),
            "remote": common.remote_sha256(args, args.remote_observer),
        },
        "harness_dependencies_sha256": {
            "runner": sha256_file(Path(common.__file__)),
            "croc_firewall_lease": sha256_file(Path(__file__).with_name("croc_firewall_lease.py")),
            "croc_log_helpers": sha256_file(Path(__file__).with_name("run_croc_baseline.py")),
            "summarizer": sha256_file(Path(__file__).with_name("summarize.py")),
            "this_script": sha256_file(Path(__file__)),
        },
        "debug_controls": "observer strips all Rustytransfer benchmark vars, metrics JSONL, Rust log/backtrace controls, Croc phase profile and proxy vars; launch rejects debug argv",
        "route_limitations": "Croc proof is four actual ESTABLISHED TCP data sockets per endpoint, matched by listener ports and expected peers with NAT-aware tuple preservation. Rust proof is normal direct-path output plus two sampled bulk UDP header captures per endpoint; continuous STREAM-frame counters are unavailable without debug controls.",
    }
    (args.output_root / "manifest.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    helper_hashes = inventory["observer_sha256"]
    if len(set(helper_hashes.values())) != 1:
        parser.error(f"observer helper copies have different hashes: {helper_hashes}")
    print(json.dumps({"manifest": str(args.output_root / "manifest.json"), "versions": versions,
                      "fixture_hashes": fixture_hashes}, indent=2))

    lease_args = argparse.Namespace(**vars(args))
    lease_args.croc_secret = ""
    schedule = []
    scored = []
    run_id = uuid.uuid4().hex[:10]
    session_remote_root = f"{args.remote_root}/cohort-{run_id}"
    remote(args, f"mkdir -- {quote(session_remote_root)}")
    args.remote_root = session_remote_root
    cohort_complete = False
    try:
        for size in args.sizes_mib:
            source = args.local_input_dir / f"input-{size}.bin"
            source_hash = fixture_hashes[str(size)]["sha256"]
            for transport in ("rustytransfer", "croc"):
                lease_args.croc_secret = f"rt-clean-{size}-{uuid.uuid4().hex[:16]}"
                rows = run_trial(args, lease_args, size, source, source_hash, transport, 0, True, args.output_root)
                entry = {"size_mib": size, "transport": transport, "warmup": True, "run_index": 0}
                schedule.append(entry)
                append_json_fsync(args.output_root / "schedule.jsonl", entry)
                scored.extend(rows)
            for trial in range(1, args.runs + 1):
                order = ("rustytransfer", "croc") if trial % 2 else ("croc", "rustytransfer")
                for transport in order:
                    lease_args.croc_secret = f"rt-clean-{size}-{trial}-{uuid.uuid4().hex[:12]}"
                    rows = run_trial(args, lease_args, size, source, source_hash, transport, trial, False, args.output_root)
                    entry = {"size_mib": size, "transport": transport, "warmup": False, "run_index": trial}
                    schedule.append(entry)
                    append_json_fsync(args.output_root / "schedule.jsonl", entry)
                    scored.extend(rows)
        (args.output_root / "schedule.json").write_text(json.dumps(schedule, indent=2) + "\n", encoding="utf-8")
        (args.output_root / "summary.json").write_text(json.dumps(summarize(scored), indent=2) + "\n", encoding="utf-8")
        cohort_complete = True
    finally:
        # Keep outputs local; remove only the empty scoped remote root after all
        # per-trial endpoint files have been verified and cleaned.
        if cohort_complete:
            remote(args, f"rmdir -- {quote(session_remote_root)}")
        else:
            print(f"Incomplete cohort remote evidence retained at {session_remote_root}", file=sys.stderr)


if __name__ == "__main__":
    main()
