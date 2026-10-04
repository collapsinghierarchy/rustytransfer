#!/usr/bin/env python3
"""Maintained Rustytransfer-only, paired local transfer-core benchmark."""
import argparse
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import statistics
import subprocess
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
HARNESS = "tests/local_transport_regressions.rs"


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def run(command, *, cwd=ROOT, env=None, log=None, timeout=120):
    """On timeout, terminate the entire build/test process group on Linux."""
    with tempfile.TemporaryFile() as capture:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=capture,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
            raise RuntimeError(f"timeout: {command}") from None
        finally:
            capture.seek(0)
            output = capture.read().decode("utf-8", errors="replace")
            if log:
                log.write_text(output)
        if process.returncode:
            raise RuntimeError(f"command failed ({process.returncode}): {command}; {log or output[-2000:]}")
        return output


def resolve(ref):
    if not ref or not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise ValueError("baseline and candidate refs must be full 40-character commit SHAs")
    if run(["git", "rev-parse", f"{ref}^{{commit}}"]).strip() != ref:
        raise ValueError("reference does not identify the requested commit")
    return ref


def trial_environment():
    # Eliminate inherited profiling/tuning and Rust build overrides for both arms.
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("RUSTYTRANSFER_", "CARGO_"))
            and k not in ("RUSTFLAGS", "RUSTDOCFLAGS", "RUSTUP_TOOLCHAIN")}


def fixture(path, size_mib):
    """Counter-mode SHAKE256 stream, deterministic and practically incompressible."""
    with path.open("xb") as stream:
        for counter in range(size_mib):
            stream.write(hashlib.shake_256(
                b"rustytransfer-performance-v1\0" + counter.to_bytes(8, "little")
            ).digest(1024 * 1024))
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest(path),
            "generator": "SHAKE256 counter stream v1"}


def schedule(pairs):
    yield 0, True, ("baseline", "candidate")
    for pair in range(1, pairs + 1):
        yield pair, False, (("baseline", "candidate") if pair % 2
                            else ("candidate", "baseline"))


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def validate(pair, rows, expected, version):
    if pair.get("harness_version") != version or not positive(pair.get("elapsed_seconds")):
        raise ValueError("incompatible harness or invalid elapsed timing")
    if pair.get("received_size_bytes") != expected["bytes"]:
        raise ValueError("received size mismatch")
    if any(pair.get(k) != expected["sha256"] for k in ("source_sha256", "received_sha256")):
        raise ValueError("received/source hash mismatch")
    if len(rows) != 2 or {row.get("role") for row in rows} != {"sender", "receiver"}:
        raise ValueError("missing or duplicated endpoint rows")
    for row in rows:
        if (row.get("success") is not True or row.get("transport") != "iroh"
                or row.get("path") != "direct" or row.get("path_start") != "direct"
                or row.get("path_end") != "direct" or row.get("direct_route_verified_both") is not True
                or row.get("size_bytes") != expected["bytes"]
                or row.get("bytes_transferred") != expected["bytes"]
                or row.get("chunk_size") != 262144 or row.get("pipeline_depth") != 1
                or row.get("profile_mode") != "standard"
                or any(row.get(k) != expected["sha256"] for k in ("source_sha256", "received_sha256"))):
            raise ValueError("invalid transfer row")
        evidence = row.get("path_evidence") or {}
        if (evidence.get("verified") is not True or evidence.get("classification") != "direct"
                or not (evidence.get("direct_stream_tx", 0) > 0 or evidence.get("direct_stream_rx", 0) > 0)
                or any(evidence.get(k) != 0 for k in ("relay_stream_tx", "relay_stream_rx"))
                or any(evidence.get(k) is not False for k in
                       ("lagged", "missing_path_stats", "relay_selected"))):
            raise ValueError("missing direct STREAM evidence")
        if not positive(row.get("payload_seconds")) or any(
            type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) or row[k] < 0
            for k in ("handshake_seconds", "shutdown_seconds")
        ):
            raise ValueError("invalid endpoint phase timing")
        if row["payload_seconds"] > pair["elapsed_seconds"]:
            raise ValueError("phase exceeds elapsed pair timer")


def describe(values):
    center = statistics.median(values)
    return {"median": center, "min": min(values), "max": max(values),
            "mad": statistics.median(abs(x - center) for x in values)}


