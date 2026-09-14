#!/usr/bin/env python3
"""Fail closed unless a complete, fingerprinted Clippy SARIF meets its floor.

This check runs after clippy-sarif and the repository fingerprint stabilizer.  It
therefore validates the exact SARIF document that the upload step will receive.
The configured floor remains a string until it is validated as ASCII decimal;
the comparison then uses Python's arbitrary-precision integers instead of a
shell arithmetic expansion.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple


_DECIMAL = re.compile(r"[0-9]+", re.ASCII)


class ReportError(ValueError):
    """A report or floor that must prevent a SARIF upload."""


def _display(value: Any) -> str:
    if value is None:
        return "<unset>"
    return repr(value)


def parse_floor(raw: Optional[str]) -> Tuple[str, int]:
    """Return the exact configured text and its arbitrary-precision value."""

    if not isinstance(raw, str) or _DECIMAL.fullmatch(raw) is None:
        raise ReportError(
            "CLIPPY_MIN_RESULTS must be a non-empty non-negative decimal "
            f"integer; received {_display(raw)}"
        )

    # Python integers are arbitrary precision.  Python 3.11+ adds a default
    # digit limit for int(str); disable only that conversion limit so a large,
    # otherwise valid, decimal configuration is compared correctly.
    set_int_max_str_digits = getattr(sys, "set_int_max_str_digits", None)
    if set_int_max_str_digits is not None:
        set_int_max_str_digits(0)
    try:
        value = int(raw)
    except (OverflowError, ValueError) as exc:  # pragma: no cover - defensive
        raise ReportError(
            "CLIPPY_MIN_RESULTS could not be represented as an "
            f"arbitrary-precision decimal integer; received {_display(raw)}"
        ) from exc
    return raw, value


def load_sarif(path: Path) -> Any:
    """Load JSON and turn I/O/JSON failures into a report-gate error."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReportError(f"cannot read SARIF report {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReportError(f"SARIF report {path} is not valid JSON: {exc}") from exc


def _mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise ReportError(f"{where} must be a JSON object")
    return value


def _list(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise ReportError(f"{where} must be a JSON array")
    return value


def _non_negative_integer(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportError(f"{where} must be a non-negative integer")
    return value


def _results(report: Any) -> Iterator[Tuple[int, int, dict]]:
    """Yield ``(run index, result index, result)`` after structural checks."""

    root = _mapping(report, "SARIF root")
    if root.get("version") != "2.1.0":
        raise ReportError("SARIF root must declare version '2.1.0'")

    runs = _list(root.get("runs"), "SARIF runs")
    if not runs:
        raise ReportError("SARIF report has no runs")

    for run_index, raw_run in enumerate(runs):
        run = _mapping(raw_run, f"SARIF run {run_index}")
        tool = _mapping(run.get("tool"), f"SARIF run {run_index}.tool")
        driver = _mapping(
            tool.get("driver"), f"SARIF run {run_index}.tool.driver"
        )
        if driver.get("name") != "clippy":
            raise ReportError(
                f"SARIF run {run_index} is not a Clippy run "
                f"(tool name {_display(driver.get('name'))})"
            )

        if "results" not in run:
            raise ReportError(f"SARIF run {run_index} has no results array")
        results = _list(run["results"], f"SARIF run {run_index}.results")
        for result_index, raw_result in enumerate(results):
            yield run_index, result_index, _mapping(
                raw_result, f"SARIF result {run_index}:{result_index}"
            )


def _validate_result(result: dict, run_index: int, result_index: int) -> Tuple[str, str, str]:
    where = f"SARIF result {run_index}:{result_index}"
    rule_id = result.get("ruleId")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ReportError(f"{where} has no non-empty ruleId")

    locations = _list(result.get("locations"), f"{where}.locations")
    if not locations:
        raise ReportError(f"{where} has no primary location")
    location = _mapping(locations[0], f"{where}.locations[0]")
    physical = _mapping(
        location.get("physicalLocation"), f"{where}.locations[0].physicalLocation"
    )
    artifact = _mapping(
        physical.get("artifactLocation"), f"{where}.artifactLocation"
    )
    uri = artifact.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        raise ReportError(f"{where} has no non-empty artifact URI")

    region = _mapping(physical.get("region"), f"{where}.region")
    _non_negative_integer(region.get("byteOffset"), f"{where}.region.byteOffset")
    _non_negative_integer(region.get("byteLength"), f"{where}.region.byteLength")

    fingerprints = _mapping(
        result.get("partialFingerprints"), f"{where}.partialFingerprints"
    )
    fingerprint = fingerprints.get("primaryLocationLineHash")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ReportError(
            f"{where} has no non-empty partialFingerprints.primaryLocationLineHash"
        )

    return rule_id, uri, fingerprint


def validate_report(report: Any, raw_floor: Optional[str]) -> int:
    """Validate the report and return its result count.

    A valid Clippy run with zero results is allowed only when the operator has
    explicitly configured the exact floor ``'0'``.  A missing run or a run with
    malformed results is always rejected, including with floor zero.
    """

    floor_text, floor = parse_floor(raw_floor)
    count = 0
    seen = set()
    for run_index, result_index, result in _results(report):
        identity = _validate_result(result, run_index, result_index)
        if identity in seen:
            rule_id, uri, fingerprint = identity
            raise ReportError(
                "duplicate Clippy finding identity in the SARIF report: "
                f"ruleId={rule_id!r}, uri={uri!r}, "
                f"primaryLocationLineHash={fingerprint!r}"
            )
        seen.add(identity)
        count += 1

    if count == 0 and floor != 0:
        raise ReportError(
            "Clippy SARIF contains zero results, but the configured floor is "
            f"{floor_text!r}; refusing to upload an empty report"
        )
    if count < floor:
        raise ReportError(
            f"Clippy SARIF contains {count} result(s), below the configured "
            f"floor {floor_text!r}; refusing to upload"
        )
    return count


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sarif", type=Path)
    parser.add_argument(
        "--min-results",
        required=True,
        help="exact CLIPPY_MIN_RESULTS text; only non-negative decimal digits are accepted",
    )
    args = parser.parse_args(argv)

    try:
        count = validate_report(load_sarif(args.sarif), args.min_results)
    except ReportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        "Clippy SARIF validation passed: "
        f"{count} fingerprinted result(s); configured floor {args.min_results!r}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
