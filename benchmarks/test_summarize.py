import unittest
import json
import tempfile
from pathlib import Path

import summarize


def record(**updates):
    value = {
        "schema_version": 1,
        "transport": "iroh",
        "build_id": "candidate-a",
        "sender_binary_sha256": "b" * 64,
        "receiver_binary_sha256": "c" * 64,
        "direction": "wsl-to-oracle",
        "host_pair": "WSL/ubuntu@oracle",
        "storage_class": "wsl-native-oracle-home",
        "pairing_mode": "invite",
        "direct_route_verified_both": True,
        "path": "direct",
        "size_bytes": 1024 * 1024,
        "chunk_size": 262144,
        "pipeline_depth": 1,
        "role": "sender",
        "transport_mode": None,
        "source_sha256": "a" * 64,
        "received_sha256": "a" * 64,
        "success": True,
        "wall_seconds": 2.0,
        "handshake_seconds": 0.25,
        "payload_seconds": 1.5,
        "shutdown_seconds": 0.25,
        "sender_cpu_seconds": 1.0,
        "receiver_cpu_seconds": 1.1,
        "sender_max_rss_kib": 1000,
        "receiver_max_rss_kib": 1100,
        "warmup": False,
    }
    value.update(updates)
    return value


class SummarizeTests(unittest.TestCase):
    def test_groups_keep_build_direction_pairing_host_and_storage_separate(self):
        rows = [
            record(),
            record(
                build_id="candidate-b",
                sender_binary_sha256="d" * 64,
                direction="oracle-to-wsl",
                pairing_mode="pake",
                storage_class="oracle-home-wsl-native",
            ),
        ]
        summary = summarize.summarize_records(rows)
        self.assertEqual(len(summary["groups"]), 2)
        self.assertEqual(
            {group["build_id"] for group in summary["groups"]},
            {"candidate-a", "candidate-b"},
        )
        self.assertEqual(
            {group["direction"] for group in summary["groups"]},
            {"wsl-to-oracle", "oracle-to-wsl"},
        )

    def test_historical_v1_rows_remain_as_unknown_provenance(self):
        legacy = record()
        legacy["path"] = "relay"
        for field in (
            "build_id",
            "sender_binary_sha256",
            "receiver_binary_sha256",
            "direction",
            "host_pair",
            "storage_class",
            "pairing_mode",
            "direct_route_verified_both",
        ):
            legacy.pop(field)
        summary = summarize.summarize_records([legacy])
        self.assertEqual(len(summary["groups"]), 1)
        group = summary["groups"][0]
        self.assertEqual(group["successful_runs"], 1)
        for field in (
            "build_id",
            "sender_binary_sha256",
            "receiver_binary_sha256",
            "direction",
            "host_pair",
            "storage_class",
            "pairing_mode",
        ):
            self.assertEqual(group[field], "unknown")

    def test_legacy_rows_group_within_but_not_across_input_files(self):
        legacy = record()
        legacy["path"] = "relay"
        for field in (
            "build_id",
            "sender_binary_sha256",
            "receiver_binary_sha256",
            "direction",
            "host_pair",
            "storage_class",
            "pairing_mode",
            "direct_route_verified_both",
        ):
            legacy.pop(field)
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "forward.jsonl"
            second = Path(temp_dir) / "reverse.jsonl"
            first.write_text(
                json.dumps(legacy) + "\n" + json.dumps(legacy) + "\n",
                encoding="utf-8",
            )
            second.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

            summary = summarize.summarize_records(
                summarize.load_records([first, second])
            )

        self.assertEqual(len(summary["groups"]), 2)
        by_source = {group["legacy_input"]: group for group in summary["groups"]}
        self.assertEqual(len(by_source), 2)
        self.assertEqual(
            sorted(group["successful_runs"] for group in by_source.values()), [1, 2]
        )

    def test_iroh_direct_rows_without_both_endpoint_evidence_are_rejected(self):
        summary = summarize.summarize_records(
            [record(direct_route_verified_both=False), record(direct_route_verified_both=None)]
        )
        self.assertEqual(summary["groups"], [])
        self.assertEqual(sum(row["count"] for row in summary["rejected_rows"]), 2)
        self.assertEqual(
            {row["reason"] for row in summary["rejected_rows"]},
            {"direct route was not verified by both endpoints"},
        )

    def test_missing_null_or_mismatched_payload_hashes_are_rejected(self):
        variants = (
            record(source_sha256=None),
            record(received_sha256=None),
            record(source_sha256=None, received_sha256=None),
            record(received_sha256="d" * 64),
        )
        summary = summarize.summarize_records(variants)
        self.assertEqual(summary["groups"], [])
        self.assertEqual(sum(row["count"] for row in summary["rejected_rows"]), 4)

    def test_invalid_wall_and_missing_binary_hashes_are_rejected(self):
        variants = (
            record(wall_seconds=0),
            record(wall_seconds=float("nan")),
            record(sender_binary_sha256=None),
            record(receiver_binary_sha256="invalid"),
        )
        summary = summarize.summarize_records(variants)
        self.assertEqual(summary["groups"], [])
        self.assertEqual(sum(row["count"] for row in summary["rejected_rows"]), 4)


if __name__ == "__main__":
    unittest.main()