def compare(trials, config):
    results = []
    for size in config["sizes_mib"]:
        measured = [t for t in trials if t["size_mib"] == size and not t["warmup"]]
        if len(measured) != config["measured_pairs"] * 2:
            raise ValueError("missing measured trials")
        ratios, elapsed_ratios = [], []
        for pair in range(1, config["measured_pairs"] + 1):
            arms = [t for t in measured if t["pair_index"] == pair]
            if len(arms) != 2 or {t["build"] for t in arms} != {"baseline", "candidate"}:
                raise ValueError("invalid measured pair")
            baseline = next(t for t in arms if t["build"] == "baseline")
            candidate = next(t for t in arms if t["build"] == "candidate")
            # Sender payload is the scored phase; endpoint spans overlap.
            ratios.append(baseline["sender_payload_seconds"] / candidate["sender_payload_seconds"])
            elapsed_ratios.append(baseline["elapsed_seconds"] / candidate["elapsed_seconds"])
        distribution = sorted(statistics.median(sample) for sample in
                              itertools.product(ratios, repeat=len(ratios)))
        low = distribution[int(0.025 * (len(distribution) - 1))]
        high = distribution[int(0.975 * (len(distribution) - 1))]
        stats = describe(ratios)
        limit = 1 - config["regression_threshold"]
        unstable = (stats["max"] - stats["min"]) / stats["median"] > config["maximum_relative_spread"]
        decision = ("inconclusive" if unstable else "regression-observed" if high < limit
                    else "inconclusive" if low < limit else "no-regression-observed")
        results.append({"size_mib": size, "paired_payload_rate_ratios": ratios,
                        "payload_rate_ratio": stats, "bootstrap_median_95pct": [low, high],
                        "paired_elapsed_rate_ratios": elapsed_ratios,
                        "elapsed_rate_ratio": describe(elapsed_ratios), "decision": decision,
                        "baseline_sender_mib_s": describe([size / t["sender_payload_seconds"]
                            for t in measured if t["build"] == "baseline"]),
                        "candidate_sender_mib_s": describe([size / t["sender_payload_seconds"]
                            for t in measured if t["build"] == "candidate"])})
    return results


