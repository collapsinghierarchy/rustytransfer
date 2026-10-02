#!/usr/bin/env python3
"""Summarize version-1 Rustytransfer and Croc JSONL measurements."""

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path


SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
PROFILE_TIME_FIELDS = (
    "source_read_seconds",
    "allocation_copy_encrypt_seconds",
    "send_wait_seconds",
    "receive_wait_seconds",
    "decrypt_seconds",
    "destination_write_seconds",
)
GROUP_FIELDS = (
    "transport",
    "build_id",
    "sender_binary_sha256",
    "receiver_binary_sha256",
    "direction",
    "host_pair",
    "storage_class",
    "pairing_mode",
    "profile_mode",
    "source_staging",
    "direct_route_verified_both",
    "legacy_input",
    "path",
    "size_bytes",
    "chunk_size",
    "pipeline_depth",
    "role",
    "transport_mode",
)


def median_absolute_deviation(values, center):
    return statistics.median(abs(value - center) for value in values)


def describe(values):
    center = statistics.median(values)
    return {
        "median": center,
        "minimum": min(values),
        "maximum": max(values),
        "mad": median_absolute_deviation(values, center),
    }


def group_key(record):
    key = []
    for field in GROUP_FIELDS:
        if field == "legacy_input":
            # Historical rows have no build provenance, so only combine them
            # when they came from the same input file. Keep that source visible.
            value = record.get("_legacy_input", "unknown") if record.get("build_id") is None else "unknown"
        else:
            value = record.get(field)
            if field == "profile_mode" and value is None:
                value = "standard"
            if field == "source_staging" and value is None:
                value = "per-trial"
        key.append("unknown" if value is None else value)
    return tuple(key)


def payload_profile_rejection_reason(profile, bytes_transferred, payload_seconds):
    if not isinstance(profile, dict):
        return "payload profile is missing or invalid"
    if (
        not isinstance(bytes_transferred, int)
        or isinstance(bytes_transferred, bool)
        or bytes_transferred < 0
    ):
        return "payload profile endpoint byte count is missing or invalid"
    if (
        not isinstance(payload_seconds, (int, float))
        or isinstance(payload_seconds, bool)
        or not math.isfinite(payload_seconds)
        or payload_seconds < 0
    ):
        return "payload profile payload duration is missing or invalid"
    for field in PROFILE_TIME_FIELDS:
        value = profile.get(field)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            return "payload profile is missing or invalid"
    for field in ("chunk_count", "payload_bytes"):
        value = profile.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return "payload profile is missing or invalid"
    if profile["payload_bytes"] != bytes_transferred:
        return "payload profile byte count does not match endpoint bytes"
    if profile["payload_bytes"] > 0 and profile["chunk_count"] == 0:
        return "payload profile chunk count is missing or invalid"
    try:
        stage_sum = math.fsum(profile[field] for field in PROFILE_TIME_FIELDS)
    except OverflowError:
        return "payload profile stage duration is invalid"
    if stage_sum > payload_seconds + 1e-6:
        return "payload profile stage durations exceed payload duration"
    return None


