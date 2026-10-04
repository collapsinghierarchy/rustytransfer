import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/wasilij/rustytransfer-bench/checks-shared-key-example-20261003/final-readiness-v3")
BIN = Path("/home/wasilij/rustytransfer-bench/target/release/examples/shared_key_parallel")
SIZE = 8 * 1024 * 1024
CHUNK = 256 * 1024
sys.path.insert(0, "/mnt/c/Users/wasil/Documents/GitHub/rustytransfer/benchmarks")
import run_oracle_transfer as runner


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def make_source(path, size=SIZE):
    block = os.urandom(1024 * 1024)
    with path.open("wb") as output:
        for _ in range(size // len(block)):
            output.write(block)


def start_sender(source, identity, metrics, log, streams):
    env = os.environ.copy()
    env["RUSTYTRANSFER_BENCH_PATH_EVIDENCE"] = "1"
    env["RUSTYTRANSFER_METRICS_JSONL"] = str(metrics)
    process = subprocess.Popen(
        [str(BIN), "--transport", "iroh", "send", "--direct", "--chunk-size", str(CHUNK),
         "--streams", str(streams), "--file", str(source), "--identity-file", str(identity)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    invite = None
    lines = []
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line:
            lines.append(line)
            match = re.search(r"Direct invite:\s*(rt1:\S+)", line)
            if match:
                invite = match.group(1)
                break
        elif process.poll() is not None:
            break
    log.write_text("".join(lines), encoding="utf-8")
    if invite is None:
        process.kill()
        raise RuntimeError(f"sender did not publish invite, exit={process.poll()}")
    return process, invite


def run_success(streams, source):
    trial = ROOT / f"success-{streams}"
    trial.mkdir()
    output = trial / "received.bin"
    metrics_s = trial / "sender.jsonl"
    metrics_r = trial / "receiver.jsonl"
    sender_log = trial / "sender.log"
    receiver_log = trial / "receiver.log"
    sender, invite = start_sender(source, trial / "identity.key", metrics_s, sender_log, streams)
    env = os.environ.copy()
    env["RUSTYTRANSFER_BENCH_PATH_EVIDENCE"] = "1"
    env["RUSTYTRANSFER_METRICS_JSONL"] = str(metrics_r)
    with receiver_log.open("w", encoding="utf-8") as log:
        receiver = subprocess.Popen(
            [str(BIN), "--transport", "iroh", "recv", "--invite", invite, "--out", str(output)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )
    sender_status = sender.wait(timeout=120)
    receiver_status = receiver.wait(timeout=120)
    with sender_log.open("a", encoding="utf-8") as log:
        log.writelines(sender.stdout.readlines())
    if sender_status or receiver_status:
        raise RuntimeError(f"success-{streams}: sender={sender_status}, receiver={receiver_status}")
    expected, actual = hash_file(source), hash_file(output)
    if expected != actual:
        raise RuntimeError(f"success-{streams}: source/output SHA-256 differ")
    sender_metric = json.loads(metrics_s.read_text(encoding="utf-8").splitlines()[0])
    receiver_metric = json.loads(metrics_r.read_text(encoding="utf-8").splitlines()[0])
    for metric in (sender_metric, receiver_metric):
        if (metric["parallel_streams"], metric["payload_key_count"], metric["kem_sessions"]) != (streams, 1, 1):
            raise RuntimeError(f"wrong experiment provenance: {metric}")
        evidence = metric.get("path_evidence")
        if not isinstance(evidence, dict):
            raise RuntimeError("missing path evidence")
        strict_direct = (
            evidence.get("classification") == "direct"
            and evidence.get("verified") is True
            and evidence.get("relay_selected") is False
            and evidence.get("relay_stream_tx") == 0
            and evidence.get("relay_stream_rx") == 0
            and evidence.get("direct_stream_tx", 0) + evidence.get("direct_stream_rx", 0) > 0
        )
        metric["strict_direct_evidence"] = strict_direct
    (trial / "smoke.json").write_text(json.dumps({
        "streams": streams,
        "bytes": SIZE,
        "sha256": expected,
        "sender_status": sender_status,
        "receiver_status": receiver_status,
        "sender_path": sender_metric["path"],
        "receiver_path": receiver_metric["path"],
        "strict_direct_evidence": {
            "sender": sender_metric["strict_direct_evidence"],
            "receiver": receiver_metric["strict_direct_evidence"],
        },
        "sender_path_evidence": sender_metric["path_evidence"],
        "receiver_path_evidence": receiver_metric["path_evidence"],
    }, indent=2) + "\n", encoding="utf-8")
    redact_logs(trial)


def run_truncated_source_failure():
    trial = ROOT / "failure-truncated-source"
    trial.mkdir()
    output = trial / "received.bin"
    part = Path(str(output) + ".shared-key-part")
    failure_source = trial / "source-128mib.bin"
    make_source(failure_source, 128 * 1024 * 1024)
    sender, invite = start_sender(failure_source, trial / "identity.key", trial / "sender.jsonl", trial / "sender.log", 4)
    env = os.environ.copy()
    env["RUSTYTRANSFER_BENCH_PATH_EVIDENCE"] = "1"
    env["RUSTYTRANSFER_METRICS_JSONL"] = str(trial / "receiver.jsonl")
    with (trial / "receiver.log").open("w", encoding="utf-8") as log:
        receiver = subprocess.Popen(
            [str(BIN), "--transport", "iroh", "recv", "--invite", invite, "--out", str(output)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not part.exists() and receiver.poll() is None:
        time.sleep(0.01)
    if not part.exists():
        sender.kill()
        receiver.kill()
        raise RuntimeError("receiver never claimed its partial output")
    with failure_source.open("r+b") as damaged_source:
        damaged_source.truncate(0)
    sender_status = sender.wait(timeout=30)
    receiver_status = receiver.wait(timeout=60)
    with (trial / "sender.log").open("a", encoding="utf-8") as log:
        log.writelines(sender.stdout.readlines())
    if sender_status == 0 or receiver_status == 0 or part.exists() or output.exists():
        raise RuntimeError(f"failure cleanup invariant failed: sender={sender_status}, receiver={receiver_status}, part={part.exists()}, output={output.exists()}")
    (trial / "smoke.json").write_text(json.dumps({
        "failure_mode": "source truncated after receiver claimed partial, causing bounded sender reads to fail",
        "sender_status": sender_status,
        "receiver_status": receiver_status,
        "part_removed": not part.exists(),
        "output_absent": not output.exists(),
    }, indent=2) + "\n", encoding="utf-8")
    redact_logs(trial)


def wait_for_progress(part, source, timeout_seconds=45):
    deadline = time.monotonic() + timeout_seconds
    with source.open("rb") as expected:
        first = expected.read(1)
    if not first or first == b"\0":
        raise RuntimeError("cancellation fixture must begin with a nonzero byte")
    while time.monotonic() < deadline:
        if part.exists():
            try:
                with part.open("rb") as output:
                    if output.read(1) == first:
                        return
            except FileNotFoundError:
                pass
        time.sleep(0.01)
    raise RuntimeError("receiver did not write authenticated payload before cancellation")


def finish_sender_log(sender, path):
    with path.open("a", encoding="utf-8") as log:
        log.writelines(sender.stdout.readlines())


def stop_processes(*processes):
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def run_initial_accept_cancellation(source):
    trial = ROOT / "cancel-initial-accept"
    trial.mkdir()
    sender_log = trial / "sender.log"
    sender, _invite = start_sender(source, trial / "identity.key", trial / "sender.jsonl", sender_log, 1)
    sender.send_signal(signal.SIGTERM)
    status = sender.wait(timeout=10)
    finish_sender_log(sender, sender_log)
    if status == 0:
        raise RuntimeError("sender unexpectedly succeeded after SIGTERM while awaiting accept")
    (trial / "smoke.json").write_text(json.dumps({
        "signal": "SIGTERM",
        "phase": "initial listener accept",
        "exit_status": status,
        "exited_within_seconds": 10,
    }, indent=2) + "\n", encoding="utf-8")
    redact_logs(trial)


def run_sender_cancellation(source):
    trial = ROOT / "cancel-sender-payload"
    trial.mkdir()
    output = trial / "received.bin"
    part = Path(str(output) + ".shared-key-part")
    sender, invite = start_sender(source, trial / "identity.key", trial / "sender.jsonl", trial / "sender.log", 4)
    env = os.environ.copy()
    env["RUSTYTRANSFER_BENCH_PATH_EVIDENCE"] = "1"
    env["RUSTYTRANSFER_METRICS_JSONL"] = str(trial / "receiver.jsonl")
    with (trial / "receiver.log").open("w", encoding="utf-8") as log:
        receiver = subprocess.Popen(
            [str(BIN), "--transport", "iroh", "recv", "--invite", invite, "--out", str(output)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )
    try:
        wait_for_progress(part, source)
        sender.send_signal(signal.SIGTERM)
        sender_status = sender.wait(timeout=10)
        receiver_status = receiver.wait(timeout=40)
        finish_sender_log(sender, trial / "sender.log")
    finally:
        stop_processes(sender, receiver)
        finish_sender_log(sender, trial / "sender.log")
    if sender_status == 0 or receiver_status == 0 or part.exists() or output.exists():
        raise RuntimeError(f"sender cancellation cleanup failed: sender={sender_status}, receiver={receiver_status}, part={part.exists()}, output={output.exists()}")
    (trial / "smoke.json").write_text(json.dumps({
        "signal": "SIGTERM",
        "phase": "payload sender",
        "sender_status": sender_status,
        "receiver_status": receiver_status,
        "part_removed": not part.exists(),
        "output_absent": not output.exists(),
    }, indent=2) + "\n", encoding="utf-8")
    redact_logs(trial)


def run_receiver_cancellation(source):
    trial = ROOT / "cancel-receiver-payload"
    trial.mkdir()
    output = trial / "received.bin"
    part = Path(str(output) + ".shared-key-part")
    sender, invite = start_sender(source, trial / "identity.key", trial / "sender.jsonl", trial / "sender.log", 4)
    env = os.environ.copy()
    env["RUSTYTRANSFER_BENCH_PATH_EVIDENCE"] = "1"
    env["RUSTYTRANSFER_METRICS_JSONL"] = str(trial / "receiver.jsonl")
    with (trial / "receiver.log").open("w", encoding="utf-8") as log:
        receiver = subprocess.Popen(
            [str(BIN), "--transport", "iroh", "recv", "--invite", invite, "--out", str(output)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )
    try:
        wait_for_progress(part, source)
        receiver.send_signal(signal.SIGTERM)
        receiver_status = receiver.wait(timeout=10)
        sender_status = sender.wait(timeout=40)
        finish_sender_log(sender, trial / "sender.log")
    finally:
        stop_processes(sender, receiver)
        finish_sender_log(sender, trial / "sender.log")
    if sender_status == 0 or receiver_status == 0 or part.exists() or output.exists():
        raise RuntimeError(f"receiver cancellation cleanup failed: sender={sender_status}, receiver={receiver_status}, part={part.exists()}, output={output.exists()}")
    (trial / "smoke.json").write_text(json.dumps({
        "signal": "SIGTERM",
        "phase": "payload receiver",
        "sender_status": sender_status,
        "receiver_status": receiver_status,
        "part_removed": not part.exists(),
        "output_absent": not output.exists(),
    }, indent=2) + "\n", encoding="utf-8")
    redact_logs(trial)


def redact_logs(trial):
    for path in trial.glob("*.log"):
        runner.redact_direct_invites(path)


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        source = ROOT / "source-8mib.bin"
        make_source(source)
        for streams in (1, 4):
            run_success(streams, source)
        run_truncated_source_failure()
        cancellation_source = ROOT / "source-cancellation-512mib.bin"
        make_source(cancellation_source, 512 * 1024 * 1024)
        with cancellation_source.open("r+b") as fixture:
            fixture.write(b"\xa5")
        run_initial_accept_cancellation(source)
        run_sender_cancellation(cancellation_source)
        run_receiver_cancellation(cancellation_source)
        (ROOT / "source-sha256.txt").write_text(hash_file(source) + "  source-8mib.bin\n", encoding="utf-8")
    finally:
        for path in ROOT.rglob("*.log"):
            runner.redact_direct_invites(path)
        for path in (
            ROOT / "source-8mib.bin",
            ROOT / "source-cancellation-512mib.bin",
            ROOT / "failure-truncated-source" / "source-128mib.bin",
        ):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
