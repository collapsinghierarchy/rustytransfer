"""Checks for fair lifetime timing and isolated completion experiment settings."""
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

import run_oracle_transfer as runner


class CompletionBenchmarkTests(unittest.TestCase):
    def test_local_settings_clear_inherited_candidate_and_profiling(self):
        environment = {
            "RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE": "1",
            "RUSTYTRANSFER_BENCH_COMPLETION_PROFILE": "1",
            "RUSTYTRANSFER_BENCH_EXPLICIT_CLOSE": "1",
        }
        runner.configure_profile_env(environment, SimpleNamespace())
        self.assertEqual(environment, {})
        runner.configure_profile_env(environment, SimpleNamespace(
            completion_profile=True, explicit_connection_close=True,
        ))
        self.assertEqual(environment, {
            "RUSTYTRANSFER_BENCH_COMPLETION_PROFILE": "1",
            "RUSTYTRANSFER_BENCH_EXPLICIT_CLOSE": "1",
        })

    def test_remote_settings_are_explicit_and_do_not_inherit_candidate(self):
        def command(**settings):
            return runner.remote_process_command(
                SimpleNamespace(rusty_path="direct", **settings),
                "/bench/run", ["/bench/rusty", "send"],
                "/bench/run/metrics", "/bench/run/time", "/bench/run/log", "/bench/run/pid",
            )
        baseline = command(completion_profile=True)
        self.assertIn("-u RUSTYTRANSFER_BENCH_EXPLICIT_CLOSE", baseline)
        self.assertNotIn("RUSTYTRANSFER_BENCH_EXPLICIT_CLOSE=1", baseline)
        self.assertIn("RUSTYTRANSFER_BENCH_COMPLETION_PROFILE=1", baseline)
        self.assertIn("process_seconds", baseline)
        candidate = command(completion_profile=True, explicit_connection_close=True)
        self.assertIn("RUSTYTRANSFER_BENCH_EXPLICIT_CLOSE=1", candidate)

    def test_process_lifetimes_preserve_each_endpoint_without_summing_overlap(self):
        sender = dict(user_cpu_seconds=1, system_cpu_seconds=2, max_rss_kib=10, process_seconds=12.34)
        receiver = dict(user_cpu_seconds=3, system_cpu_seconds=4, max_rss_kib=20, process_seconds=10.25)
        result = runner.resource_values(sender, receiver)
        self.assertEqual(result["sender_process_seconds"], 12.34)
        self.assertEqual(result["receiver_process_seconds"], 10.25)
        self.assertEqual(result["sender_cpu_seconds"], 3)
        self.assertEqual(result["receiver_cpu_seconds"], 7)
        for bad in (True, -1, float("nan"), float("inf"), "12"):
            with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeError, "Invalid sender"):
                runner.resource_values({**sender, "process_seconds": bad}, receiver)
        legacy = runner.resource_values({k:v for k,v in sender.items() if k != "process_seconds"},
                                        {k:v for k,v in receiver.items() if k != "process_seconds"})
        self.assertNotIn("sender_process_seconds", legacy)

    def test_outer_timeline_does_not_change_scored_duration_or_payload_phases(self):
        rows = [{"role": role, "wall_seconds": 8, "payload_seconds": 5,
                 "effective_mib_per_second": 64} for role in ("sender", "receiver")]
        runner.attach_benchmark_timing(rows, 100, 101, 101.1, 108)
        for row in rows:
            self.assertEqual(row["wall_seconds"], 8)
            self.assertEqual(row["payload_seconds"], 5)
            self.assertEqual(row["effective_mib_per_second"], 64)
            self.assertEqual(row["benchmark_timing"]["ready_observed_seconds"], 1)
            self.assertEqual(row["benchmark_timing"]["pair_exit_observed_seconds"], 8)
            self.assertEqual(row["benchmark_timing"]["endpoint_process_resolution_seconds"], 0.01)
        with self.assertRaisesRegex(RuntimeError, "out of order"):
            runner.attach_benchmark_timing(rows, 100, 103, 102, 108)

    def test_invalid_or_unapplied_candidate_is_retained_before_rejection(self):
        profile = dict(setup_seconds=1, application_wall_seconds=10,
                       transfer_lifetime_seconds=8, protocol_confirmation_seconds=0.1,
                       finish_receiving_seconds=0.1, endpoint_close_seconds=0.1,
                       close_send_seconds=0.1, wait_peer_close_seconds=0.1,
                       explicit_connection_close_seconds=0.01,
                       explicit_connection_close_applied=True)
        row = dict(role="sender", completion_profile_enabled=True,
                   explicit_connection_close=True, completion_profile=profile,
                   shutdown_seconds=1, endpoint_process_seconds=10.01)
        self.assertIsNone(runner.completion_profile_rejection_reason(row))
        for bad in ({**row, "completion_profile": None},
                    {**row, "completion_profile": {**profile, "explicit_connection_close_applied": False}},
                    {**row, "completion_profile": {**profile, "endpoint_close_seconds": float("nan")}},
                    {**row, "completion_profile": {**profile, "endpoint_close_seconds": 2}}):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory:
                raw = Path(directory) / "rows.jsonl"
                with self.assertRaisesRegex(RuntimeError, "diagnostic rows were retained"):
                    runner.append_and_validate_rust_rows(raw, [bad], "direct", "direct")
                self.assertTrue(raw.exists())
                self.assertIn('"role": "sender"', raw.read_text())

    @unittest.skipUnless(Path('/usr/bin/time').exists(), 'requires GNU time')
    def test_endpoint_cwd_is_used_and_actual_process_elapsed_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timing = root / 'time.json'
            output = root / 'output.log'
            process, captured = runner.time_command(
                ['/usr/bin/env', 'pwd'], timing, output, {}, cwd=root,
            )
            self.assertEqual(process.wait(timeout=5), 0)
            captured.wait()
            process.stdout.close()
            self.assertEqual(output.read_text().strip(), str(root))
            report = runner.read_time(timing)
            self.assertIn('process_seconds', report)
            self.assertGreaterEqual(report['process_seconds'], 0)


if __name__ == "__main__":
    unittest.main()