def load_records(input_paths):
    records = []
    for input_path in input_paths:
        source_id = "legacy-file-" + hashlib.sha256(
            str(input_path.resolve()).encode("utf-8")
        ).hexdigest()[:12]
        with input_path.open(encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    record = json.loads(line)
                    if record.get("build_id") is None:
                        record["_legacy_input"] = source_id
                    records.append(record)
    return records


def rejection_reason(record):
    if record.get("success") is not True:
        return "transfer failed"
    profile_mode = record.get("profile_mode", "standard")
    if profile_mode not in (None, "standard", "payload-profile"):
        return "profile mode is invalid"
    if profile_mode == "payload-profile":
        profile_error = payload_profile_rejection_reason(
            record.get("payload_profile"),
            record.get("bytes_transferred"),
            record.get("payload_seconds"),
        )
        if profile_error is not None:
            return profile_error
    if record.get("source_staging", "per-trial") not in ("per-trial", "pre-staged"):
        return "source staging mode is invalid"
    if record.get("path") not in ("direct", "relay"):
        return "path is mixed, unknown, or auto"
    if record.get("transport") == "iroh" and record.get("path") == "direct":
        if record.get("direct_route_verified_both") is not True:
            return "direct route was not verified by both endpoints"
    source_hash = record.get("source_sha256")
    received_hash = record.get("received_sha256")
    if not isinstance(source_hash, str) or not SHA256.fullmatch(source_hash):
        return "source SHA-256 is missing or invalid"
    if not isinstance(received_hash, str) or not SHA256.fullmatch(received_hash):
        return "received SHA-256 is missing or invalid"
    if source_hash.lower() != received_hash.lower():
        return "source and received SHA-256 values do not match"

    wall = record.get("wall_seconds")
    if not isinstance(wall, (int, float)) or isinstance(wall, bool) or wall <= 0:
        return "wall duration is missing or invalid"
    try:
        wall = float(wall)
    except OverflowError:
        return "wall duration is missing or invalid"
    if not math.isfinite(wall):
        return "wall duration is missing or invalid"
    size_bytes = record.get("size_bytes")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        return "size is missing or invalid"

    build_id = record.get("build_id")
    sender_binary_hash = record.get("sender_binary_sha256")
    receiver_binary_hash = record.get("receiver_binary_sha256")
    if build_id is not None:
        if not isinstance(build_id, str) or not build_id.strip():
            return "build ID is invalid"
        if not isinstance(sender_binary_hash, str) or not SHA256.fullmatch(sender_binary_hash):
            return "sender binary SHA-256 is missing or invalid"
        if not isinstance(receiver_binary_hash, str) or not SHA256.fullmatch(receiver_binary_hash):
            return "receiver binary SHA-256 is missing or invalid"
        for field in ("direction", "host_pair", "storage_class", "pairing_mode"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                return f"{field} is missing or invalid"
    elif sender_binary_hash is not None or receiver_binary_hash is not None:
        if not isinstance(sender_binary_hash, str) or not SHA256.fullmatch(sender_binary_hash):
            return "sender binary SHA-256 is invalid"
        if not isinstance(receiver_binary_hash, str) or not SHA256.fullmatch(receiver_binary_hash):
            return "receiver binary SHA-256 is invalid"
    return None


def summarize_records(records):
    groups = defaultdict(list)
    rejected = defaultdict(int)
    for record in records:
        if record.get("warmup", False):
            continue
        key = group_key(record)
        reason = rejection_reason(record)
        if reason is not None:
            rejected[(key, reason)] += 1
            continue
        groups[key].append(record)

    rows = []
    for key, group_records in sorted(groups.items(), key=lambda item: json.dumps(item[0])):
        dimensions = dict(zip(GROUP_FIELDS, key))
        wall_durations = [float(record["wall_seconds"]) for record in group_records]
        size_mib = dimensions["size_bytes"] / (1024.0 * 1024.0)
        rates = [size_mib / duration for duration in wall_durations]
        phase_summaries = {}
        for field in ("handshake_seconds", "payload_seconds", "shutdown_seconds"):
            values = [record[field] for record in group_records if record.get(field) is not None]
            phase_summaries[field] = describe(values) if values else None
        resource_summaries = {}
        for field in (
            "sender_cpu_seconds",
            "receiver_cpu_seconds",
            "sender_max_rss_kib",
            "receiver_max_rss_kib",
        ):
            values = [record[field] for record in group_records if record.get(field) is not None]
            resource_summaries[field] = describe(values) if values else None
        profile_summary = None
        if dimensions["profile_mode"] == "payload-profile":
            profile_summary = {
                field: describe([record["payload_profile"][field] for record in group_records])
                for field in (*PROFILE_TIME_FIELDS, "chunk_count", "payload_bytes")
            }
        rows.append(
            {
                **dimensions,
                "successful_runs": len(group_records),
                "effective_mib_per_second": describe(rates),
                "wall_seconds": describe(wall_durations),
                "handshake_seconds": phase_summaries["handshake_seconds"],
                "payload_seconds": phase_summaries["payload_seconds"],
                "shutdown_seconds": phase_summaries["shutdown_seconds"],
                "payload_profile": profile_summary,
                "sender_cpu_seconds": resource_summaries["sender_cpu_seconds"],
                "receiver_cpu_seconds": resource_summaries["receiver_cpu_seconds"],
                "sender_max_rss_kib": resource_summaries["sender_max_rss_kib"],
                "receiver_max_rss_kib": resource_summaries["receiver_max_rss_kib"],
                "endpoint_process_cpu_and_rss_available": all(
                    record.get("sender_cpu_seconds") is not None
                    and record.get("receiver_cpu_seconds") is not None
                    and record.get("sender_max_rss_kib") is not None
                    and record.get("receiver_max_rss_kib") is not None
                    for record in group_records
                ),
            }
        )

    rejected_rows = []
    for (key, reason), count in sorted(
        rejected.items(), key=lambda item: json.dumps(item[0])
    ):
        rejected_rows.append(
            {
                **dict(zip(GROUP_FIELDS, key)),
                "count": count,
                "reason": reason,
            }
        )
    return {"schema_version": 1, "groups": rows, "rejected_rows": rejected_rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = summarize_records(load_records(args.inputs))

    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
