#!/usr/bin/env python3
"""Focused tests for the complete-report and numeric-floor gate."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
HELPER = SCRIPTS / "check-clippy-report.py"
spec = importlib.util.spec_from_file_location("check_clippy_report", HELPER)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def result(
    number: int = 0,
    *,
    fingerprint: str = "fingerprint",
    uri: str = "src/lib.rs",
) -> dict:
    return {
        "ruleId": "clippy::expect_used",
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": uri},
                    "region": {"byteOffset": number, "byteLength": 1},
                }
            }
        ],
        "partialFingerprints": {
            "primaryLocationLineHash": fingerprint,
        },
    }


def report(results=None, *, runs=None) -> dict:
    if runs is None:
        runs = [
            {
                "tool": {"driver": {"name": "clippy"}},
                "results": list(results or []),
            }
        ]
    return {"version": "2.1.0", "runs": runs}


class ReportGateTests(unittest.TestCase):
    def assert_rejected(self, sarif, floor, expected):
        with self.assertRaisesRegex(gate.ReportError, expected):
            gate.validate_report(sarif, floor)

    def run_cli(self, sarif, floor):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.sarif"
            path.write_text(json.dumps(sarif), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(HELPER),
                    str(path),
                    "--min-results",
                    floor,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_malformed_octal_like_floor_is_rejected(self):
        completed = self.run_cli(report([result()]), "17o")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("'17o'", completed.stderr)

    def test_empty_floor_is_rejected(self):
        self.assert_rejected(report([result()]), "", "non-empty")

    def test_other_non_decimal_floor_forms_are_rejected(self):
        for floor in ("+1", "-1", " 1", "1 ", "1.0"):
            with self.subTest(floor=floor):
                self.assert_rejected(report([result()]), floor, "decimal")

    def test_huge_all_digit_floor_uses_arbitrary_precision(self):
        floor = "9" * 5000
        completed = self.run_cli(report([result()]), floor)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("below the configured floor", completed.stderr)
        self.assertIn(repr(floor), completed.stderr)

    def test_equality_passes(self):
        self.assertEqual(
            gate.validate_report(report([result(0), result(1, fingerprint="second")]), "2"),
            2,
        )

    def test_below_floor_fails_closed(self):
        self.assert_rejected(report([result()]), "2", "below the configured floor")

    def test_valid_zero_result_run_requires_explicit_zero_floor(self):
        self.assertEqual(gate.validate_report(report([]), "0"), 0)
        self.assert_rejected(report([]), "1", "zero results")

    def test_missing_runs_are_rejected_even_at_zero(self):
        self.assert_rejected({"version": "2.1.0", "runs": []}, "0", "no runs")
        self.assert_rejected({"version": "2.1.0"}, "0", "must be a JSON array")

    def test_malformed_run_results_are_rejected(self):
        runs = [{"tool": {"driver": {"name": "clippy"}}}]
        self.assert_rejected(report(runs=runs), "0", "has no results array")
        runs = [
            {
                "tool": {"driver": {"name": "clippy"}},
                "results": {},
            }
        ]
        self.assert_rejected(report(runs=runs), "0", "must be a JSON array")

    def test_non_clippy_report_is_rejected(self):
        runs = [
            {
                "tool": {"driver": {"name": "osv-scanner"}},
                "results": [],
            }
        ]
        self.assert_rejected(report(runs=runs), "0", "not a Clippy run")

    def test_unfingerprintable_result_is_rejected(self):
        missing_fingerprint = result()
        del missing_fingerprint["partialFingerprints"]
        self.assert_rejected(
            report([missing_fingerprint]),
            "0",
            "partialFingerprints",
        )

        empty_fingerprint = result(fingerprint=" ")
        self.assert_rejected(report([empty_fingerprint]), "0", "primaryLocationLineHash")

        missing_region = result()
        del missing_region["locations"][0]["physicalLocation"]["region"]
        self.assert_rejected(report([missing_region]), "0", "region must be")

    def test_duplicate_identity_is_rejected(self):
        self.assert_rejected(
            report([result(0), result(1)]),
            "0",
            "duplicate Clippy finding identity",
        )

    def test_invalid_json_is_rejected_by_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.sarif"
            path.write_text("{", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(HELPER), str(path), "--min-results", "0"],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("not valid JSON", completed.stderr)


if __name__ == "__main__":
    unittest.main()
