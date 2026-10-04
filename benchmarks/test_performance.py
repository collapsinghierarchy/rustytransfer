import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import performance as p


CONFIG = json.loads((p.ROOT / "benchmarks/baseline.json").read_text())


def sample():
    expected = {"bytes": 64 * 1024 * 1024, "sha256": "a" * 64}
    pair = {"harness_version": 1, "elapsed_seconds": 4,
            "received_size_bytes": expected["bytes"], "source_sha256": "a" * 64,
            "received_sha256": "a" * 64}
    evidence = dict(verified=True, classification="direct", direct_stream_tx=1,
                    direct_stream_rx=0, relay_stream_tx=0, relay_stream_rx=0,
                    lagged=False, missing_path_stats=False, relay_selected=False)
    rows = [dict(role=role, success=True, transport="iroh", path="direct",
                 path_start="direct", path_end="direct", direct_route_verified_both=True,
                 size_bytes=expected["bytes"], bytes_transferred=expected["bytes"],
                 chunk_size=262144, pipeline_depth=1, profile_mode="standard",
                 source_sha256="a" * 64, received_sha256="a" * 64,
                 path_evidence=copy.deepcopy(evidence), payload_seconds=1,
                 handshake_seconds=1, shutdown_seconds=1) for role in ("sender", "receiver")]
    return pair, rows, expected


def trials(ratios):
    return [dict(size_mib=size, pair_index=i, warmup=False, build=arm,
                 sender_payload_seconds=1 if arm == "baseline" else 1 / ratio,
                 elapsed_seconds=2 if arm == "baseline" else 2 / ratio)
            for size in CONFIG["sizes_mib"] for i, ratio in enumerate(ratios, 1)
            for arm in ("baseline", "candidate")]


class PerformanceTests(unittest.TestCase):
    def test_schedule_one_warmup_five_alternating_pairs(self):
        order = list(p.schedule(5))
        self.assertEqual(len(order), 6)
        self.assertTrue(order[0][1])
        self.assertEqual([x[2][0] for x in order[1:]],
                         ["baseline", "candidate", "baseline", "candidate", "baseline"])

    def test_valid_pair_and_rejected_evidence(self):
        p.validate(*sample(), 1)
        for field, value in (("received_sha256", "b" * 64), ("received_size_bytes", 1),
                             ("elapsed_seconds", float("nan")), ("elapsed_seconds", True),
                             ("harness_version", 2)):
            pair, rows, expected = sample()
            pair[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                p.validate(pair, rows, expected, 1)
        for field, value in (("relay_stream_tx", 1), ("lagged", True),
                             ("missing_path_stats", True), ("verified", False)):
            pair, rows, expected = sample()
            rows[1]["path_evidence"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                p.validate(pair, rows, expected, 1)

    def test_missing_rows_and_invalid_phase(self):
        pair, rows, expected = sample()
        with self.assertRaises(ValueError):
            p.validate(pair, rows[:1], expected, 1)
        rows[0]["payload_seconds"] = 5
        with self.assertRaises(ValueError):
            p.validate(pair, rows, expected, 1)

    def test_statistics_keep_slow_samples_and_do_not_count_endpoints(self):
        for ratios, decision in (([1] * 5, "no-regression-observed"),
                                 ([0.7] * 5, "regression-observed"),
                                 ([1, 1, 1, 1, 0.3], "inconclusive")):
            result = p.compare(trials(ratios), CONFIG)[0]
            self.assertEqual(result["decision"], decision)
            self.assertEqual(result["paired_payload_rate_ratios"], ratios)
        with self.assertRaises(ValueError):
            p.compare(trials([1] * 4), CONFIG)
        duplicated = trials([1] * 5)
        duplicated[1]["build"] = "baseline"
        with self.assertRaises(ValueError):
            p.compare(duplicated, CONFIG)

    def test_environment_isolates_controls(self):
        with patch.dict(os.environ, {"RUSTYTRANSFER_BENCH_EXPLICIT_CLOSE": "1",
                                    "RUSTFLAGS": "bad", "CARGO_PROFILE_RELEASE_OPT_LEVEL": "0"}):
            env = p.trial_environment()
        self.assertFalse(any(k.startswith(("RUSTYTRANSFER_", "CARGO_")) for k in env))
        self.assertNotIn("RUSTFLAGS", env)

    def test_deterministic_fixture(self):
        with tempfile.TemporaryDirectory() as root:
            a = p.fixture(Path(root) / "a", 1)
            b = p.fixture(Path(root) / "b", 1)
            self.assertEqual(a["sha256"], b["sha256"])
            self.assertEqual(a["bytes"], 1024 * 1024)

    def test_timeout_retains_log_and_stops_process(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "log"
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                p.run([sys.executable, "-u", "-c", "import time; print('started'); time.sleep(10)"],
                      log=log, timeout=0.2)
            self.assertIn("started", log.read_text())

    def test_full_sha_required(self):
        for ref in ("HEAD", "abc", "a" * 39, "A" * 40, None):
            with self.assertRaises(ValueError):
                p.resolve(ref)

    def test_failure_retains_report_and_cleans_only_owned_sources(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "run"
            preserved = Path(root) / "user-fixture"
            preserved.write_text("keep")

            def failed_build(ref, label, directory, environment, config):
                for name in ("sources", "fixtures"):
                    (directory / name).mkdir()
                    (directory / name / "owned").write_text("temporary")
                raise RuntimeError("synthetic build failure")

            args = ["performance.py", "--profile", "ci", "--baseline-ref", CONFIG["baseline_ref"],
                    "--candidate-ref", "b" * 40, "--output", str(output)]
            with patch.object(sys, "argv", args), patch.object(p, "resolve", side_effect=lambda x: x), \
                    patch.object(p, "run", return_value="performance harness v1"), \
                    patch.object(p, "build", side_effect=failed_build):
                with self.assertRaisesRegex(RuntimeError, "synthetic build failure"):
                    p.main()
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "invalid")
            self.assertTrue((output / "summary.md").exists())
            self.assertFalse((output / "sources").exists())
            self.assertFalse((output / "fixtures").exists())
            self.assertEqual(preserved.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