def build(ref, label, output, environment, config):
    source = output / "sources" / label
    source.mkdir(parents=True)
    archive = output / f"{label}-source.tar"
    run(["git", "archive", "--format=tar", f"--output={archive}", ref])
    with tarfile.open(archive) as bundle:
        bundle.extractall(source, filter="data")
    archive.unlink()
    target = output / "builds" / label
    env = {**environment, "CARGO_TARGET_DIR": str(target)}
    command = ["cargo", f"+{config['rust_toolchain']}", "test", "-p", "rustytransfer",
               "--release", "--locked", "--test", "local_transport_regressions",
               "--no-run", "--message-format=json"]
    text = run(command, cwd=source, env=env, log=output / f"build-{label}.log",
               timeout=config["build_timeout_seconds"])
    binaries = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("reason") == "compiler-artifact" and item.get("executable") and item["target"]["name"] == "local_transport_regressions":
            binaries.append(item["executable"])
    if len(binaries) != 1:
        raise ValueError("build did not produce exactly one harness executable")
    binary = Path(binaries[0])
    return {"commit": ref, "source_tree": run(["git", "rev-parse", f"{ref}^{{tree}}"]).strip(),
            "binary": str(binary), "binary_sha256": digest(binary),
            "harness_sha256": digest(source / HARNESS),
            "cargo_lock_sha256": digest(source / "Cargo.lock"), "command": command}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["ci"], required=True)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--candidate-ref", help="full SHA; defaults to committed HEAD")
    parser.add_argument("--calibration", choices=["aa", "slow"],
                        help="explicit same-revision comparison; slow adds 2 ms per sender progress callback")
    parser.add_argument("--output", type=Path, help="fresh output directory; builds retained, owned sources/fixtures/received files removed")
    args = parser.parse_args()
    config = json.loads((ROOT / "benchmarks/baseline.json").read_text())
    baseline = resolve(args.baseline_ref)
    candidate = resolve(args.candidate_ref or run(["git", "rev-parse", "HEAD"]).strip())
    if baseline == candidate and not args.calibration:
        parser.error("candidate equals baseline; only explicit calibration permits A/A")
    if args.calibration and baseline != candidate:
        parser.error("calibration requires identical source revisions")
    if not args.calibration and baseline != config["baseline_ref"]:
        parser.error("baseline differs from reviewed baseline.json; update configuration explicitly")
    output = (args.output or ROOT / "target/performance" / time.strftime("%Y%m%d-%H%M%S")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    environment = trial_environment()
    report = {"schema_version": 1, "scope": "local Iroh transfer-core; endpoints share one process",
              "gating": config["gating"], "configuration": config, "calibration": args.calibration,
              "runner_sha256": digest(Path(__file__)), "platform": platform.platform(),
              "architecture": platform.machine(), "runner_image": {k: os.getenv(k) for k in
                  ("ImageOS", "ImageVersion", "RUNNER_ARCH", "GITHUB_SHA", "GITHUB_HEAD_REF")},
              "candidate": candidate, "baseline": baseline, "trials": [], "status": "running",
              "cache_policy": "one unscored warmup per build/size; reused source; fresh destinations",
              "evidence": "minimal route counters on; payload/completion profiling off",
              "resources": "per-endpoint CPU/RSS unavailable"}
    write_json(output / "report.json", report)
    try:
        report["rustc"] = run(["rustc", f"+{config['rust_toolchain']}", "-vV"], env=environment)
        report["cargo"] = run(["cargo", f"+{config['rust_toolchain']}", "--version"], env=environment)
        # Reject harness migrations before expensive builds. No harness overlays.
        harnesses = [run(["git", "show", f"{ref}:{HARNESS}"]) for ref in (baseline, candidate)]
        if harnesses[0] != harnesses[1] or "performance harness v1" not in harnesses[0]:
            raise ValueError("incompatible committed harness; reviewed migration required")
        report["builds"] = {label: build(ref, label, output, environment, config)
                            for label, ref in (("baseline", baseline), ("candidate", candidate))}
        if len({b["harness_sha256"] for b in report["builds"].values()}) != 1:
            raise ValueError("harness binary sources differ")
        fixtures = output / "fixtures"
        fixtures.mkdir()
        report["fixtures"] = {str(size): fixture(fixtures / f"{size}.bin", size)
                              for size in config["sizes_mib"]}
        write_json(output / "report.json", report)
        for size in config["sizes_mib"]:
            expected = report["fixtures"][str(size)]
            for pair_index, warmup, order in schedule(config["measured_pairs"]):
                for label in order:
                    trial = output / "trials" / f"{size}-{pair_index}-{label}"
                    trial.mkdir(parents=True)
                    env = {**environment, "RUSTYTRANSFER_PERFORMANCE_TRIAL_DIR": str(trial),
                           "RUSTYTRANSFER_BENCH_SOURCE": expected["path"],
                           "RUSTYTRANSFER_BENCH_PATH_EVIDENCE": "1",
                           "RUSTYTRANSFER_BENCH_EXPECTED_PATH": "direct"}
                    if args.calibration == "slow" and label == "candidate":
                        env["RUSTYTRANSFER_PERFORMANCE_DELAY_MS"] = "2"
                    print(f"{size} MiB pair={pair_index} {label} warmup={warmup}", flush=True)
                    try:
                        run([report["builds"][label]["binary"], "--exact",
                             "local_full_file_performance_baseline", "--ignored", "--nocapture"],
                            cwd=trial, env=env, log=trial / "process.log",
                            timeout=config["trial_timeout_seconds"])
                        pair = json.loads((trial / "pair.json").read_text())
                        rows = [json.loads(line) for line in (trial / "endpoints.jsonl").read_text().splitlines()]
                        validate(pair, rows, expected, config["harness_version"])
                        received = trial / "received.bin"
                        if received.stat().st_size != expected["bytes"] or digest(received) != expected["sha256"]:
                            raise ValueError("independent received-file verification failed")
                        sender = next(r for r in rows if r["role"] == "sender")
                        entry = {"size_mib": size, "pair_index": pair_index, "warmup": warmup,
                                 "build": label, "elapsed_seconds": pair["elapsed_seconds"],
                                 "sender_payload_seconds": sender["payload_seconds"],
                                 "pair": pair, "endpoints": rows}
                        report["trials"].append(entry)
                        with (output / "raw.jsonl").open("a") as stream:
                            stream.write(json.dumps(entry, allow_nan=False) + "\n")
                        write_json(output / "report.json", report)
                    finally:
                        # Only this run's known output; never source fixtures or user caches.
                        for path in (trial / "received.bin", trial / "received.bin.part"):
                            path.unlink(missing_ok=True)
        report["results"] = compare(report["trials"], config)
        report["status"] = "complete"
    except Exception as error:
        report["status"] = "invalid"
        report["error"] = str(error)
        raise
    finally:
        for directory in (output / "sources", output / "fixtures"):
            if directory.exists():
                shutil.rmtree(directory)
        write_json(output / "report.json", report)
        summary = f"Rustytransfer local Iroh performance: {report['status']} ({report['gating']})\n\n"
        for result in report.get("results", []):
            summary += (f"- {result['size_mib']} MiB: paired median payload rate ratio "
                        f"{result['payload_rate_ratio']['median']:.3f}; {result['decision']}\n")
        if "error" in report:
            summary += f"\nError: {report['error']}\n"
        (output / "summary.md").write_text(summary)
        if os.getenv("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
                stream.write(summary)
        print(summary, flush=True)


if __name__ == "__main__":
    main()
