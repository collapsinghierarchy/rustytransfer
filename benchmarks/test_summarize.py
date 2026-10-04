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
    def test_profile_summary_accepts_overlapping_sender_substages_and_connection_counters(self):
        row = record(
            profile_mode="payload-profile",
            payload_profile={
                "source_read_seconds": 0.1,
                "allocation_copy_encrypt_seconds": 0.3,
                "sender_allocation_copy_seconds": 0.1,
                "sender_encrypt_seconds": 0.2,
                "send_wait_seconds": 0.2,
                "receive_wait_seconds": 0.0,
                "decrypt_seconds": 0.0,
                "destination_write_seconds": 0.0,
                "chunk_count": 1,
                "payload_bytes": 1024,
            },
            path_evidence={
                "connection_stats": {
                    "samples": 4,
                    "rtt_min_us": 20_000,
                    "lost_packets_delta": 2,
                    "send_stalls_over_10ms": 1,
                }
            },
            bytes_transferred=1024,
        )
        summary = summarize.summarize_records([row])
        group = summary["groups"][0]
        self.assertEqual(group["payload_profile"]["sender_encrypt_seconds"]["median"], 0.2)
        self.assertEqual(group["connection_stats"]["rtt_min_us"]["median"], 20_000)
        self.assertEqual(group["connection_stats"]["lost_packets_delta"]["median"], 2)

        row["path_evidence"]["connection_stats"]["lost_packets_delta"] = -1
        summary = summarize.summarize_records([row])
        self.assertEqual(summary["groups"], [])
        self.assertIn("invalid counter", summary["rejected_rows"][0]["reason"])

        row["path_evidence"]["connection_stats"]["lost_packets_delta"] = 0
        row["payload_profile"]["sender_encrypt_seconds"] = 0.4
        summary = summarize.summarize_records([row])
        self.assertEqual(summary["groups"], [])
        self.assertIn("sender substages exceed", summary["rejected_rows"][0]["reason"])

    def test_sender_substages_remain_optional_for_old_profiles_but_validate_when_present(self):
        profile = {
            "source_read_seconds": 0.1,
            "allocation_copy_encrypt_seconds": 0.2,
            "send_wait_seconds": 0.1,
            "receive_wait_seconds": 0.0,
            "decrypt_seconds": 0.0,
            "destination_write_seconds": 0.0,
            "chunk_count": 1,
            "payload_bytes": 1024,
        }
        self.assertIsNone(summarize.payload_profile_rejection_reason(profile, 1024, 1.0))
        profile["sender_encrypt_seconds"] = 0.1
        self.assertIn(
            "sender substages are incomplete",
            summarize.payload_profile_rejection_reason(profile, 1024, 1.0),
        )
        profile["sender_allocation_copy_seconds"] = -0.1
        self.assertIn(
            "missing or invalid",
            summarize.payload_profile_rejection_reason(profile, 1024, 1.0),
        )

    def test_profile_mode_separates_diagnostic_rows_and_legacy_defaults_to_standard(self):
        rows = [
            record(),
            record(
                profile_mode="payload-profile",
                payload_profile={
                    "source_read_seconds": 0.0,
                    "allocation_copy_encrypt_seconds": 0.0,
                    "send_wait_seconds": 0.0,
                    "receive_wait_seconds": 0.0,
                    "decrypt_seconds": 0.0,
                    "destination_write_seconds": 0.0,
                    "chunk_count": 1,
                    "payload_bytes": 1024 * 1024,
                },
                bytes_transferred=1024 * 1024,
            ),
            record(profile_mode="standard"),
        ]
        rows[2].pop("profile_mode")
        summary = summarize.summarize_records(rows)
        self.assertEqual(len(summary["groups"]), 2)
        by_profile = {group["profile_mode"]: group for group in summary["groups"]}
        self.assertEqual(by_profile["standard"]["successful_runs"], 2)
        self.assertEqual(by_profile["payload-profile"]["successful_runs"], 1)

    def test_invalid_payload_profile_is_rejected_from_success_summary(self):
        row = record(
            profile_mode="payload-profile",
            payload_profile={
                "source_read_seconds": 0.1,
                "allocation_copy_encrypt_seconds": 0.2,
                "send_wait_seconds": 0.3,
                "receive_wait_seconds": 0.0,
                "decrypt_seconds": 0.0,
                "destination_write_seconds": 0.0,
                "chunk_count": 1,
                "payload_bytes": 1024,
            },
            bytes_transferred=1023,
        )
        summary = summarize.summarize_records([row])
        self.assertEqual(summary["groups"], [])
        self.assertIn("payload profile byte count", summary["rejected_rows"][0]["reason"])

    def test_source_staging_separates_pre_staged_rows_and_legacy_defaults(self):
        rows = [
            record(),
            record(source_staging="pre-staged"),
            record(source_staging="per-trial"),
        ]
        rows[2].pop("source_staging")
        summary = summarize.summarize_records(rows)
        self.assertEqual(len(summary["groups"]), 2)
        by_staging = {group["source_staging"]: group for group in summary["groups"]}
        self.assertEqual(by_staging["per-trial"]["successful_runs"], 2)
        self.assertEqual(by_staging["pre-staged"]["successful_runs"], 1)

    def test_stream_window_separates_default_and_benchmark_window_rows(self):
        rows = [record(), record(stream_window_bytes=2_500_000)]
        summary = summarize.summarize_records(rows)
        self.assertEqual(len(summary["groups"]), 2)
        self.assertEqual(
            {group["stream_window_bytes"] for group in summary["groups"]},
            {"unknown", 2_500_000},
        )

    def test_shared_key_experiments_group_by_protocol_stream_and_connection_count(self):
        path_evidence = {
            "classification": "direct", "verified": True, "lagged": False,
            "missing_path_stats": False, "relay_selected": False,
            "relay_stream_tx": 0, "relay_stream_rx": 0,
            "direct_stream_tx": 1, "direct_stream_rx": 1,
        }

        def experiment(streams, connections, protocol="shared-key-parallel/2"):
            per_connection = [
                {
                    "connection_index": index,
                    "stable_id": 10 + index,
                    "local_endpoint_id": "sender",
                    "remote_endpoint_id": "receiver",
                    "path": "direct",
                    "path_start": "direct",
                    "path_end": "direct",
                    "path_evidence": path_evidence,
                }
                for index in range(connections)
            ]
            return record(
                experimental_protocol_version=protocol,
                parallel_streams=streams,
                parallel_connections=connections,
                payload_key_count=1,
                kem_sessions=1,
                path_start="direct",
                path_end="direct",
                path_evidence=path_evidence,
                connection_evidence=per_connection,
            )

        rows = [
            record(),
            experiment(1, 1),
            experiment(4, 1),
            experiment(4, 4),
            experiment(4, 1, "shared-key-parallel/1"),
        ]
        summary = summarize.summarize_records(rows)
        self.assertEqual(len(summary["groups"]), 5)
        self.assertEqual(
            {group["parallel_streams"] for group in summary["groups"]},
            {"unknown", 1, 4},
        )
        self.assertEqual(
            {group["parallel_connections"] for group in summary["groups"]},
            {"unknown", 1, 4},
        )

    def test_v2_summarizer_rejects_corrupt_secondary_connection_evidence(self):
        evidence = {
            "classification": "direct", "verified": True, "lagged": False,
            "missing_path_stats": False, "relay_selected": False,
            "relay_stream_tx": 0, "relay_stream_rx": 0,
            "direct_stream_tx": 1, "direct_stream_rx": 1,
        }
        connections = [
            {
                "connection_index": index,
                "stable_id": 100 + index,
                "local_endpoint_id": "sender",
                "remote_endpoint_id": "receiver",
                "path": "direct",
                "path_start": "direct",
                "path_end": "direct",
                "path_evidence": evidence,
            }
            for index in range(4)
        ]
        valid = record(
            experimental_protocol_version="shared-key-parallel/2",
            parallel_streams=4,
            parallel_connections=4,
            payload_key_count=1,
            kem_sessions=1,
            path_start="direct",
            path_end="direct",
            path_evidence=evidence,
            connection_evidence=connections,
        )
        self.assertEqual(summarize.rejection_reason(valid), None)
        relay = {**valid, "connection_evidence": [dict(item) for item in connections]}
        relay["connection_evidence"][2]["path"] = "relay"
        self.assertIn("connection did not remain direct", summarize.rejection_reason(relay))
        missing = {**valid, "connection_evidence": connections[:3]}
        self.assertIn("missing or incomplete", summarize.rejection_reason(missing))
        duplicate = {**valid, "connection_evidence": [dict(item) for item in connections]}
        duplicate["connection_evidence"][3]["stable_id"] = 101
        self.assertIn("duplicated", summarize.rejection_reason(duplicate))

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
