#!/usr/bin/env python3
"""Mark repository-baselined Clippy findings as externally suppressed in SARIF.

The baseline stores the repository's stable v5 fingerprint, not a GitHub alert
number or source location.  Results stay in the SARIF document: adding a
``suppressions[]`` entry lets ``advanced-security/dismiss-alerts`` apply the
suppressed state to the current GitHub alert after upload.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


JUSTIFICATION = "Accepted in the repository Clippy baseline"


def read_baseline(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        values = data
    elif isinstance(data, dict):
        values = data.get("fingerprints")
    else:
        raise ValueError("baseline must be a JSON array or object")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("baseline fingerprints must be a list of strings")
    if len(values) != len(set(values)):
        raise ValueError("baseline contains duplicate fingerprints")
    return set(values)


def apply_suppressions(sarif: dict, fingerprints: set[str]) -> tuple[int, set[str]]:
    matched: set[str] = set()
    count = 0
    for run in sarif.get("runs", []):
        for result in run.get("results", []):
            partial = result.get("partialFingerprints") or {}
            fingerprint = partial.get("primaryLocationLineHash")
            if fingerprint not in fingerprints:
                continue
            suppressions = result.setdefault("suppressions", [])
            marker = {"kind": "external", "justification": JUSTIFICATION}
            if marker not in suppressions:
                suppressions.append(marker)
                count += 1
            matched.add(fingerprint)
    return count, matched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sarif", type=Path)
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()

    fingerprints = read_baseline(args.baseline)
    sarif = json.loads(args.sarif.read_text(encoding="utf-8"))
    changed, matched = apply_suppressions(sarif, fingerprints)
    args.sarif.write_text(json.dumps(sarif, indent=2) + "\n", encoding="utf-8")
    print(
        f"Applied {changed} Clippy SARIF suppression(s); "
        f"matched {len(matched)}/{len(fingerprints)} baseline fingerprint(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
