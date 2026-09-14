#!/usr/bin/env python3
"""Read-only GitHub Code Scanning alert snapshots and comparisons.

The command deliberately has no write-capable GitHub operation.  It accepts a
SARIF upload id or an analysis id, verifies the complete analysis scope, and
stores both stable comparison data and the evidence used to produce it.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode


SCHEMA = "rustytransfer-alert-lifecycle/v1"
DEFAULT_TOOL = "clippy"
DEFAULT_CATEGORY = "Code Scanner"
DEFAULT_STEP = "Upload Clippy SARIF report"
DEFAULT_JOB = "clippy"
DEFAULT_ACCEPT = "application/vnd.github+json"
SARIF_ACCEPT = "application/sarif+json"
ALERT_STATES = ("open", "dismissed", "fixed")
TERMINAL_SARIF_STATES = {"complete", "failed", "error"}


class LifecycleError(RuntimeError):
    """A verification failure that should make the harness exit non-zero."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LifecycleError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LifecycleError(f"JSON document {path} must be an object")
    return value


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def repo_endpoint(repo: str, suffix: str) -> str:
    return f"repos/{repo}/{suffix.lstrip('/')}"


class GhClient:
    """The only GitHub transport used by this file.

    The command line is hard-coded to GET and never accepts a method argument.
    That makes accidental POST/PATCH/DELETE calls impossible through the
    harness.
    """

    def __init__(self, gh: str = "gh") -> None:
        self.gh = gh

    def get(self, endpoint: str, accept: str = DEFAULT_ACCEPT) -> Any:
        command = [
            self.gh,
            "api",
            "--method",
            "GET",
            endpoint,
            "-H",
            f"Accept: {accept}",
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise LifecycleError(
                f"gh GET {endpoint} failed ({completed.returncode}): {detail}"
            )
        try:
            return json.loads(completed.stdout)
        except ValueError as exc:
            raise LifecycleError(
                f"gh GET {endpoint} returned non-JSON output"
            ) from exc

    def pages(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        accept: str = DEFAULT_ACCEPT,
    ) -> List[Any]:
        query = {key: value for key, value in (params or {}).items() if value is not None}
        if query:
            separator = "&" if "?" in endpoint else "?"
            endpoint = endpoint + separator + urlencode(query)
        command = [
            self.gh,
            "api",
            "--method",
            "GET",
            "--paginate",
            "--slurp",
            endpoint,
            "-H",
            f"Accept: {accept}",
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise LifecycleError(
                f"gh paginated GET {endpoint} failed ({completed.returncode}): {detail}"
            )
        try:
            value = json.loads(completed.stdout)
        except ValueError as exc:
            raise LifecycleError(
                f"gh paginated GET {endpoint} returned non-JSON output"
            ) from exc
        if not isinstance(value, list):
            raise LifecycleError(f"paginated GET {endpoint} did not return pages")
        return value


def page_items(pages: Iterable[Any], key: Optional[str] = None) -> Iterable[Any]:
    """Yield items from gh's --paginate --slurp result without overwriting."""

    for page in pages:
        if isinstance(page, list):
            yield from page
        elif isinstance(page, dict) and key is not None:
            values = page.get(key)
            if not isinstance(values, list):
                raise LifecycleError(f"paginated response is missing list field {key}")
            yield from values
        elif isinstance(page, dict) and key is None:
            # GitHub normally returns a JSON array for these endpoints.  Keep
            # accepting the object envelope used by fixture servers and older
            # API proxies, while refusing a single unrecognised object.
            for envelope_key in ("analyses", "alerts", "jobs"):
                values = page.get(envelope_key)
                if isinstance(values, list):
                    yield from values
                    break
            else:
                raise LifecycleError("paginated response has an unexpected shape")
        else:
            raise LifecycleError("paginated response has an unexpected shape")


def missing_fields(fields: Iterable[str]) -> None:
    values = sorted(set(fields))
    if values:
        raise LifecycleError("missing fields: " + ", ".join(values))


def as_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise LifecycleError(f"field {field} must be a positive integer")
    try:
        number = int(value)
    except ValueError as exc:
        raise LifecycleError(f"field {field} must be a positive integer") from exc
    if number <= 0:
        raise LifecycleError(f"field {field} must be a positive integer")
    return number


def analysis_tool_name(analysis: Dict[str, Any]) -> Optional[str]:
    tool = analysis.get("tool")
    return tool.get("name") if isinstance(tool, dict) else None


def validate_analysis(
    analysis: Any,
    repo: str,
    ref: str,
    tool: str,
    category: str,
    commit_sha: str,
) -> Dict[str, Any]:
    if not isinstance(analysis, dict):
        raise LifecycleError("analysis response must be an object")
    required = []
    for field in ("id", "ref", "commit_sha", "category"):
        if analysis.get(field) in (None, ""):
            required.append(f"analysis.{field}")
    if analysis_tool_name(analysis) in (None, ""):
        required.append("analysis.tool.name")
    missing_fields(required)

    actual_tool = analysis_tool_name(analysis)
    if analysis["ref"] != ref:
        raise LifecycleError(
            f"stale/wrong analysis: ref {analysis['ref']!r}, expected full ref {ref!r}"
        )
    if analysis["commit_sha"] != commit_sha:
        raise LifecycleError(
            f"stale/wrong analysis: commit_sha {analysis['commit_sha']!r}, "
            f"expected {commit_sha!r}"
        )
    if actual_tool != tool:
        raise LifecycleError(
            f"stale/wrong analysis: tool {actual_tool!r}, expected {tool!r}"
        )
    if analysis["category"] != category:
        raise LifecycleError(
            f"stale/wrong analysis: category {analysis['category']!r}, "
            f"expected {category!r}"
        )

    return {
        "id": str(analysis["id"]),
        "sarif_id": (
            str(analysis["sarif_id"])
            if analysis.get("sarif_id") is not None
            else None
        ),
        "ref": analysis["ref"],
        "commit_sha": analysis["commit_sha"],
        "tool": actual_tool,
        "category": analysis["category"],
        "run_attempt": analysis.get("run_attempt"),
        "created_at": analysis.get("created_at"),
        "results_count": analysis.get("results_count"),
        "rules_count": analysis.get("rules_count"),
        "deletable": analysis.get("deletable"),
        "raw": analysis,
        "repository": repo,
    }


def validate_run(
    run: Any, run_id: str, expected_sha: str, expected_ref: Optional[str] = None
) -> Dict[str, Any]:
    if not isinstance(run, dict):
        raise LifecycleError("Actions run response must be an object")
    required = []
    if run.get("id") in (None, ""):
        required.append("run.id")
    if run.get("head_sha") in (None, ""):
        required.append("run.head_sha")
    if run.get("status") in (None, ""):
        required.append("run.status")
    missing_fields(required)
    if str(run["id"]) != str(run_id):
        raise LifecycleError(f"wrong Actions run id {run['id']!r}, expected {run_id!r}")
    if run["head_sha"] != expected_sha:
        raise LifecycleError(
            f"wrong Actions run head_sha {run['head_sha']!r}, expected {expected_sha!r}"
        )
    if run["status"] != "completed":
        raise LifecycleError(
            f"Actions run {run_id} is {run['status']!r}; wait for completion"
        )
    if expected_ref and expected_ref.startswith("refs/heads/"):
        branch = run.get("head_branch")
        expected_branch = expected_ref.removeprefix("refs/heads/")
        if branch is not None and branch != expected_branch:
            raise LifecycleError(
                f"wrong Actions run branch {branch!r}, expected {expected_branch!r}"
            )
    return {
        "id": str(run["id"]),
        "run_attempt": run.get("run_attempt"),
        "status": run["status"],
        "conclusion": run.get("conclusion"),
        "head_sha": run["head_sha"],
        "head_branch": run.get("head_branch"),
        "event": run.get("event"),
        "raw": run,
    }


def poll_sarif(
    client: Any,
    repo: str,
    sarif_id: str,
    timeout: float,
    interval: float,
) -> Dict[str, Any]:
    endpoint = repo_endpoint(repo, f"code-scanning/sarifs/{sarif_id}")
    deadline = time.monotonic() + timeout
    while True:
        status = client.get(endpoint)
        if not isinstance(status, dict):
            raise LifecycleError("SARIF processing response must be an object")
        processing = status.get("processing_status")
        if processing in (None, ""):
            raise LifecycleError("missing fields: sarif.processing_status")
        if processing == "complete":
            return status
        if processing in {"failed", "error"}:
            raise LifecycleError(
                f"SARIF {sarif_id} processing failed: "
                f"errors={status.get('errors')!r} warnings={status.get('warnings')!r}"
            )
        if time.monotonic() >= deadline:
            raise LifecycleError(
                f"timed out waiting for SARIF {sarif_id}; last status {processing!r}"
            )
        time.sleep(max(0.0, interval))


def analysis_matches(
    analysis: Any,
    ref: str,
    tool: str,
    category: str,
    commit_sha: str,
    sarif_id: Optional[str] = None,
) -> bool:
    if not isinstance(analysis, dict):
        return False
    if analysis.get("ref") != ref or analysis.get("commit_sha") != commit_sha:
        return False
    if analysis.get("category") != category or analysis_tool_name(analysis) != tool:
        return False
    exposed_sarif_id = analysis.get("sarif_id")
    return (
        sarif_id is None
        or exposed_sarif_id is None
        or str(exposed_sarif_id) == str(sarif_id)
    )


def select_analysis(
    client: Any,
    repo: str,
    ref: str,
    tool: str,
    category: str,
    commit_sha: str,
    sarif_id: str,
    status: Dict[str, Any],
) -> Dict[str, Any]:
    analyses_url = status.get("analyses_url")
    if analyses_url:
        pages = client.pages(analyses_url)
    else:
        pages = client.pages(
            repo_endpoint(repo, "code-scanning/analyses"),
            {"ref": ref, "tool_name": tool, "per_page": 100},
        )
    candidates = [
        item
        for item in page_items(pages, key=None)
        if analysis_matches(item, ref, tool, category, commit_sha, sarif_id)
    ]
    if len(candidates) != 1:
        raise LifecycleError(
            "new analysis verification failed: expected exactly one completed "
            f"analysis for ref={ref!r}, tool={tool!r}, category={category!r}, "
            f"commit_sha={commit_sha!r}, sarif_id={sarif_id!r}; found {len(candidates)}"
        )
    return candidates[0]


def assert_new_analysis(
    analysis: Dict[str, Any], previous: Optional[Dict[str, Any]]
) -> None:
    if not previous:
        return
    previous_analysis = previous.get("analysis") or {}
    current_id = str(analysis.get("id"))
    previous_id = previous_analysis.get("id")
    if previous_id is not None and current_id == str(previous_id):
        raise LifecycleError(
            f"stale analysis: current analysis id {current_id} is the baseline analysis"
        )
    current_sarif = analysis.get("sarif_id")
    previous_sarif = previous_analysis.get("sarif_id")
    if current_sarif and previous_sarif and current_sarif == previous_sarif:
        raise LifecycleError(
            f"stale SARIF upload: current sarif_id {current_sarif} is the baseline upload"
        )


def extract_sarif_results(
    sarif: Any,
    require_alert_numbers: bool = True,
    label: str = "sarif",
) -> List[Dict[str, Any]]:
    if not isinstance(sarif, dict):
        raise LifecycleError("downloaded SARIF must be an object")
    runs = sarif.get("runs")
    if not isinstance(runs, list):
        missing_fields([f"{label}.runs"])
    evidence: List[Dict[str, Any]] = []
    missing: List[str] = []
    for run_index, run in enumerate(runs):
        if not isinstance(run, dict):
            missing.append(f"{label}.runs[{run_index}]")
            continue
        results = run.get("results")
        if results is None:
            continue
        if not isinstance(results, list):
            missing.append(f"{label}.runs[{run_index}].results")
            continue
        for result_index, result in enumerate(results):
            prefix = f"{label}.runs[{run_index}].results[{result_index}]"
            if not isinstance(result, dict):
                missing.append(prefix)
                continue
            locations = result.get("locations")
            if not isinstance(locations, list) or not locations:
                missing.append(f"{prefix}.locations[0]")
            fingerprints = result.get("partialFingerprints")
            fingerprint = (
                fingerprints.get("primaryLocationLineHash")
                if isinstance(fingerprints, dict)
                else None
            )
            if not isinstance(fingerprint, str) or not fingerprint:
                missing.append(
                    f"{prefix}.partialFingerprints.primaryLocationLineHash"
                )
            properties = result.get("properties")
            alert_number = (
                properties.get("github/alertNumber")
                if isinstance(properties, dict)
                else None
            )
            if require_alert_numbers and alert_number in (None, ""):
                missing.append(f"{prefix}.properties.github/alertNumber")
            elif require_alert_numbers:
                try:
                    alert_number = as_positive_int(
                        alert_number, f"{prefix}.properties.github/alertNumber"
                    )
                except LifecycleError:
                    missing.append(f"{prefix}.properties.github/alertNumber")
            elif alert_number not in (None, ""):
                try:
                    alert_number = as_positive_int(
                        alert_number, f"{prefix}.properties.github/alertNumber"
                    )
                except LifecycleError:
                    missing.append(f"{prefix}.properties.github/alertNumber")

            location = locations[0] if isinstance(locations, list) and locations else {}
            physical = location.get("physicalLocation") if isinstance(location, dict) else {}
            if not isinstance(physical, dict):
                physical = {}
            artifact = physical.get("artifactLocation")
            artifact_uri = artifact.get("uri") if isinstance(artifact, dict) else None
            region = physical.get("region")
            evidence.append(
                {
                    "run_index": run_index,
                    "result_index": result_index,
                    "rule_id": result.get("ruleId"),
                    "fingerprint": fingerprint,
                    "alert_number": alert_number,
                    "artifact_uri": artifact_uri,
                    "region": copy.deepcopy(region) if isinstance(region, dict) else None,
                    "message": copy.deepcopy(result.get("message")),
                    "level": result.get("level"),
                    "raw_result": copy.deepcopy(result),
                }
            )
    missing_fields(missing)
    return evidence


def validate_uploaded_artifact(
    uploaded: Any, downloaded: Any, artifact_label: str = "uploaded SARIF"
) -> Tuple[List[Dict[str, Any]], str]:
    """Check the enriched download against the exact pre-upload artifact.

    Matching is by the unique supplied tuple
    ``(ruleId, artifactLocation.uri, primaryLocationLineHash)``.  SARIF
    result-array ordering is deliberately not part of the comparison.  The
    harness never reconstructs a location from line, column, message, or
    source text.  Duplicate tuples and missing/extra tuples are explicit
    failures, so the worker has to retain the exact artifact from that run.
    """

    uploaded_results = extract_sarif_results(
        uploaded, require_alert_numbers=False, label=artifact_label
    )
    downloaded_results = extract_sarif_results(downloaded, label="downloaded SARIF")

    def keyed(
        results: List[Dict[str, Any]], label: str
    ) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        indexed: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        duplicates: List[Tuple[str, str, str]] = []
        missing: List[str] = []
        for index, result in enumerate(results):
            prefix = f"{label}.result[{index}]"
            rule_id = result.get("rule_id")
            artifact_uri = result.get("artifact_uri")
            fingerprint = result.get("fingerprint")
            if not isinstance(rule_id, str) or not rule_id:
                missing.append(f"{prefix}.ruleId")
            if not isinstance(artifact_uri, str) or not artifact_uri:
                missing.append(f"{prefix}.locations[0].physicalLocation.artifactLocation.uri")
            if not isinstance(fingerprint, str) or not fingerprint:
                missing.append(
                    f"{prefix}.partialFingerprints.primaryLocationLineHash"
                )
            if (
                isinstance(rule_id, str)
                and rule_id
                and isinstance(artifact_uri, str)
                and artifact_uri
                and isinstance(fingerprint, str)
                and fingerprint
            ):
                key = (rule_id, artifact_uri, fingerprint)
                if key in indexed:
                    duplicates.append(key)
                else:
                    indexed[key] = result
        missing_fields(missing)
        if duplicates:
            rendered = ", ".join(repr(key) for key in duplicates)
            raise LifecycleError(
                f"{label} contains duplicate supplied tuple(s): {rendered}"
            )
        return indexed

    uploaded_by_tuple = keyed(uploaded_results, artifact_label)
    downloaded_by_tuple = keyed(downloaded_results, "downloaded SARIF")
    missing_tuples = sorted(
        set(uploaded_by_tuple) - set(downloaded_by_tuple), key=repr
    )
    extra_tuples = sorted(
        set(downloaded_by_tuple) - set(uploaded_by_tuple), key=repr
    )
    if missing_tuples or extra_tuples:
        raise LifecycleError(
            "SARIF artifact mismatch: supplied tuple sets differ; "
            f"missing from downloaded={missing_tuples!r}, "
            f"extra in downloaded={extra_tuples!r}"
        )
    return downloaded_results, artifact_label


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise LifecycleError(f"cannot read uploaded SARIF artifact {path}: {exc}") from exc
    return digest.hexdigest()


def validate_worker_metadata(
    metadata: Any,
    repo: str,
    ref: str,
    tool: str,
    category: str,
    commit_sha: str,
    run_id: Optional[str] = None,
    run_attempt: Optional[int] = None,
) -> Dict[str, Any]:
    """Validate the fixed CI artifact metadata contract.

    The workflow currently writes snake_case keys.  We intentionally do not
    silently translate guessed aliases such as ``commitSHA`` or ``attempt``:
    changing the worker schema must be visible to the lifecycle verifier.
    """

    if not isinstance(metadata, dict):
        raise LifecycleError("worker metadata must be a JSON object")
    required = (
        "schema_version",
        "artifact_name",
        "run_id",
        "run_attempt",
        "ref",
        "commit_sha",
        "tool",
        "category",
        "sarif_id",
        "report_complete",
        "clippy_step_outcome",
        "upload_step_outcome",
        "configured_floor",
        "files",
        "versions",
    )
    missing_fields(
        f"worker_metadata.{field}"
        for field in required
        if field not in metadata
    )
    if not isinstance(metadata.get("files"), dict):
        raise LifecycleError("worker_metadata.files must be an object")
    if not isinstance(metadata.get("versions"), dict):
        raise LifecycleError("worker_metadata.versions must be an object")

    actual_run_id = str(metadata["run_id"])
    actual_attempt = str(metadata["run_attempt"])
    as_positive_int(actual_run_id, "worker_metadata.run_id")
    as_positive_int(actual_attempt, "worker_metadata.run_attempt")
    expected_artifact = f"clippy-sarif-{actual_run_id}-{actual_attempt}"
    if metadata["artifact_name"] != expected_artifact:
        raise LifecycleError(
            "worker metadata artifact_name does not match run_id/run_attempt: "
            f"{metadata['artifact_name']!r} != {expected_artifact!r}"
        )
    expected = {
        "ref": ref,
        "commit_sha": commit_sha,
        "tool": tool,
        "category": category,
    }
    for field, wanted in expected.items():
        if metadata.get(field) != wanted:
            raise LifecycleError(
                f"worker metadata scope mismatch for {field}: "
                f"{metadata.get(field)!r} != {wanted!r}"
            )
    if run_id is not None and actual_run_id != str(run_id):
        raise LifecycleError(
            f"worker metadata run_id {actual_run_id!r} != requested {str(run_id)!r}"
        )
    if run_attempt is not None and actual_attempt != str(run_attempt):
        raise LifecycleError(
            f"worker metadata run_attempt {actual_attempt!r} != requested {run_attempt!r}"
        )

    return {
        "schema_version": metadata["schema_version"],
        "artifact_name": metadata["artifact_name"],
        "run_id": actual_run_id,
        "run_attempt": actual_attempt,
        "ref": metadata["ref"],
        "commit_sha": metadata["commit_sha"],
        "tool": metadata["tool"],
        "category": metadata["category"],
        "sarif_id": (
            str(metadata["sarif_id"])
            if metadata["sarif_id"] not in (None, "")
            else None
        ),
        "report_complete": metadata["report_complete"],
        "clippy_step_outcome": metadata["clippy_step_outcome"],
        "upload_step_outcome": metadata["upload_step_outcome"],
        "configured_floor": metadata["configured_floor"],
        "files": copy.deepcopy(metadata["files"]),
        "versions": copy.deepcopy(metadata["versions"]),
        "raw": copy.deepcopy(metadata),
    }


def alert_path(alert: Dict[str, Any]) -> Optional[str]:
    instance = alert.get("most_recent_instance")
    if not isinstance(instance, dict):
        instance = {}
    location = instance.get("location")
    if not isinstance(location, dict):
        location = {}
    return location.get("path") or location.get("uri") or alert.get("path")


def alert_rule_id(alert: Dict[str, Any]) -> Optional[str]:
    rule = alert.get("rule")
    if isinstance(rule, dict):
        return rule.get("id") or rule.get("name")
    return alert.get("rule_id")


def normalize_alert(
    alert: Any,
    queried_state: str,
    ref: str,
    tool: str,
    category: str,
) -> Dict[str, Any]:
    if not isinstance(alert, dict):
        raise LifecycleError("code-scanning alert page contained a non-object")
    number = alert.get("number")
    state = alert.get("state")
    missing = []
    if number in (None, ""):
        missing.append("alerts[].number")
    if state in (None, ""):
        missing.append("alerts[].state")
    missing_fields(missing)
    number = as_positive_int(number, "alerts[].number")
    instance = alert.get("most_recent_instance")
    if not isinstance(instance, dict):
        instance = {}
    return {
        "alert_number": number,
        "state": state,
        "rule_id": alert_rule_id(alert),
        "path": alert_path(alert),
        "fingerprint": None,
        "dismissed_reason": alert.get("dismissed_reason"),
        "dismissed_comment": alert.get("dismissed_comment"),
        # GitHub can retain state=dismissed after the finding was later fixed.
        # Keep a boolean derived from fixed_at so comparisons never interpret
        # the state string alone as the resolution.
        "fixed_at_present": alert.get("fixed_at") not in (None, ""),
        "present_in_sarif": False,
        "scope": {
            "repository": alert.get("repository", {}).get("full_name")
            if isinstance(alert.get("repository"), dict)
            else None,
            "ref": ref,
            "tool": tool,
            "category": category,
            "query_state": queried_state,
            "instance_ref": instance.get("ref"),
            "instance_commit_sha": instance.get("commit_sha"),
            "instance_category": instance.get("category"),
        },
        "evidence": {"api": copy.deepcopy(alert)},
    }


def collect_alerts(
    client: Any,
    repo: str,
    ref: str,
    tool: str,
    category: str,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for state in ALERT_STATES:
        pages = client.pages(
            repo_endpoint(repo, "code-scanning/alerts"),
            {"state": state, "tool_name": tool, "ref": ref, "per_page": 100},
        )
        for alert in page_items(pages, key=None):
            records.append(normalize_alert(alert, state, ref, tool, category))

    counts = Counter(record["alert_number"] for record in records)
    for number, occurrence in counts.items():
        if occurrence > 1:
            for index, record in enumerate(
                item for item in records if item["alert_number"] == number
            ):
                record["evidence"]["duplicate_api_occurrence"] = index + 1
    return records


def snapshot_record_key(record: Dict[str, Any], occurrence: int = 1) -> str:
    return f"alert:{record.get('alert_number')}:{occurrence}"


def previous_records(snapshot: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not snapshot:
        return []
    records = snapshot.get("alerts")
    if not isinstance(records, list):
        raise LifecycleError("snapshot.alerts must be a list")
    return [record for record in records if isinstance(record, dict)]


def merge_alert_evidence(
    api_records: List[Dict[str, Any]],
    sarif_results: List[Dict[str, Any]],
    previous: Optional[Dict[str, Any]],
    ref: str,
    tool: str,
    category: str,
    sarif_observed: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Associate SARIF results with alerts while retaining every record."""

    by_number: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for record in api_records:
        by_number[record["alert_number"]].append(record)

    by_fingerprint: Dict[str, List[int]] = defaultdict(list)
    association_errors: List[str] = []
    for index, result in enumerate(sarif_results):
        number = result.get("alert_number")
        matches = by_number.get(number, [])
        if not matches:
            association_errors.append(
                f"sarif result {index} alert number {number} is absent from the "
                "paginated alert API response"
            )
            continue
        if len(matches) != 1:
            association_errors.append(
                f"sarif result {index} alert number {number} has "
                f"{len(matches)} alert API records"
            )
            continue
        record = matches[0]
        if record.get("fingerprint") not in (None, result["fingerprint"]):
            association_errors.append(
                f"alert number {number} is associated with two fingerprints"
            )
        record["fingerprint"] = result["fingerprint"]
        if not record.get("rule_id"):
            record["rule_id"] = result.get("rule_id")
        if not record.get("path"):
            record["path"] = result.get("artifact_uri")
        record["present_in_sarif"] = True
        record.setdefault("evidence", {}).setdefault("sarif", []).append(
            {
                "run_index": result.get("run_index"),
                "result_index": result.get("result_index"),
                "artifact_uri": result.get("artifact_uri"),
                "region": copy.deepcopy(result.get("region")),
                "message": copy.deepcopy(result.get("message")),
                "level": result.get("level"),
            }
        )
        by_fingerprint[result["fingerprint"]].append(number)

    for fingerprint, numbers in by_fingerprint.items():
        if len(numbers) != len(set(numbers)):
            association_errors.append(
                "duplicate fingerprint collision: fingerprint "
                f"{fingerprint!r} maps to alert number {numbers[0]} more than once"
            )

    prior_by_number = unique_records(previous) if previous else {}
    current_numbers = {record["alert_number"] for record in api_records}
    missing_current_states = sorted(set(prior_by_number) - current_numbers)
    if missing_current_states:
        raise LifecycleError(
            "current alert state missing for baseline alert(s): "
            f"{missing_current_states!r}; refusing to capture an incomplete "
            "paginated open/dismissed/fixed response"
        )

    # For a negative/skipped run there is no new SARIF document.  Retain the
    # previous SARIF association so the comparison measures alert state, not
    # the absence of an upload artifact.
    if not sarif_observed:
        for record in api_records:
            old = prior_by_number.get(record["alert_number"])
            if old:
                for field in ("fingerprint", "rule_id", "path"):
                    if record.get(field) in (None, ""):
                        record[field] = old.get(field)
                record["present_in_sarif"] = old.get("present_in_sarif")
                record.setdefault("evidence", {})["sarif_observed"] = False

    # Same alert number is never silently selected.  The duplicate records stay
    # in the output and make the snapshot fail loudly before comparison.
    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for record in api_records:
        grouped[record["alert_number"]].append(record)
    duplicate_numbers = {
        number: len(records) for number, records in grouped.items() if len(records) > 1
    }
    if duplicate_numbers:
        association_errors.append(
            "duplicate alert records: "
            + ", ".join(
                f"{number} ({count})" for number, count in sorted(duplicate_numbers.items())
            )
        )
    if association_errors:
        raise LifecycleError("; ".join(association_errors))

    for record in api_records:
        record["scope"].update(
            {"ref": ref, "tool": tool, "category": category}
        )
    api_records.sort(key=lambda record: (record["alert_number"], record.get("state", "")))
    for number, records in grouped.items():
        for occurrence, record in enumerate(
            (item for item in api_records if item["alert_number"] == number), 1
        ):
            record["record_key"] = snapshot_record_key(record, occurrence)

    collisions = {
        fingerprint: numbers
        for fingerprint, numbers in by_fingerprint.items()
        if len(set(numbers)) > 1
    }
    return api_records, {
        "fingerprint_alert_numbers": {
            key: list(values) for key, values in sorted(by_fingerprint.items())
        },
        "duplicate_fingerprint_groups": collisions,
        "sarif_result_count": len(sarif_results),
    }


def repo_from_snapshot(snapshot: Optional[Dict[str, Any]]) -> Optional[str]:
    if not snapshot:
        return None
    scope = snapshot.get("scope")
    return scope.get("repository") if isinstance(scope, dict) else None


def make_snapshot(
    repo: str,
    ref: str,
    tool: str,
    category: str,
    commit_sha: str,
    analysis: Dict[str, Any],
    sarif_results: List[Dict[str, Any]],
    alerts: List[Dict[str, Any]],
    association: Dict[str, Any],
    run: Optional[Dict[str, Any]] = None,
    sarif_status: Optional[Dict[str, Any]] = None,
    previous: Optional[Dict[str, Any]] = None,
    uploaded_artifact: Optional[Dict[str, Any]] = None,
    worker_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "captured_at": utc_now(),
        "scope": {
            "repository": repo,
            "ref": ref,
            "tool": tool,
            "category": category,
            "commit_sha": commit_sha,
        },
        "analysis": analysis,
        "run": run,
        "worker_metadata": worker_metadata,
        "sarif": {
            "downloaded": sarif_status is not None,
            "accept": SARIF_ACCEPT if sarif_status is not None else None,
            "processing_status": (
                sarif_status.get("processing_status") if sarif_status else "skipped"
            ),
            "status_evidence": copy.deepcopy(sarif_status),
            "results": sarif_results,
            "result_count": len(sarif_results),
        },
        "uploaded_artifact": uploaded_artifact,
        "alerts": alerts,
        "association": association,
        "baseline": {
            "snapshot": None,
            "carried_alert_count": sum(
                1
                for record in alerts
                if "carried_from" in (record.get("evidence") or {})
            ),
        },
    }


def stable_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Fields used by comparisons; timestamps and source coordinates are omitted."""

    return {
        "alert_number": record.get("alert_number"),
        "state": record.get("state"),
        "rule_id": record.get("rule_id"),
        "path": record.get("path"),
        "fingerprint": record.get("fingerprint"),
        "dismissed_reason": record.get("dismissed_reason"),
        "dismissed_comment": record.get("dismissed_comment"),
        "fixed_at_present": bool(record.get("fixed_at_present", False)),
    }


def unique_records(snapshot: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    records = snapshot.get("alerts")
    if not isinstance(records, list):
        raise LifecycleError("snapshot.alerts must be a list")
    result: Dict[int, Dict[str, Any]] = {}
    duplicates: Dict[int, int] = defaultdict(int)
    for record in records:
        if not isinstance(record, dict):
            raise LifecycleError("snapshot.alerts contains a non-object")
        number = record.get("alert_number")
        if not isinstance(number, int):
            raise LifecycleError("snapshot alert is missing integer alert_number")
        duplicates[number] += 1
        result.setdefault(number, record)
    repeated = {number: count for number, count in duplicates.items() if count > 1}
    if repeated:
        raise LifecycleError(
            "duplicate alert records cannot be compared safely: "
            + ", ".join(f"{number} ({count})" for number, count in sorted(repeated.items()))
        )
    return result


def normalized_diff(
    baseline: Dict[str, Any], current: Dict[str, Any]
) -> Dict[str, Any]:
    before = unique_records(baseline)
    after = unique_records(current)
    changed = []
    for number in sorted(set(before) & set(after)):
        old = stable_record(before[number])
        new = stable_record(after[number])
        if old != new:
            changed.append(
                {"alert_number": number, "before": old, "after": new}
            )
    return {
        "baseline_only": [stable_record(before[number]) for number in sorted(set(before) - set(after))],
        "current_only": [stable_record(after[number]) for number in sorted(set(after) - set(before))],
        "changed": changed,
    }


def selector_matches(record: Dict[str, Any], selector: Dict[str, Any]) -> bool:
    if not isinstance(selector, dict):
        raise LifecycleError("expectation selector must be an object")
    number = record.get("alert_number")
    if "alert_number" in selector and number != selector["alert_number"]:
        return False
    numbers = selector.get("alert_numbers")
    if numbers is not None and number not in numbers:
        return False
    rule_id = record.get("rule_id")
    if "rule_id" in selector and rule_id != selector["rule_id"]:
        return False
    rule_ids = selector.get("rule_ids")
    if rule_ids is not None and rule_id not in rule_ids:
        return False
    path = record.get("path")
    if "path" in selector and path != selector["path"]:
        return False
    paths = selector.get("paths")
    if paths is not None and path not in paths:
        return False
    prefix = selector.get("path_prefix")
    if prefix is not None and (not isinstance(path, str) or not path.startswith(prefix)):
        return False
    states = selector.get("states")
    if states is not None and record.get("state") not in states:
        return False
    fingerprint = record.get("fingerprint")
    fingerprints = selector.get("fingerprints")
    if fingerprints is not None and fingerprint not in fingerprints:
        return False
    return True


def resolve_baseline_expectation(
    baseline_records: Dict[int, Dict[str, Any]], entry: Any
) -> List[int]:
    if isinstance(entry, int):
        if entry not in baseline_records:
            raise LifecycleError(f"expectation refers to absent baseline alert {entry}")
        return [entry]
    if not isinstance(entry, dict):
        raise LifecycleError("affected expectation must be an alert number or object")
    if "alert_number" in entry:
        return resolve_baseline_expectation(baseline_records, entry["alert_number"])
    selector = entry.get("selector", entry)
    matches = [
        number
        for number, record in baseline_records.items()
        if selector_matches(record, selector)
    ]
    if not matches:
        raise LifecycleError(f"expectation selector matched no baseline alert: {selector!r}")
    return sorted(matches)


def assert_fields(
    number: int,
    before: Dict[str, Any],
    after: Optional[Dict[str, Any]],
    expected: Dict[str, Any],
) -> List[str]:
    if after is None:
        return [f"affected alert {number} is absent from current snapshot"]
    failures = []
    for field, wanted in expected.items():
        old_value = stable_record(before).get(field)
        new_value = stable_record(after).get(field)
        if wanted == "same":
            okay = old_value == new_value
        elif wanted == "changed":
            okay = old_value != new_value
        else:
            okay = new_value == wanted
        if not okay:
            failures.append(
                f"alert {number} field {field}: expected {wanted!r}, "
                f"got {new_value!r} (baseline {old_value!r})"
            )
    return failures


def fingerprint_base(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    head, separator, tail = value.rpartition(":")
    return head if separator and tail.isdigit() else value


def measure_rekeys(
    baseline: Dict[int, Dict[str, Any]], current: Dict[int, Dict[str, Any]], diff: Dict[str, Any]
) -> Dict[str, Any]:
    rekeys = []
    potential_wrong_dismissals = []
    for change in diff["changed"]:
        old = change["before"]
        new = change["after"]
        if fingerprint_base(old.get("fingerprint")) == fingerprint_base(new.get("fingerprint")):
            rekeys.append(
                {
                    "kind": "fingerprint_ordinal_or_rekey",
                    "alert_number": change["alert_number"],
                    "before": old,
                    "after": new,
                }
            )
    for record in diff["current_only"]:
        candidates = [
            old
            for old in baseline.values()
            if old.get("rule_id") == record.get("rule_id")
            and old.get("path") == record.get("path")
            and fingerprint_base(old.get("fingerprint"))
            == fingerprint_base(record.get("fingerprint"))
        ]
        if any(old.get("state") == "dismissed" for old in candidates) and record.get("state") == "open":
            potential_wrong_dismissals.append(
                {
                    "kind": "possible_dismissal_rekey",
                    "new_alert": record,
                    "dismissed_baseline_candidates": [stable_record(old) for old in candidates],
                }
            )
    for record in diff["current_only"]:
        candidates = [
            old
            for old in baseline.values()
            if old.get("rule_id") == record.get("rule_id")
            and old.get("path") == record.get("path")
            and fingerprint_base(old.get("fingerprint"))
            == fingerprint_base(record.get("fingerprint"))
        ]
        if candidates:
            rekeys.append(
                {
                    "kind": "new_alert_same_base_fingerprint",
                    "new_alert_number": record.get("alert_number"),
                    "before_candidates": [stable_record(old) for old in candidates],
                    "after": record,
                }
            )
    return {
        "rekeys": rekeys,
        "potential_wrong_dismissals": potential_wrong_dismissals,
        "added_count": len(diff["current_only"]),
    }


def active_presence(record: Optional[Dict[str, Any]]) -> bool:
    """Whether an alert is currently active rather than historical.

    GitHub may expose a record as ``state=dismissed`` after it has later been
    fixed.  ``fixed_at_present`` therefore participates in active presence;
    the state string alone is not sufficient.
    """

    return bool(
        record
        and record.get("state") in {"open", "dismissed"}
        and not bool(record.get("fixed_at_present", False))
    )


def dismissal_state(record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the state fields that constitute the dismissal oracle."""

    if record is None:
        return {
            "state": None,
            "dismissed_reason": None,
            "dismissed_comment": None,
        }
    return {
        "state": record.get("state"),
        "dismissed_reason": record.get("dismissed_reason"),
        "dismissed_comment": record.get("dismissed_comment"),
    }


def record_oracle_comments(record: Dict[str, Any]) -> List[str]:
    """Collect explicit occurrence comments available in snapshot evidence."""

    comments: List[str] = []

    def add_message(value: Any) -> None:
        if isinstance(value, dict):
            value = value.get("text")
        if isinstance(value, str) and value not in comments:
            comments.append(value)

    evidence = record.get("evidence")
    if isinstance(evidence, dict):
        sarif_evidence = evidence.get("sarif")
        if isinstance(sarif_evidence, list):
            for item in sarif_evidence:
                if isinstance(item, dict):
                    add_message(item.get("message"))
        api_alert = evidence.get("api")
        if isinstance(api_alert, dict):
            add_message(api_alert.get("dismissed_comment"))
            instance = api_alert.get("most_recent_instance")
            if isinstance(instance, dict):
                add_message(instance.get("message"))
    add_message(record.get("dismissed_comment"))
    return comments


def positive_number_list(value: Any, field: str) -> List[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifecycleError(f"{field} must be a list")
    numbers = [as_positive_int(item, f"{field}[]") for item in value]
    duplicates = sorted(number for number, count in Counter(numbers).items() if count > 1)
    if duplicates:
        raise LifecycleError(f"{field} contains duplicate alert numbers: {duplicates!r}")
    return numbers


def validate_survivor_map(value: Any) -> Dict[str, Any]:
    """Validate the driver-supplied T5 source-occurrence mapping.

    The mapping is intentionally an input to the comparison, not an inferred
    pairing.  Each source occurrence must name both observed alert numbers.
    ``source_occurrence`` carries stable evidence (and may carry a unique
    comment) used to detect two occurrences being swapped while the set of
    alert numbers remains unchanged.
    """

    if not isinstance(value, dict):
        raise LifecycleError("survivor map must be a JSON object")
    schema = value.get("schema")
    if schema not in (None, "rustytransfer-alert-lifecycle/survivor-map/v1"):
        raise LifecycleError(f"unsupported survivor map schema: {schema!r}")
    raw_survivors = value.get("survivors")
    if not isinstance(raw_survivors, list):
        raise LifecycleError("survivor_map.survivors must be a list")
    if not raw_survivors:
        raise LifecycleError("survivor_map.survivors must not be empty")

    survivors: List[Dict[str, Any]] = []
    before_numbers: List[int] = []
    after_numbers: List[int] = []
    for index, entry in enumerate(raw_survivors):
        prefix = f"survivor_map.survivors[{index}]"
        if not isinstance(entry, dict):
            raise LifecycleError(f"{prefix} must be an object")
        if "before_alert_number" not in entry:
            raise LifecycleError(f"{prefix}.before_alert_number is required")
        if "after_alert_number" not in entry:
            raise LifecycleError(f"{prefix}.after_alert_number is required")
        source_occurrence = entry.get("source_occurrence")
        if not isinstance(source_occurrence, dict):
            raise LifecycleError(f"{prefix}.source_occurrence must be an object")
        if not any(
            field in source_occurrence
            for field in ("primary_fingerprint", "comment")
        ):
            raise LifecycleError(
                f"{prefix}.source_occurrence needs a stable discriminator: "
                "primary_fingerprint or comment"
            )
        before_number = as_positive_int(
            entry["before_alert_number"], f"{prefix}.before_alert_number"
        )
        after_number = as_positive_int(
            entry["after_alert_number"], f"{prefix}.after_alert_number"
        )
        before_numbers.append(before_number)
        after_numbers.append(after_number)
        survivors.append(
            {
                "before_alert_number": before_number,
                "after_alert_number": after_number,
                "source_occurrence": copy.deepcopy(source_occurrence),
                "comment": entry.get("comment"),
            }
        )

    duplicate_before = sorted(
        number for number, count in Counter(before_numbers).items() if count > 1
    )
    duplicate_after = sorted(
        number for number, count in Counter(after_numbers).items() if count > 1
    )
    if duplicate_before:
        raise LifecycleError(
            "survivor_map has duplicate before alert numbers: "
            f"{duplicate_before!r}"
        )
    if duplicate_after:
        raise LifecycleError(
            "survivor_map has duplicate after alert numbers: "
            f"{duplicate_after!r}"
        )

    expected_removed = positive_number_list(
        value.get("expected_removed", []), "survivor_map.expected_removed"
    )
    expected_added = positive_number_list(
        value.get("expected_added", []), "survivor_map.expected_added"
    )
    return {
        "schema": schema or "rustytransfer-alert-lifecycle/survivor-map/v1",
        "survivors": survivors,
        "expected_removed": expected_removed,
        "expected_added": expected_added,
        "raw": copy.deepcopy(value),
    }


def compare_survivor_map(
    baseline: Dict[str, Any],
    current: Dict[str, Any],
    expectation: Dict[str, Any],
    survivor_map: Any,
) -> Dict[str, Any]:
    """Compare T5 using only the driver's explicit occurrence mapping.

    A survivor passes when its mapped alert number is present and unchanged,
    its active presence is unchanged, and its dismissal state is unchanged.
    Fingerprint changes are evidence, not a lifecycle failure.  Fixture
    removals and additions are declared separately and are not mistaken for
    survivor failures.
    """

    spec = validate_survivor_map(survivor_map)
    before = unique_records(baseline)
    after = unique_records(current)
    diff = normalized_diff(baseline, current)
    failures: List[str] = []
    survivor_results: List[Dict[str, Any]] = []
    fingerprint_changes: List[Dict[str, Any]] = []

    survivors = spec["survivors"]
    survivor_before = {entry["before_alert_number"] for entry in survivors}
    survivor_after = {entry["after_alert_number"] for entry in survivors}
    expected_removed = set(spec["expected_removed"])
    expected_added = set(spec["expected_added"])

    if survivor_before & expected_removed:
        failures.append(
            "survivor_map overlaps survivors and expected_removed: "
            f"{sorted(survivor_before & expected_removed)!r}"
        )
    if survivor_after & expected_added:
        failures.append(
            "survivor_map overlaps survivors and expected_added: "
            f"{sorted(survivor_after & expected_added)!r}"
        )
    unknown_removed = sorted(expected_removed - set(before))
    if unknown_removed:
        failures.append(
            f"expected_removed refers to absent baseline alert(s): {unknown_removed!r}"
        )
    old_added = sorted(expected_added & set(before))
    if old_added:
        failures.append(
            f"expected_added must be new current alerts, but is in baseline: {old_added!r}"
        )
    missing_baseline = sorted(set(before) - survivor_before - expected_removed)
    if missing_baseline:
        failures.append(
            "survivor_map does not classify baseline alert(s) as survivor or "
            f"expected_removed: {missing_baseline!r}"
        )

    permitted_current = survivor_after | expected_added | (expected_removed & set(after))
    unclassified_current = sorted(set(after) - permitted_current)
    if unclassified_current:
        failures.append(
            "survivor_map does not classify current alert(s) as survivor, "
            f"expected_added, or removed fixture: {unclassified_current!r}"
        )
    missing_added = sorted(expected_added - set(after))
    if missing_added:
        failures.append(
            f"expected_added is absent from current snapshot: {missing_added!r}"
        )

    def source_mismatch(
        oracle: Dict[str, Any],
        record: Dict[str, Any],
        check_fingerprint: bool,
        same_alert_number: bool = False,
    ) -> List[str]:
        mismatches: List[str] = []
        if "rule_id" in oracle and record.get("rule_id") != oracle["rule_id"]:
            mismatches.append(
                f"rule_id expected {oracle['rule_id']!r}, got {record.get('rule_id')!r}"
            )
        if "artifact_uri" in oracle and record.get("path") != oracle["artifact_uri"]:
            mismatches.append(
                f"artifact_uri expected {oracle['artifact_uri']!r}, "
                f"got {record.get('path')!r}"
            )
        if "comment" in oracle:
            comment = oracle["comment"]
            if not isinstance(comment, str) or comment not in record_oracle_comments(record):
                mismatches.append(
                    f"comment oracle {comment!r} is not present in current alert evidence"
                )
        fingerprint_field = (
            "primary_fingerprint"
            if check_fingerprint
            else "after_primary_fingerprint"
        )
        if not check_fingerprint and fingerprint_field not in oracle:
            # If the ordinal changed, the baseline fingerprint is expected to
            # change too.  In that case the optional after-value or a stable
            # comment is the evidence that the mapped alert is the same source
            # occurrence.  For an unchanged alert number, the baseline
            # fingerprint remains an exact current oracle.
            fingerprint_field = (
                "primary_fingerprint"
                if same_alert_number
                else ""
            )
        if fingerprint_field and fingerprint_field in oracle:
            if record.get("fingerprint") != oracle[fingerprint_field]:
                mismatches.append(
                    f"{fingerprint_field} expected "
                    f"{oracle[fingerprint_field]!r}, got "
                    f"{record.get('fingerprint')!r}"
                )
        return mismatches

    for entry in survivors:
        before_number = entry["before_alert_number"]
        after_number = entry["after_alert_number"]
        old = before.get(before_number)
        new = after.get(after_number)
        outcome: Dict[str, Any] = {
            "before_alert_number": before_number,
            "after_alert_number": after_number,
            "actual_alert_number": new.get("alert_number") if new else None,
            "alert_number_preserved": after_number == before_number and new is not None,
            "active_presence": {
                "before": active_presence(old),
                "after": active_presence(new),
            },
            "dismissal_state": {
                "before": dismissal_state(old),
                "after": dismissal_state(new),
            },
            "source_occurrence": copy.deepcopy(entry["source_occurrence"]),
        }
        outcome["active_presence"]["preserved"] = (
            outcome["active_presence"]["before"]
            == outcome["active_presence"]["after"]
        )
        outcome["dismissal_state"]["preserved"] = (
            outcome["dismissal_state"]["before"]
            == outcome["dismissal_state"]["after"]
        )
        if old is None:
            failures.append(f"survivor baseline alert {before_number} is absent")
            survivor_results.append(outcome)
            continue
        baseline_source_mismatches = source_mismatch(
            entry["source_occurrence"], old, check_fingerprint=True
        )
        if baseline_source_mismatches:
            failures.extend(
                f"survivor {before_number} baseline source occurrence mismatch: {mismatch}"
                for mismatch in baseline_source_mismatches
            )
        if new is None:
            failures.append(
                f"survivor {before_number} lost: mapped current alert "
                f"{after_number} is absent"
            )
            survivor_results.append(outcome)
            continue
        if (
            after_number != before_number
            and "after_primary_fingerprint" not in entry["source_occurrence"]
            and "comment" not in entry["source_occurrence"]
        ):
            failures.append(
                f"survivor {before_number} changed alert number without an "
                "after_primary_fingerprint or comment oracle"
            )
        if after_number != before_number:
            failures.append(
                f"survivor {before_number} did not keep its alert number: "
                f"mapped to {after_number}"
            )
        if not outcome["active_presence"]["preserved"]:
            failures.append(
                f"survivor {before_number} changed active presence: "
                f"{outcome['active_presence']['before']!r} -> "
                f"{outcome['active_presence']['after']!r}"
            )
        if not outcome["dismissal_state"]["preserved"]:
            failures.append(
                f"survivor {before_number} changed dismissal state: "
                f"{outcome['dismissal_state']['before']!r} -> "
                f"{outcome['dismissal_state']['after']!r}"
            )
        failures.extend(
            f"survivor {before_number} current source occurrence mismatch: {mismatch}"
            for mismatch in source_mismatch(
                entry["source_occurrence"],
                new,
                check_fingerprint=False,
                same_alert_number=after_number == before_number,
            )
        )
        if old.get("fingerprint") != new.get("fingerprint"):
            fingerprint_changes.append(
                {
                    "before_alert_number": before_number,
                    "after_alert_number": after_number,
                    "before_fingerprint": old.get("fingerprint"),
                    "after_fingerprint": new.get("fingerprint"),
                    "reason": "mapped survivor fingerprint changed",
                }
            )
        expected_fingerprint = entry["source_occurrence"].get("primary_fingerprint")
        if (
            expected_fingerprint is not None
            and expected_fingerprint != new.get("fingerprint")
        ):
            fingerprint_changes.append(
                {
                    "before_alert_number": before_number,
                    "after_alert_number": after_number,
                    "expected_fingerprint": expected_fingerprint,
                    "actual_fingerprint": new.get("fingerprint"),
                    "reason": "source occurrence oracle fingerprint changed",
                }
            )
        survivor_results.append(outcome)

    removed_results: List[Dict[str, Any]] = []
    for number in sorted(expected_removed):
        old = before.get(number)
        new = after.get(number)
        active = active_presence(new)
        if active:
            failures.append(
                f"expected removed alert {number} remains active in current snapshot"
            )
        removed_results.append(
            {
                "alert_number": number,
                "before": stable_record(old) if old else None,
                "after": stable_record(new) if new else None,
                "active_after": active,
            }
        )

    added_results = [
        {"alert_number": number, "after": stable_record(after[number])}
        for number in sorted(expected_added)
        if number in after
    ]
    return {
        "trial": expectation.get("trial"),
        "mode": "survivor-map",
        "status": "PASS" if not failures else "FAIL",
        "diff": diff,
        "failures": failures,
        "survivors": survivor_results,
        "expected_removed": removed_results,
        "expected_added": added_results,
        "fingerprint_changes": fingerprint_changes,
        "survivor_map": spec["raw"],
    }


def compare_with_expectation(
    baseline: Dict[str, Any],
    current: Dict[str, Any],
    expectation: Dict[str, Any],
) -> Dict[str, Any]:
    base_scope = baseline.get("scope") or {}
    current_scope = current.get("scope") or {}
    for field in ("repository", "ref", "tool", "category"):
        if base_scope.get(field) != current_scope.get(field):
            raise LifecycleError(
                f"scope mismatch for {field}: baseline={base_scope.get(field)!r}, "
                f"current={current_scope.get(field)!r}"
            )
    if baseline.get("analysis", {}).get("id") == current.get("analysis", {}).get("id"):
        raise LifecycleError("comparison uses the same analysis id twice")

    mode = expectation.get("mode", "assert")
    if mode == "survivor-map":
        if "survivor_map" not in expectation:
            raise LifecycleError(
                "survivor-map expectation requires a survivor_map object"
            )
        return compare_survivor_map(
            baseline, current, expectation, expectation["survivor_map"]
        )
    if mode == "stateless-limitation":
        raise LifecycleError(
            "stateless-limitation is deprecated; provide an observed "
            "survivor-map expectation instead"
        )

    before = unique_records(baseline)
    after = unique_records(current)
    diff = normalized_diff(baseline, current)
    affected = set()
    expectation_entries = expectation.get("affected", [])
    if not isinstance(expectation_entries, list):
        raise LifecycleError("expectation.affected must be a list")
    failures = []
    for entry in expectation_entries:
        numbers = resolve_baseline_expectation(before, entry)
        affected.update(numbers)
        expected_fields = entry.get("expect", {}) if isinstance(entry, dict) else {}
        if not isinstance(expected_fields, dict):
            raise LifecycleError("expectation entry expect must be an object")
        for number in numbers:
            if (
                isinstance(entry, dict)
                and entry.get("allow_absent") is True
                and number not in after
            ):
                continue
            failures.extend(assert_fields(number, before[number], after.get(number), expected_fields))

    restore = expectation.get("restore")
    allowed_historical: set = set()
    if restore is not None:
        if not isinstance(restore, dict):
            raise LifecycleError("expectation.restore must be an object")
        raw_historical = restore.get("allowed_fixed_history", [])
        if not isinstance(raw_historical, list):
            raise LifecycleError("restore.allowed_fixed_history must be a list")
        for number in raw_historical:
            number = as_positive_int(number, "restore.allowed_fixed_history[]")
            if number in before:
                failures.append(
                    f"restore historical alert {number} is already in the baseline; "
                    "historical exceptions must be newly created"
                )
            allowed_historical.add(number)
        if restore.get("active_baseline", "unchanged") != "unchanged":
            raise LifecycleError("restore.active_baseline currently supports only 'unchanged'")
        # This oracle applies to every baseline record, including dismissed and
        # fixed records. Only explicitly listed newly created fixed alerts are
        # admitted below.
        for number, old in before.items():
            new = after.get(number)
            if new is None:
                failures.append(f"restore lost baseline alert {number}")
            elif stable_record(old) != stable_record(new):
                failures.append(
                    f"restore changed baseline alert {number}: "
                    f"{stable_record(old)!r} -> {stable_record(new)!r}"
                )

    allowed_removed = set()
    for entry in expectation.get("allow_removed", []):
        allowed_removed.update(resolve_baseline_expectation(before, entry))
    allowed_added = set()
    for entry in expectation.get("allow_added", []):
        selector = entry.get("selector", entry) if isinstance(entry, dict) else {}
        matches = [number for number, record in after.items() if selector_matches(record, selector)]
        if not matches:
            failures.append(f"allow_added selector matched no current alert: {selector!r}")
        allowed_added.update(matches)
    if restore is not None:
        for number in sorted(allowed_historical):
            current_record = after.get(number)
            if current_record is None:
                failures.append(
                    f"restore allowed historical alert {number} is absent from current snapshot"
                )
            elif not current_record.get("fixed_at_present", False):
                failures.append(
                    f"restore historical alert {number} has no fixed_at evidence: "
                    f"state={current_record.get('state')!r}"
                )
            allowed_added.add(number)

    for rekey in expectation.get("allow_rekeys", []):
        if not isinstance(rekey, dict) or "old_alert_number" not in rekey:
            raise LifecycleError("allow_rekeys entries require old_alert_number")
        old_number = rekey["old_alert_number"]
        if old_number not in before:
            failures.append(f"allow_rekeys refers to absent baseline alert {old_number}")
            continue
        affected.add(old_number)
        selector = rekey.get("new_selector", {})
        matches = [number for number, record in after.items() if selector_matches(record, selector)]
        if len(matches) != 1:
            failures.append(
                f"rekey for old alert {old_number} expected one new alert, found {len(matches)}"
            )
        allowed_added.update(matches)

    allowed_changes = affected | allowed_removed
    for change in diff["changed"]:
        if change["alert_number"] not in allowed_changes:
            failures.append(f"unexpected changed alert {change['alert_number']}")
    for record in diff["baseline_only"]:
        if record["alert_number"] not in allowed_removed and record["alert_number"] not in affected:
            failures.append(f"unexpected removed alert {record['alert_number']}")
    for record in diff["current_only"]:
        if record["alert_number"] not in allowed_added:
            failures.append(f"unexpected added alert {record['alert_number']}")

    measurement = None
    if mode == "measure":
        measurement = measure_rekeys(before, after, diff)
    result: Dict[str, Any] = {
        "trial": expectation.get("trial"),
        "mode": mode,
        "status": "PASS",
        "diff": diff,
        "failures": failures,
    }
    if measurement is not None:
        result["measurement"] = measurement
    if failures:
        raise LifecycleError(
            "comparison failed:\n" + "\n".join(f"- {failure}" for failure in failures)
        )
    return result


def verify_upload_skipped(
    client: Any, repo: str, run_id: str, job_name: str, step_name: str
) -> Dict[str, Any]:
    pages = client.pages(
        repo_endpoint(repo, f"actions/runs/{run_id}/jobs"),
        {"per_page": 100},
    )
    matches = []
    for job in page_items(pages, key="jobs"):
        if not isinstance(job, dict):
            continue
        if job_name and job.get("name") != job_name:
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("name") == step_name:
                matches.append({"job": job, "step": step})
    if len(matches) != 1:
        raise LifecycleError(
            f"expected exactly one {step_name!r} step in job {job_name!r}; "
            f"found {len(matches)}"
        )
    step = matches[0]["step"]
    if step.get("status") != "completed" or step.get("conclusion") != "skipped":
        raise LifecycleError(
            f"Clippy SARIF upload step was not skipped: "
            f"status={step.get('status')!r}, conclusion={step.get('conclusion')!r}"
        )
    return {
        "job_name": matches[0]["job"].get("name"),
        "job_id": matches[0]["job"].get("id"),
        "step_name": step_name,
        "status": step.get("status"),
        "conclusion": step.get("conclusion"),
        "raw": matches[0],
    }


def comparable_analyses(
    client: Any,
    repo: str,
    ref: str,
    tool: str,
    category: str,
    commit_sha: str,
) -> List[Dict[str, Any]]:
    pages = client.pages(
        repo_endpoint(repo, "code-scanning/analyses"),
        {"ref": ref, "tool_name": tool, "per_page": 100},
    )
    return [
        analysis
        for analysis in page_items(pages, key=None)
        if analysis_matches(analysis, ref, tool, category, commit_sha)
    ]


def scope_from_args(args: argparse.Namespace) -> Tuple[str, str, str, str, str]:
    values = (args.repo, args.ref, args.tool, args.category, args.commit_sha)
    if any(value in (None, "") for value in values):
        raise LifecycleError("repo, ref, tool, category, and commit_sha are required")
    if not args.ref.startswith("refs/"):
        raise LifecycleError(f"ref must be the complete ref, such as refs/heads/main: {args.ref!r}")
    return values


def cmd_snapshot(args: argparse.Namespace) -> int:
    repo, ref, tool, category, commit_sha = scope_from_args(args)
    previous = read_json(Path(args.previous)) if args.previous else None
    worker_metadata = validate_worker_metadata(
        read_json(Path(args.metadata)),
        repo,
        ref,
        tool,
        category,
        commit_sha,
        args.run_id,
        args.run_attempt,
    )
    if worker_metadata["report_complete"] not in (True, "true"):
        raise LifecycleError(
            "positive snapshot requires worker_metadata.report_complete=true"
        )
    if worker_metadata["upload_step_outcome"] not in ("success", True):
        raise LifecycleError(
            "positive snapshot requires worker_metadata.upload_step_outcome=success"
        )
    client = GhClient(args.gh)
    effective_run_id = args.run_id or worker_metadata["run_id"]
    effective_run_attempt = args.run_attempt
    if effective_run_attempt is None:
        effective_run_attempt = int(worker_metadata["run_attempt"])
    run = None
    if effective_run_id:
        run = validate_run(
            client.get(repo_endpoint(repo, f"actions/runs/{effective_run_id}")),
            effective_run_id,
            commit_sha,
            ref,
        )
        if run.get("run_attempt") is not None and str(run.get("run_attempt")) != str(effective_run_attempt):
            raise LifecycleError(
                f"wrong run_attempt {run.get('run_attempt')!r}, expected {effective_run_attempt!r}"
            )

    effective_sarif_id = args.sarif_id or (
        None if args.analysis_id else worker_metadata.get("sarif_id")
    )
    if args.sarif_id and worker_metadata.get("sarif_id") not in (
        None,
        str(args.sarif_id),
        args.sarif_id,
    ):
        raise LifecycleError(
            f"worker metadata sarif_id {worker_metadata.get('sarif_id')!r} "
            f"!= requested {str(args.sarif_id)!r}"
        )
    if bool(args.analysis_id) == bool(effective_sarif_id):
        raise LifecycleError("provide exactly one of --analysis-id or --sarif-id")

    sarif_status = None
    if effective_sarif_id:
        sarif_status = poll_sarif(
            client, repo, effective_sarif_id, args.timeout, args.poll_interval
        )
        analysis_raw = select_analysis(
            client,
            repo,
            ref,
            tool,
            category,
            commit_sha,
            effective_sarif_id,
            sarif_status,
        )
    else:
        analysis_raw = client.get(
            repo_endpoint(repo, f"code-scanning/analyses/{args.analysis_id}")
        )
    analysis = validate_analysis(
        analysis_raw, repo, ref, tool, category, commit_sha
    )
    if effective_sarif_id:
        # Keep the upload identifier even when the analysis representation does
        # not echo sarif_id.  This is the exact asynchronous upload we polled.
        analysis["sarif_id"] = str(effective_sarif_id)
    assert_new_analysis(analysis, previous)
    if run and analysis.get("run_attempt") is None:
        analysis["run_attempt"] = run.get("run_attempt")
    sarif = client.get(
        repo_endpoint(repo, f"code-scanning/analyses/{analysis['id']}"), SARIF_ACCEPT
    )
    uploaded_artifact = None
    if args.uploaded_sarif:
        uploaded_path = Path(args.uploaded_sarif).resolve()
        uploaded = read_json(uploaded_path)
        sarif_results, _ = validate_uploaded_artifact(uploaded, sarif)
        uploaded_artifact = {
            "path": str(uploaded_path),
            "sha256": sha256_file(uploaded_path),
            "result_count": len(sarif_results),
            "validation": "run/result index + rule_id + artifact_uri + fingerprint",
        }
    else:
        if args.require_uploaded_artifact:
            raise LifecycleError(
                "missing fields: uploaded SARIF artifact (required by this baseline)"
            )
        sarif_results = extract_sarif_results(sarif)
    api_records = collect_alerts(client, repo, ref, tool, category)
    alerts, association = merge_alert_evidence(
        api_records,
        sarif_results,
        previous,
        ref,
        tool,
        category,
    )
    snapshot = make_snapshot(
        repo,
        ref,
        tool,
        category,
        commit_sha,
        analysis,
        sarif_results,
        alerts,
        association,
        run=run,
        sarif_status=sarif_status or {"processing_status": "complete"},
        previous=previous,
        uploaded_artifact=uploaded_artifact,
        worker_metadata=worker_metadata,
    )
    write_json(Path(args.out), snapshot)
    print(
        f"snapshot written: {args.out} "
        f"analysis={analysis['id']} sarif_id={analysis.get('sarif_id')!r} "
        f"run_attempt={analysis.get('run_attempt')!r} results={len(sarif_results)} "
        f"alerts={len(alerts)}"
    )
    return 0


def cmd_negative(args: argparse.Namespace) -> int:
    baseline = read_json(Path(args.baseline))
    scope = baseline.get("scope") or {}
    repo = args.repo or scope.get("repository")
    ref = args.ref or scope.get("ref")
    tool = args.tool or scope.get("tool")
    category = args.category or scope.get("category")
    if not all((repo, ref, tool, category, args.failure_sha)):
        raise LifecycleError(
            "negative requires baseline scope plus --failure-sha"
        )
    if not ref.startswith("refs/"):
        raise LifecycleError(f"ref must be complete: {ref!r}")
    for field in ("repository", "ref", "tool", "category"):
        if scope.get(field) != {"repository": repo, "ref": ref, "tool": tool, "category": category}[field]:
            raise LifecycleError(f"negative scope differs from baseline for {field}")

    worker_metadata = validate_worker_metadata(
        read_json(Path(args.metadata)),
        repo,
        ref,
        tool,
        category,
        args.failure_sha,
        args.run_id,
        args.run_attempt,
    )
    if worker_metadata["upload_step_outcome"] not in ("skipped", None):
        raise LifecycleError(
            "negative snapshot requires worker_metadata.upload_step_outcome=skipped"
        )
    if worker_metadata["report_complete"] in (True, "true"):
        raise LifecycleError(
            "negative snapshot cannot use worker_metadata.report_complete=true"
        )
    client = GhClient(args.gh)
    effective_run_id = args.run_id or worker_metadata["run_id"]
    run = validate_run(
        client.get(repo_endpoint(repo, f"actions/runs/{effective_run_id}")),
        effective_run_id,
        args.failure_sha,
        ref,
    )
    if args.run_attempt is not None and str(run.get("run_attempt")) != str(args.run_attempt):
        raise LifecycleError(
            f"wrong run_attempt {run.get('run_attempt')!r}, expected {args.run_attempt!r}"
        )
    upload = verify_upload_skipped(
        client, repo, effective_run_id, args.job_name, args.step_name
    )
    analyses = comparable_analyses(
        client, repo, ref, tool, category, args.failure_sha
    )
    if analyses:
        ids = ", ".join(str(analysis.get("id")) for analysis in analyses)
        raise LifecycleError(
            f"negative verification found comparable analysis at failure SHA {args.failure_sha}: {ids}"
        )
    api_records = collect_alerts(client, repo, ref, tool, category)
    alerts, association = merge_alert_evidence(
        api_records,
        [],
        baseline,
        ref,
        tool,
        category,
        sarif_observed=False,
    )
    analysis = {
        "id": None,
        "sarif_id": None,
        "ref": ref,
        "commit_sha": args.failure_sha,
        "tool": tool,
        "category": category,
        "run_attempt": run.get("run_attempt"),
        "processing_status": "skipped",
        "repository": repo,
    }
    current = make_snapshot(
        repo,
        ref,
        tool,
        category,
        args.failure_sha,
        analysis,
        [],
        alerts,
        association,
        run=run,
        sarif_status=None,
        previous=baseline,
        worker_metadata=worker_metadata,
    )
    current["negative_evidence"] = {
        "upload_step": upload,
        "comparable_analyses_at_failure_sha": [],
    }
    diff = normalized_diff(baseline, current)
    if diff != {"baseline_only": [], "current_only": [], "changed": []}:
        raise LifecycleError(
            "negative verification changed baseline alert state:\n"
            + json.dumps(diff, indent=2, sort_keys=True)
        )
    write_json(Path(args.out), current)
    print(
        f"negative verified: upload=skipped run_attempt={run.get('run_attempt')!r} "
        f"failure_sha={args.failure_sha} baseline alerts={len(alerts)}"
    )
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    baseline = read_json(Path(args.baseline))
    current = read_json(Path(args.current))
    expectation = read_json(Path(args.expect))
    result = compare_with_expectation(baseline, current, expectation)
    if args.out:
        write_json(Path(args.out), result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 3


def add_scope_arguments(parser: argparse.ArgumentParser, optional: bool = False) -> None:
    required = not optional
    parser.add_argument("--repo", required=required)
    parser.add_argument("--ref", required=required)
    parser.add_argument("--tool", default=None if optional else DEFAULT_TOOL)
    parser.add_argument("--category", default=None if optional else DEFAULT_CATEGORY)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="capture a completed analysis")
    add_scope_arguments(snapshot)
    snapshot.add_argument("--commit-sha", required=True)
    snapshot.add_argument("--analysis-id")
    snapshot.add_argument("--sarif-id")
    snapshot.add_argument(
        "--metadata",
        required=True,
        help="clippy-upload-metadata.json from clippy-sarif-<run_id>-<run_attempt>",
    )
    snapshot.add_argument("--previous", help="previous snapshot; also requires a new analysis")
    snapshot.add_argument("--run-id")
    snapshot.add_argument("--run-attempt", type=int)
    snapshot.add_argument(
        "--uploaded-sarif",
        help="exact pre-upload SARIF artifact retained by the workflow worker",
    )
    snapshot.add_argument(
        "--require-uploaded-artifact",
        action="store_true",
        help="fail unless --uploaded-sarif is supplied (use for T0)",
    )
    snapshot.add_argument("--timeout", type=float, default=900.0)
    snapshot.add_argument("--poll-interval", type=float, default=5.0)
    snapshot.add_argument("--gh", default="gh")
    snapshot.add_argument("--out", required=True)
    snapshot.set_defaults(function=cmd_snapshot)

    negative = subparsers.add_parser(
        "negative", help="verify a failed run skipped upload and changed nothing"
    )
    add_scope_arguments(negative, optional=True)
    negative.add_argument("--baseline", required=True)
    negative.add_argument("--failure-sha", required=True)
    negative.add_argument("--run-id")
    negative.add_argument("--run-attempt", type=int)
    negative.add_argument(
        "--metadata",
        required=True,
        help="clippy-upload-metadata.json from clippy-sarif-<run_id>-<run_attempt>",
    )
    negative.add_argument("--job-name", default=DEFAULT_JOB)
    negative.add_argument("--step-name", default=DEFAULT_STEP)
    negative.add_argument("--gh", default="gh")
    negative.add_argument("--out", required=True)
    negative.set_defaults(function=cmd_negative)

    compare = subparsers.add_parser(
        "compare", help="compare two snapshots under a trial expectation"
    )
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--current", required=True)
    compare.add_argument("--expect", required=True)
    compare.add_argument("--out")
    compare.set_defaults(function=cmd_compare)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.function(args)
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
