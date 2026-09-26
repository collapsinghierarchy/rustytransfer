#!/usr/bin/env python3
"""Summarize version-1 Rustytransfer and Croc JSONL measurements."""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    groups = defaultdict(list)
    rejected = defaultdict(int)
    for input_path in args.inputs:
        with input_path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("warmup", False):
                    continue
                key = (
                    record.get("transport"),
                    record.get("path"),
                    record.get("size_bytes"),
                    record.get("chunk_size"),
                    record.get("pipeline_depth"),
                    record.get("role", "pair"),
                    record.get("transport_mode") or "",
                )
                if not record.get("success"):
                    rejected[key] += 1
                    continue
                if record.get("path") not in ("direct", "relay"):
                    rejected[key] += 1
                    continue
                if record.get("source_sha256") != record.get("received_sha256"):
                    rejected[key] += 1
                    continue
                groups[key].append(record)

    rows = []
    for key, records in sorted(groups.items()):
        wall_durations = [
            record.get("wall_seconds")
            or sum(
                record.get(field) or 0.0
                for field in (
                    "handshake_seconds",
                    "payload_seconds",
                    "shutdown_seconds",
                )
            )
            for record in records
        ]
        size_mib = key[2] / (1024.0 * 1024.0)
        rates = [size_mib / duration for duration in wall_durations]
        phase_summaries = {}
        for field in ("handshake_seconds", "payload_seconds", "shutdown_seconds"):
            values = [record[field] for record in records if record.get(field) is not None]
            phase_summaries[field] = describe(values) if values else None
        resource_summaries = {}
        for field in (
            "sender_cpu_seconds",
            "receiver_cpu_seconds",
            "sender_max_rss_kib",
            "receiver_max_rss_kib",
        ):
            values = [record[field] for record in records if record.get(field) is not None]
            resource_summaries[field] = describe(values) if values else None
        rows.append(
            {
                "transport": key[0],
                "path": key[1],
                "size_bytes": key[2],
                "chunk_size": key[3],
                "pipeline_depth": key[4],
                "role": key[5],
                "transport_mode": key[6] or None,
                "successful_runs": len(records),
                "effective_mib_per_second": describe(rates),
                "wall_seconds": describe(wall_durations),
                "handshake_seconds": phase_summaries["handshake_seconds"],
                "payload_seconds": phase_summaries["payload_seconds"],
                "shutdown_seconds": phase_summaries["shutdown_seconds"],
                "sender_cpu_seconds": resource_summaries["sender_cpu_seconds"],
                "receiver_cpu_seconds": resource_summaries["receiver_cpu_seconds"],
                "sender_max_rss_kib": resource_summaries["sender_max_rss_kib"],
                "receiver_max_rss_kib": resource_summaries["receiver_max_rss_kib"],
                "endpoint_process_cpu_and_rss_available": all(
                    record["sender_cpu_seconds"] is not None
                    and record["receiver_cpu_seconds"] is not None
                    and record["sender_max_rss_kib"] is not None
                    and record["receiver_max_rss_kib"] is not None
                    for record in records
                ),
            }
        )

    result = {"schema_version": 1, "groups": rows, "rejected_rows": []}
    for key, count in sorted(rejected.items()):
        result["rejected_rows"].append(
            {
                "transport": key[0],
                "path": key[1],
                "size_bytes": key[2],
                "chunk_size": key[3],
                "pipeline_depth": key[4],
                "role": key[5],
                "transport_mode": key[6] or None,
                "count": count,
                "reason": "failed, hash mismatch, or path is mixed/unknown/auto",
            }
        )

    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
