#!/usr/bin/env python3
"""Strictly audit and summarize the two-series ARM A/B result directory."""

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path


SERIES_DIR = re.compile(
    r"series(?P<series>[0-9]+)-(?P<direction>wsl-to-oracle|oracle-to-wsl)-"
    r"(?P<variant>baseline|candidate)$"
)
SIZE_FILE = re.compile(r"oracle-(?P<size>[0-9]+)\.jsonl$")
SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")
SIZE_BYTES = {64: 64 * 1024 * 1024, 512: 512 * 1024 * 1024}
EXPECTED_RUNS = 5


def fail(message):
    raise ValueError(message)


def is_sha256(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def verified_direct(row):
    evidence = row.get("path_evidence")
    return (
        row.get("path") == "direct"
        and row.get("direct_route_verified_both") is True
        and isinstance(evidence, dict)
        and evidence.get("classification") == "direct"
        and evidence.get("verified") is True
        and evidence.get("lagged") is False
        and evidence.get("missing_path_stats") is False
        and evidence.get("relay_selected") is False
        and evidence.get("relay_stream_tx") == 0
        and evidence.get("relay_stream_rx") == 0
        and isinstance(evidence.get("direct_stream_tx"), int)
        and not isinstance(evidence.get("direct_stream_tx"), bool)
        and isinstance(evidence.get("direct_stream_rx"), int)
        and not isinstance(evidence.get("direct_stream_rx"), bool)
        and evidence["direct_stream_tx"] + evidence["direct_stream_rx"] > 0
    )


def read_jsonl(path):
    result = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            fail(f"{path}:{line_no}: invalid JSON: {error}")
        if not isinstance(row, dict):
            fail(f"{path}:{line_no}: row is not an object")
        result.append(row)
    return result


def finite_nonnegative(row, key, context):
    value = row.get(key)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        fail(f"{context}: {key} is missing or invalid")
    return float(value)


def validate_pair(path, run_index, pair_rows, direction, size_mib, expected_bytes, expected_hash, manifest, warmup):
    label = "warmup" if warmup else "measured"
    if sorted(row.get("role") for row in pair_rows) != ["receiver", "sender"]:
        fail(f"{path} {label} trial {run_index}: expected exactly one row per endpoint")
    sender = next(row for row in pair_rows if row.get("role") == "sender")
    receiver = next(row for row in pair_rows if row.get("role") == "receiver")
    oracle_hash = manifest["remote_binaries"][path.parent.name.rsplit("-", 1)[-1]]["sha256"]
    local_hash = manifest["x86_binary_sha256"]
    for row in pair_rows:
        context = f"{path} {label} trial {run_index} {row.get('role')}"
        if row.get("success") is not True:
            fail(f"{context}: transfer did not succeed")
        if row.get("profile_mode") != "standard" or row.get("payload_profile") is not None:
            fail(f"{context}: result is not a standard, unprofiled run")
        if row.get("transport") != "iroh" or row.get("size_bytes") != expected_bytes:
            fail(f"{context}: transport or payload size mismatch")
        if not verified_direct(row):
            fail(f"{context}: strict direct route evidence is missing or invalid")
        if not is_sha256(row.get("source_sha256")) or not is_sha256(row.get("received_sha256")):
            fail(f"{context}: source/received SHA-256 is missing or malformed")
        if row["source_sha256"].lower() != row["received_sha256"].lower():
            fail(f"{context}: source and received SHA-256 values differ")
        if row["source_sha256"].lower() != expected_hash:
            fail(f"{context}: source hash does not match the experiment manifest")
        endpoint_hash = (
            local_hash if (direction == "wsl-to-oracle") == (row["role"] == "sender")
            else oracle_hash
        )
        hash_field = "sender_binary_sha256" if row["role"] == "sender" else "receiver_binary_sha256"
        if row.get(hash_field) != endpoint_hash:
            fail(f"{context}: {hash_field} does not match the experiment manifest")
        if not isinstance(row.get("build_id"), str) or not row["build_id"].strip():
            fail(f"{context}: build_id is absent")
        if row.get("direction") != direction or row.get("pairing_mode") != "invite":
            fail(f"{context}: direction or authentication mode mismatch")
        if not isinstance(row.get("bytes_transferred"), int) or isinstance(row.get("bytes_transferred"), bool):
            fail(f"{context}: endpoint byte count is invalid")
        if row["bytes_transferred"] != expected_bytes:
            fail(f"{context}: expected a full, non-resumed payload byte count")
        chunk_size = row.get("chunk_size")
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
            fail(f"{context}: endpoint chunk size is invalid")
        for field in (
            "wall_seconds", "handshake_seconds", "payload_seconds", "shutdown_seconds",
            "sender_cpu_seconds", "receiver_cpu_seconds", "sender_max_rss_kib", "receiver_max_rss_kib",
        ):
            finite_nonnegative(row, field, context)
    shared_fields = (
        "source_sha256", "received_sha256", "build_id", "sender_binary_sha256",
        "receiver_binary_sha256", "direction", "host_pair", "storage_class", "pairing_mode",
        "wall_seconds", "sender_cpu_seconds", "receiver_cpu_seconds", "sender_max_rss_kib",
        "receiver_max_rss_kib", "size_bytes", "chunk_size", "bytes_transferred",
    )
    for field in shared_fields:
        if sender.get(field) != receiver.get(field):
            fail(f"{path} {label} trial {run_index}: endpoint rows disagree on {field}")
    # Endpoint handshake/payload/shutdown values are endpoint-local and may
    # naturally differ. The externally measured wall/resource rows must match.
    return sender, receiver


def parse_case(path, manifest):
    dir_match = SERIES_DIR.fullmatch(path.parent.name)
    size_match = SIZE_FILE.fullmatch(path.name)
    if not dir_match or not size_match:
        fail(f"unrecognized result path: {path}")
    series = f"series{dir_match['series']}"
    direction = dir_match["direction"]
    variant = dir_match["variant"]
    size_mib = int(size_match["size"])
    if size_mib not in SIZE_BYTES:
        fail(f"{path}: expected a 64 or 512 MiB result file")
    expected_bytes = SIZE_BYTES[size_mib]
    rows = read_jsonl(path)
    if any(not isinstance(row.get("warmup"), bool) for row in rows):
        fail(f"{path}: each row must declare a boolean warmup flag")
    warmup_rows = [row for row in rows if row.get("warmup") is True]
    measured_rows = [row for row in rows if row.get("warmup") is False]
    if len(warmup_rows) < 2 or len(warmup_rows) % 2:
        fail(f"{path}: warmup rows must contain complete endpoint pairs")
    warmups_by_run = defaultdict(list)
    for row in warmup_rows:
        run_index = row.get("run_index")
        if not isinstance(run_index, int) or isinstance(run_index, bool):
            fail(f"{path}: warmup row has invalid run_index")
        warmups_by_run[run_index].append(row)
    if any(len(pair) != 2 for pair in warmups_by_run.values()):
        fail(f"{path}: a warmup trial is missing an endpoint row")
    expected_hash = manifest["source_hashes"][str(size_mib)].lower()
    if not is_sha256(expected_hash):
        fail(f"{path}: source hash in manifest is malformed")
    provenance_by_run = {}
    for run_index, pair in warmups_by_run.items():
        sender, _ = validate_pair(
            path, run_index, pair, direction, size_mib, expected_bytes, expected_hash, manifest, True
        )
        if finite_nonnegative(sender, "wall_seconds", f"{path} warmup trial {run_index}") <= 0:
            fail(f"{path} warmup trial {run_index}: wall_seconds must be positive")
        provenance_by_run[run_index] = tuple(sender.get(field) for field in (
            "build_id", "sender_binary_sha256", "receiver_binary_sha256",
            "host_pair", "storage_class", "pairing_mode",
        ))
    if len(measured_rows) != EXPECTED_RUNS * 2:
        fail(
            f"{path}: expected {EXPECTED_RUNS} measured endpoint pairs "
            f"({EXPECTED_RUNS * 2} rows), found {len(measured_rows)}"
        )

    by_run = defaultdict(list)
    for row in measured_rows:
        run_index = row.get("run_index")
        if not isinstance(run_index, int) or isinstance(run_index, bool):
            fail(f"{path}: measured row has invalid run_index")
        by_run[run_index].append(row)
    if sorted(by_run) != list(range(1, EXPECTED_RUNS + 1)):
        fail(f"{path}: measured run indexes must be 1 through {EXPECTED_RUNS}")

    pairs = []
    group_provenance = next(iter(provenance_by_run.values()), None)
    if any(value != group_provenance for value in provenance_by_run.values()):
        fail(f"{path}: provenance changed between warmup trials")
    for run_index, pair_rows in sorted(by_run.items()):
        sender, receiver = validate_pair(
            path, run_index, pair_rows, direction, size_mib, expected_bytes, expected_hash, manifest, False
        )
        for field in ("build_id", "sender_binary_sha256", "receiver_binary_sha256"):
            if not isinstance(sender.get(field), str) or not sender[field].strip():
                fail(f"{path} trial {run_index}: {field} is absent")
        for field in ("sender_binary_sha256", "receiver_binary_sha256"):
            if not is_sha256(sender.get(field)):
                fail(f"{path} trial {run_index}: {field} is malformed")
        provenance = tuple(sender.get(field) for field in (
            "build_id", "sender_binary_sha256", "receiver_binary_sha256", "host_pair", "storage_class", "pairing_mode"
        ))
        if group_provenance is None:
            group_provenance = provenance
        elif group_provenance != provenance:
            fail(f"{path}: provenance changed between warmup and measured trials")
        wall = finite_nonnegative(sender, "wall_seconds", f"{path} trial {run_index}")
        if wall <= 0:
            fail(f"{path} trial {run_index}: wall_seconds must be positive")
        size_gib = expected_bytes / (1024**3)
        sender_cpu = finite_nonnegative(sender, "sender_cpu_seconds", f"{path} trial {run_index}")
        receiver_cpu = finite_nonnegative(receiver, "receiver_cpu_seconds", f"{path} trial {run_index}")
        arm_cpu = receiver_cpu if direction == "wsl-to-oracle" else sender_cpu
        pairs.append({
            "run_index": run_index,
            "size_mib": size_mib,
            "size_gib": size_gib,
            "rate_mib_s": size_mib / wall,
            "wall_seconds": wall,
            "setup_seconds": finite_nonnegative(sender, "handshake_seconds", f"{path} trial {run_index}"),
            "receiver_setup_seconds": finite_nonnegative(receiver, "handshake_seconds", f"{path} trial {run_index}"),
            "sender_cpu_seconds": sender_cpu,
            "receiver_cpu_seconds": receiver_cpu,
            "total_cpu_seconds_per_gib": (sender_cpu + receiver_cpu) / size_gib,
            "arm_target_cpu_seconds_per_gib": arm_cpu / size_gib,
            "sender_cpu_seconds_per_gib": sender_cpu / size_gib,
            "receiver_cpu_seconds_per_gib": receiver_cpu / size_gib,
            "sender_max_rss_kib": finite_nonnegative(sender, "sender_max_rss_kib", f"{path} trial {run_index}"),
            "receiver_max_rss_kib": finite_nonnegative(receiver, "receiver_max_rss_kib", f"{path} trial {run_index}"),
            "source_sha256": sender["source_sha256"].lower(),
            "binary_hashes": [sender["sender_binary_sha256"], sender["receiver_binary_sha256"]],
        })
    return {
        "series": series,
        "direction": direction,
        "variant": variant,
        "size_mib": size_mib,
        "provenance": group_provenance,
        "pairs": pairs,
    }


def describe(values):
    median = statistics.median(values)
    return {
        "median": round(median, 6),
        "minimum": round(min(values), 6),
        "maximum": round(max(values), 6),
        "mad": round(statistics.median(abs(value - median) for value in values), 6),
    }


def summarize_case(case):
    pairs = case["pairs"]
    return {
        "n": len(pairs),
        "build_id": case["provenance"][0],
        "binary_hashes": case["provenance"][1:3],
        "throughput_mib_s": describe([pair["rate_mib_s"] for pair in pairs]),
        "wall_seconds": describe([pair["wall_seconds"] for pair in pairs]),
        "setup_handshake_seconds": describe([pair["setup_seconds"] for pair in pairs]),
        "receiver_setup_handshake_seconds": describe([pair["receiver_setup_seconds"] for pair in pairs]),
        "arm_target_cpu_seconds_per_gib": describe([pair["arm_target_cpu_seconds_per_gib"] for pair in pairs]),
        "sender_cpu_seconds_per_gib": describe([pair["sender_cpu_seconds_per_gib"] for pair in pairs]),
        "receiver_cpu_seconds_per_gib": describe([pair["receiver_cpu_seconds_per_gib"] for pair in pairs]),
        "total_cpu_seconds_per_gib": describe([pair["total_cpu_seconds_per_gib"] for pair in pairs]),
        "sender_max_rss_kib": describe([pair["sender_max_rss_kib"] for pair in pairs]),
        "receiver_max_rss_kib": describe([pair["receiver_max_rss_kib"] for pair in pairs]),
        "pairs": pairs,
    }


def pct_delta(candidate, baseline):
    if baseline == 0:
        return None
    return round((candidate / baseline - 1.0) * 100.0, 3)


def summarize_population(cases):
    pairs = [pair for case in cases for pair in case["pairs"]]
    return {
        "n_pairs": len(pairs),
        "throughput_mib_s": describe([pair["rate_mib_s"] for pair in pairs]),
        "wall_seconds": describe([pair["wall_seconds"] for pair in pairs]),
        "setup_handshake_seconds": describe([pair["setup_seconds"] for pair in pairs]),
        "receiver_setup_handshake_seconds": describe([pair["receiver_setup_seconds"] for pair in pairs]),
        "arm_target_cpu_seconds_per_gib": describe([pair["arm_target_cpu_seconds_per_gib"] for pair in pairs]),
        "sender_cpu_seconds_per_gib": describe([pair["sender_cpu_seconds_per_gib"] for pair in pairs]),
        "receiver_cpu_seconds_per_gib": describe([pair["receiver_cpu_seconds_per_gib"] for pair in pairs]),
        "total_cpu_seconds_per_gib": describe([pair["total_cpu_seconds_per_gib"] for pair in pairs]),
        "sender_max_rss_kib": describe([pair["sender_max_rss_kib"] for pair in pairs]),
        "receiver_max_rss_kib": describe([pair["receiver_max_rss_kib"] for pair in pairs]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path("/home/wasilij/rustytransfer-bench/results/arm-comparison-20261002"),
    )
    args = parser.parse_args()
    cases = []
    manifest = json.loads((args.root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("profile_mode") != "standard" or manifest.get("runs_per_series_variant_case") != EXPECTED_RUNS:
        fail("manifest profile mode or expected trial count does not match this audit")
    for path in sorted(args.root.glob("series*-*/*oracle-*.jsonl")):
        cases.append(parse_case(path, manifest))
    keys = {(case["series"], case["direction"], case["size_mib"], case["variant"]) for case in cases}
    expected = {
        (series, direction, size, variant)
        for series in ("series1", "series2")
        for direction in ("wsl-to-oracle", "oracle-to-wsl")
        for size in (64, 512)
        for variant in ("baseline", "candidate")
    }
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing or extra:
        fail(f"result case inventory mismatch; missing={missing}; extra={extra}")

    by_key = {(case["series"], case["direction"], case["size_mib"], case["variant"]): case for case in cases}
    comparisons = []
    regressions = []
    for series in ("series1", "series2"):
        for direction in ("wsl-to-oracle", "oracle-to-wsl"):
            for size in (64, 512):
                baseline = by_key[(series, direction, size, "baseline")]
                candidate = by_key[(series, direction, size, "candidate")]
                base = summarize_case(baseline)
                cand = summarize_case(candidate)
                rate_delta = pct_delta(cand["throughput_mib_s"]["median"], base["throughput_mib_s"]["median"])
                cpu_delta = pct_delta(
                    cand["arm_target_cpu_seconds_per_gib"]["median"],
                    base["arm_target_cpu_seconds_per_gib"]["median"],
                )
                record = {
                    "series": series,
                    "direction": direction,
                    "size_mib": size,
                    "baseline": base,
                    "candidate": cand,
                    "candidate_throughput_delta_percent": rate_delta,
                    "candidate_arm_cpu_per_gib_delta_percent": cpu_delta,
                    "candidate_setup_delta_seconds": round(cand["setup_handshake_seconds"]["median"] - base["setup_handshake_seconds"]["median"], 6),
                    "candidate_receiver_setup_delta_seconds": round(cand["receiver_setup_handshake_seconds"]["median"] - base["receiver_setup_handshake_seconds"]["median"], 6),
                    "candidate_wall_delta_seconds": round(cand["wall_seconds"]["median"] - base["wall_seconds"]["median"], 6),
                }
                comparisons.append(record)
                if rate_delta is not None and rate_delta < -3.0:
                    regressions.append({
                        "series": series,
                        "direction": direction,
                        "size_mib": size,
                        "throughput_delta_percent": rate_delta,
                        "status": "unresolved regression over 3%",
                    })

    pooled = {}
    series_cpu_gains = {}
    pooled_by_case = {}
    for direction in ("wsl-to-oracle", "oracle-to-wsl"):
        for size in (64, 512):
            case_id = f"{direction}-{size}mib"
            subset = [case for case in cases if case["direction"] == direction and case["size_mib"] == size]
            pooled_by_case[case_id] = {}
            for variant in ("baseline", "candidate"):
                pooled_by_case[case_id][variant] = summarize_population(
                    [case for case in subset if case["variant"] == variant]
                )
            base = pooled_by_case[case_id]["baseline"]["arm_target_cpu_seconds_per_gib"]["median"]
            cand = pooled_by_case[case_id]["candidate"]["arm_target_cpu_seconds_per_gib"]["median"]
            pooled_by_case[case_id]["candidate_arm_cpu_per_gib_delta_percent"] = pct_delta(cand, base)

    # The crypto candidate is expected to reduce CPU in the Oracle ARM endpoint
    # on large transfers in both directions. Require >=10% in each direction,
    # independently confirmed by each of the two interleaved series.
    target_cases = ("wsl-to-oracle-512mib", "oracle-to-wsl-512mib")
    for series in ("series1", "series2"):
        series_cpu_gains[series] = {}
        for direction, size in (("wsl-to-oracle", 512), ("oracle-to-wsl", 512)):
            base = summarize_case(by_key[(series, direction, size, "baseline")])
            cand = summarize_case(by_key[(series, direction, size, "candidate")])
            delta = pct_delta(
                cand["arm_target_cpu_seconds_per_gib"]["median"],
                base["arm_target_cpu_seconds_per_gib"]["median"],
            )
            series_cpu_gains[series][f"{direction}-{size}mib"] = delta is not None and delta <= -10.0

    report = {
        "result_root": str(args.root),
        "standard_profile_only": True,
        "strict_direct_route_and_sha256_gate": "passed",
        "expected_measured_pairs_per_variant_case_series": EXPECTED_RUNS,
        "comparisons": comparisons,
        "pooled_across_series_by_direction_and_size": pooled_by_case,
        "unresolved_throughput_regressions_over_3_percent": regressions,
        "cpu_improvement_at_least_10_percent_confirmed_both_series": all(
            value for series_values in series_cpu_gains.values() for value in series_values.values()
        ),
        "cpu_target_cases": list(target_cases),
        "series_cpu_gate": series_cpu_gains,
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
