#!/usr/bin/env python3
"""Focused, network-free tests for alert-lifecycle.py."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "alert-lifecycle.py"
SPEC = importlib.util.spec_from_file_location("alert_lifecycle", MODULE_PATH)
assert SPEC and SPEC.loader
alert_lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alert_lifecycle)


class FixtureClient:
    """Small fake for the GhClient interface, retaining every request."""

    def __init__(self, gets=None, pages=None):
        self.get_fixtures = gets or {}
        self.page_fixtures = pages or {}
        self.get_calls = []
        self.page_calls = []

    def get(self, endpoint, accept=alert_lifecycle.DEFAULT_ACCEPT):
        self.get_calls.append((endpoint, accept))
        key = (endpoint, accept)
        value = self.get_fixtures.get(key, self.get_fixtures.get(endpoint))
        if value is None:
            raise AssertionError(f"no GET fixture for {key!r}")
        return copy.deepcopy(value)

    def pages(self, endpoint, params=None, accept=alert_lifecycle.DEFAULT_ACCEPT):
        params = params or {}
        normalized = tuple(sorted(params.items()))
        self.page_calls.append((endpoint, normalized, accept))
        key = (endpoint, normalized, accept)
        value = self.page_fixtures.get(key, self.page_fixtures.get(endpoint))
        if value is None:
            raise AssertionError(f"no page fixture for {key!r}")
        return copy.deepcopy(value)


def alert(number, state, path="src/lib.rs", rule_id="clippy::unwrap_used"):
    return {
        "number": number,
        "state": state,
        "rule": {"id": rule_id},
        "most_recent_instance": {
            "ref": "refs/heads/main",
            "commit_sha": "base-sha",
            "location": {"path": path, "start_line": number},
        },
    }


def sarif_result(number, fingerprint, path="src/lib.rs", rule_id="clippy::unwrap_used"):
    return {
        "ruleId": rule_id,
        "partialFingerprints": {"primaryLocationLineHash": fingerprint},
        "properties": {"github/alertNumber": number},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": path},
                    "region": {"startLine": number, "startColumn": 1},
                }
            }
        ],
        "message": {"text": "diagnostic"},
    }


def sarif(*results):
    return {"version": "2.1.0", "runs": [{"results": list(results)}]}


def record(
    number,
    state,
    fingerprint,
    path="src/lib.rs",
    rule_id="clippy::unwrap_used",
    fixed_at_present=None,
):
    if fixed_at_present is None:
        fixed_at_present = state == "fixed"
    return {
        "record_key": f"alert:{number}:1",
        "alert_number": number,
        "state": state,
        "rule_id": rule_id,
        "path": path,
        "fingerprint": fingerprint,
        "dismissed_reason": "used in tests" if state == "dismissed" else None,
        "dismissed_comment": "lifecycle fixture" if state == "dismissed" else None,
        "fixed_at_present": fixed_at_present,
        "present_in_sarif": True,
        "scope": {
            "repository": "owner/repo",
            "ref": "refs/heads/main",
            "tool": "clippy",
            "category": "Code Scanner",
        },
        "evidence": {"api": {"number": number}},
    }


def snapshot(analysis_id, records, commit="base-sha"):
    return {
        "schema": alert_lifecycle.SCHEMA,
        "captured_at": "2026-09-14T00:00:00+00:00",
        "scope": {
            "repository": "owner/repo",
            "ref": "refs/heads/main",
            "tool": "clippy",
            "category": "Code Scanner",
            "commit_sha": commit,
        },
        "analysis": {
            "id": str(analysis_id),
            "sarif_id": f"sarif-{analysis_id}",
            "ref": "refs/heads/main",
            "commit_sha": commit,
            "tool": "clippy",
            "category": "Code Scanner",
        },
        "sarif": {"downloaded": True, "results": []},
        "alerts": records,
    }


def worker_metadata(
    run_id="20",
    run_attempt="1",
    commit_sha="base-sha",
    sarif_id="sarif-10",
    upload_outcome="success",
    report_complete="true",
):
    return {
        "schema_version": 1,
        "artifact_name": f"clippy-sarif-{run_id}-{run_attempt}",
        "run_id": run_id,
        "run_attempt": run_attempt,
        "ref": "refs/heads/main",
        "commit_sha": commit_sha,
        "tool": "clippy",
        "category": "Code Scanner",
        "sarif_id": sarif_id,
        "report_complete": report_complete,
        "clippy_step_outcome": "success",
        "upload_step_outcome": upload_outcome,
        "configured_floor": "150",
        "files": {
            "raw_clippy_json": "target/clippy.json",
            "uploaded_clippy_sarif": "target/clippy.sarif",
        },
        "versions": {"rustc": "rustc 1.97.0"},
    }


class AlertLifecycleTests(unittest.TestCase):
    def test_worker_metadata_matches_the_fixed_artifact_schema(self):
        normalized = alert_lifecycle.validate_worker_metadata(
            worker_metadata(),
            "owner/repo",
            "refs/heads/main",
            "clippy",
            "Code Scanner",
            "base-sha",
            "20",
            1,
        )
        self.assertEqual(normalized["run_id"], "20")
        self.assertEqual(normalized["run_attempt"], "1")
        self.assertEqual(normalized["sarif_id"], "sarif-10")

        malformed = worker_metadata()
        del malformed["commit_sha"]
        with self.assertRaisesRegex(
            alert_lifecycle.LifecycleError,
            r"missing fields: worker_metadata\.commit_sha",
        ):
            alert_lifecycle.validate_worker_metadata(
                malformed,
                "owner/repo",
                "refs/heads/main",
                "clippy",
                "Code Scanner",
                "base-sha",
            )

    def test_fixed_at_is_preserved_when_api_state_remains_dismissed(self):
        item = alert(7, "dismissed")
        item["fixed_at"] = "2026-09-14T00:00:00Z"
        normalized = alert_lifecycle.normalize_alert(
            item, "dismissed", "refs/heads/main", "clippy", "Code Scanner"
        )
        self.assertEqual(normalized["state"], "dismissed")
        self.assertTrue(normalized["fixed_at_present"])

    def test_collect_alerts_follows_pages_and_preserves_records(self):
        endpoint = "repos/owner/repo/code-scanning/alerts"
        pages = {
            (endpoint, tuple(sorted({"state": "open", "tool_name": "clippy", "ref": "refs/heads/main", "per_page": 100}.items())), alert_lifecycle.DEFAULT_ACCEPT): [
                [alert(1, "open"),],
                [alert(2, "open"),],
            ],
            (endpoint, tuple(sorted({"state": "dismissed", "tool_name": "clippy", "ref": "refs/heads/main", "per_page": 100}.items())), alert_lifecycle.DEFAULT_ACCEPT): [[]],
            (endpoint, tuple(sorted({"state": "fixed", "tool_name": "clippy", "ref": "refs/heads/main", "per_page": 100}.items())), alert_lifecycle.DEFAULT_ACCEPT): [[]],
        }
        client = FixtureClient(pages=pages)
        records = alert_lifecycle.collect_alerts(
            client, "owner/repo", "refs/heads/main", "clippy", "Code Scanner"
        )
        self.assertEqual([item["alert_number"] for item in records], [1, 2])
        self.assertEqual(len(client.page_calls), 3)

    def test_missing_fingerprint_and_alert_number_are_explicit(self):
        missing_fingerprint = sarif(
            {
                "ruleId": "clippy::unwrap_used",
                "locations": [{}],
                "properties": {"github/alertNumber": 1},
            }
        )
        with self.assertRaisesRegex(
            alert_lifecycle.LifecycleError,
            r"missing fields: .*partialFingerprints\.primaryLocationLineHash",
        ):
            alert_lifecycle.extract_sarif_results(missing_fingerprint)

        missing_number = sarif(
            {
                "ruleId": "clippy::unwrap_used",
                "partialFingerprints": {"primaryLocationLineHash": "fp"},
                "locations": [{}],
                "properties": {},
            }
        )
        with self.assertRaisesRegex(
            alert_lifecycle.LifecycleError,
            r"missing fields: .*properties\.github/alertNumber",
        ):
            alert_lifecycle.extract_sarif_results(missing_number)

    def test_uploaded_artifact_is_compared_without_location_guessing(self):
        uploaded = sarif(sarif_result(1, "fp-1"))
        downloaded = sarif(sarif_result(1, "fp-1"))
        results, label = alert_lifecycle.validate_uploaded_artifact(
            uploaded, downloaded
        )
        self.assertEqual(label, "uploaded SARIF")
        self.assertEqual(results[0]["fingerprint"], "fp-1")

        changed = sarif(sarif_result(1, "fp-2"))
        with self.assertRaisesRegex(alert_lifecycle.LifecycleError, "artifact mismatch"):
            alert_lifecycle.validate_uploaded_artifact(uploaded, changed)

    def test_duplicate_fingerprint_does_not_overwrite_distinct_alerts(self):
        api_records = [
            alert_lifecycle.normalize_alert(
                alert(1, "open"), "open", "refs/heads/main", "clippy", "Code Scanner"
            ),
            alert_lifecycle.normalize_alert(
                alert(2, "open"), "open", "refs/heads/main", "clippy", "Code Scanner"
            ),
        ]
        results = alert_lifecycle.extract_sarif_results(
            sarif(sarif_result(1, "same"), sarif_result(2, "same"))
        )
        merged, association = alert_lifecycle.merge_alert_evidence(
            api_records, results, None, "refs/heads/main", "clippy", "Code Scanner"
        )
        self.assertEqual([item["alert_number"] for item in merged], [1, 2])
        self.assertEqual(association["fingerprint_alert_numbers"]["same"], [1, 2])

    def test_duplicate_fingerprint_for_one_alert_fails_loudly(self):
        api_records = [
            alert_lifecycle.normalize_alert(
                alert(1, "open"), "open", "refs/heads/main", "clippy", "Code Scanner"
            )
        ]
        results = alert_lifecycle.extract_sarif_results(
            sarif(sarif_result(1, "same"), sarif_result(1, "same"))
        )
        with self.assertRaisesRegex(
            alert_lifecycle.LifecycleError, "duplicate fingerprint collision"
        ):
            alert_lifecycle.merge_alert_evidence(
                api_records, results, None, "refs/heads/main", "clippy", "Code Scanner"
            )

    def test_missing_current_api_alert_state_fails_capture(self):
        previous = snapshot(10, [record(1, "dismissed", "fp-1")])
        with self.assertRaisesRegex(
            alert_lifecycle.LifecycleError,
            r"current alert state missing for baseline alert\(s\): \[1\]",
        ):
            alert_lifecycle.merge_alert_evidence(
                [], [], previous, "refs/heads/main", "clippy", "Code Scanner"
            )

    def test_missing_alert_state_field_fails_normalization(self):
        item = alert(1, "open")
        del item["state"]
        with self.assertRaisesRegex(
            alert_lifecycle.LifecycleError, r"missing fields: alerts\[\]\.state"
        ):
            alert_lifecycle.normalize_alert(
                item, "open", "refs/heads/main", "clippy", "Code Scanner"
            )

    def test_stale_analysis_is_rejected_even_when_it_is_the_latest_fixture(self):
        endpoint = "repos/owner/repo/code-scanning/analyses"
        analysis = {
            "id": 10,
            "ref": "refs/heads/main",
            "commit_sha": "old-sha",
            "category": "Code Scanner",
            "tool": {"name": "clippy"},
        }
        client = FixtureClient(pages={endpoint: [[analysis]]})
        with self.assertRaisesRegex(
            alert_lifecycle.LifecycleError, "new analysis verification failed"
        ):
            alert_lifecycle.select_analysis(
                client,
                "owner/repo",
                "refs/heads/main",
                "clippy",
                "Code Scanner",
                "new-sha",
                "sarif-new",
                {},
            )

        current = {
            "id": "10",
            "sarif_id": "sarif-new",
            "ref": "refs/heads/main",
            "commit_sha": "new-sha",
            "tool": "clippy",
            "category": "Code Scanner",
        }
        with self.assertRaisesRegex(alert_lifecycle.LifecycleError, "stale analysis"):
            alert_lifecycle.assert_new_analysis(
                current, {"analysis": {"id": "10", "sarif_id": "sarif-old"}}
            )

    def test_normalized_diff_ignores_timestamps_and_source_coordinates(self):
        before = snapshot(10, [record(1, "open", "fp-1")])
        after = snapshot(11, [record(1, "open", "fp-1")], commit="new-sha")
        after["captured_at"] = "2026-09-14T01:00:00+00:00"
        after["alerts"][0]["evidence"]["api"]["location"] = {"start_line": 900}
        self.assertEqual(
            alert_lifecycle.normalized_diff(before, after),
            {"baseline_only": [], "current_only": [], "changed": []},
        )

    def test_uploaded_sarif_comparison_is_order_independent(self):
        uploaded = sarif(sarif_result(1, "fp-1"), sarif_result(2, "fp-2"))
        downloaded = sarif(sarif_result(2, "fp-2"), sarif_result(1, "fp-1"))
        results, _ = alert_lifecycle.validate_uploaded_artifact(uploaded, downloaded)
        self.assertEqual(
            {item["alert_number"] for item in results},
            {1, 2},
        )

    def test_normalized_alert_diff_is_order_independent(self):
        before = snapshot(10, [record(1, "open", "fp-1"), record(2, "open", "fp-2")])
        after = snapshot(
            11,
            [record(2, "open", "fp-2"), record(1, "open", "fp-1")],
            commit="new-sha",
        )
        self.assertEqual(
            alert_lifecycle.normalized_diff(before, after),
            {"baseline_only": [], "current_only": [], "changed": []},
        )

    def test_affected_cohort_allows_only_declared_change(self):
        before = snapshot(10, [record(1, "open", "fp-1"), record(2, "open", "fp-2")])
        after = snapshot(11, [record(1, "dismissed", "fp-1"), record(2, "open", "fp-2")], commit="new-sha")
        result = alert_lifecycle.compare_with_expectation(
            before,
            after,
            {
                "trial": "T1",
                "affected": [
                    {"alert_number": 1, "expect": {"state": "dismissed", "fingerprint": "same"}}
                ],
            },
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual([item["alert_number"] for item in result["diff"]["changed"]], [1])

    def test_restore_oracle_allows_only_explicit_new_fixed_history(self):
        before = snapshot(10, [record(1, "open", "fp-1"), record(2, "dismissed", "fp-2")])
        after = snapshot(
            11,
            [record(1, "open", "fp-1"), record(2, "dismissed", "fp-2"), record(3, "fixed", "fp-3")],
            commit="restored-sha",
        )
        result = alert_lifecycle.compare_with_expectation(
            before,
            after,
            {
                "trial": "restore",
                "restore": {
                    "active_baseline": "unchanged",
                    "allowed_fixed_history": [3],
                },
            },
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual([item["alert_number"] for item in result["diff"]["current_only"]], [3])

        with self.assertRaisesRegex(alert_lifecycle.LifecycleError, "unexpected added alert 3"):
            alert_lifecycle.compare_with_expectation(
                before,
                after,
                {"trial": "restore", "restore": {"active_baseline": "unchanged"}},
            )

        dismissed_but_fixed = snapshot(
            12,
            [
                record(1, "open", "fp-1"),
                record(2, "dismissed", "fp-2"),
                record(4, "dismissed", "fp-4", fixed_at_present=True),
            ],
            commit="restored-sha",
        )
        result = alert_lifecycle.compare_with_expectation(
            before,
            dismissed_but_fixed,
            {
                "trial": "restore-dismissed-fixed",
                "restore": {
                    "active_baseline": "unchanged",
                    "allowed_fixed_history": [4],
                },
            },
        )
        self.assertEqual(result["status"], "PASS")

    def test_restore_oracle_rejects_changed_baseline_even_if_it_is_dismissed(self):
        before = snapshot(10, [record(1, "dismissed", "fp-1")])
        after = snapshot(11, [record(1, "open", "fp-1")], commit="restored-sha")
        with self.assertRaisesRegex(alert_lifecycle.LifecycleError, "restore changed baseline alert 1"):
            alert_lifecycle.compare_with_expectation(
                before,
                after,
                {
                    "trial": "restore",
                    "restore": {"active_baseline": "unchanged"},
                },
            )

    def test_survivor_map_dispatches_and_evaluates_actual_mapping(self):
        before = snapshot(10, [record(1, "dismissed", "dup:1"), record(2, "open", "dup:2")])
        after = snapshot(11, [record(2, "open", "dup:1"), record(3, "open", "dup:2")], commit="new-sha")
        before["alerts"][0]["dismissed_comment"] = "T5_ORACLE_A"
        after["alerts"][0]["dismissed_comment"] = None
        after["alerts"][1]["dismissed_comment"] = "T5_ORACLE_A"
        result = alert_lifecycle.compare_with_expectation(
            before,
            after,
            {
                "trial": "T5",
                "mode": "survivor-map",
                "survivor_map": {
                    "survivors": [
                        {
                            "before_alert_number": 1,
                            "after_alert_number": 3,
                            "source_occurrence": {
                                "primary_fingerprint": "dup:1",
                                "comment": "T5_ORACLE_A",
                            },
                        },
                        {
                            "before_alert_number": 2,
                            "after_alert_number": 2,
                            "source_occurrence": {"primary_fingerprint": "dup:2"},
                        },
                    ],
                    "expected_removed": [],
                    "expected_added": [],
                },
            },
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["mode"], "survivor-map")
        self.assertTrue(any("did not keep its alert number" in item for item in result["failures"]))
        self.assertEqual(result["survivors"][0]["after_alert_number"], 3)

    def test_survivor_map_passes_when_actual_numbers_and_state_survive(self):
        before = snapshot(10, [record(1, "dismissed", "dup:1")])
        after = snapshot(11, [record(1, "dismissed", "dup:1")], commit="new-sha")
        before["alerts"][0]["dismissed_comment"] = "T5_ORACLE_A"
        after["alerts"][0]["dismissed_comment"] = "T5_ORACLE_A"
        result = alert_lifecycle.compare_with_expectation(
            before,
            after,
            {
                "trial": "T5",
                "mode": "survivor-map",
                "survivor_map": {
                    "survivors": [
                        {
                            "before_alert_number": 1,
                            "after_alert_number": 1,
                            "source_occurrence": {
                                "primary_fingerprint": "dup:1",
                                "comment": "T5_ORACLE_A",
                            },
                        }
                    ]
                },
            },
        )
        self.assertEqual(result["status"], "PASS")

    def test_old_stateless_limitation_mode_is_rejected(self):
        before = snapshot(10, [record(1, "open", "dup:1")])
        after = snapshot(11, [record(1, "open", "dup:1")], commit="new-sha")
        with self.assertRaisesRegex(alert_lifecycle.LifecycleError, "deprecated"):
            alert_lifecycle.compare_with_expectation(
                before,
                after,
                {"trial": "T5", "mode": "stateless-limitation", "affected": [1]},
            )

    def test_skipped_upload_step_requires_exact_job_and_step_state(self):
        endpoint = "repos/owner/repo/actions/runs/20/jobs"
        client = FixtureClient(
            pages={
                endpoint: [
                    {
                        "jobs": [
                            {
                                "id": 21,
                                "name": "clippy",
                                "steps": [
                                    {
                                        "name": "Upload Clippy SARIF report",
                                        "status": "completed",
                                        "conclusion": "skipped",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        )
        evidence = alert_lifecycle.verify_upload_skipped(
            client, "owner/repo", "20", "clippy", "Upload Clippy SARIF report"
        )
        self.assertEqual(evidence["conclusion"], "skipped")


if __name__ == "__main__":
    unittest.main()
