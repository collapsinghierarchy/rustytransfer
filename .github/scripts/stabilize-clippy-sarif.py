#!/usr/bin/env python3
"""Add stable, repository-specific fingerprints to a Clippy SARIF report."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse


FINGERPRINT_VERSION = "rustytransfer-clippy-v2"

# This is intentionally a lightweight item finder rather than a Rust parser.
# The declaration text is only used as a disambiguating anchor; the exact
# diagnostic span and its blame commit remain the primary identity inputs.
ITEM_DECLARATION = re.compile(
    r"(?m)^[ \t]*(?:(?:pub(?:\([^\n]*\))?|async|unsafe|const|extern(?:\s+\"[^\"]*\")?)\s+)*"
    r"(?:fn|struct|enum|trait|impl|mod|type|union|const|static)\b[^\n]*"
)


def normalized_path(uri: str, repository: Path) -> tuple[str, Path] | None:
    uri = unquote(uri)
    if uri.startswith("file://"):
        uri = urlparse(uri).path

    source = Path(uri)
    if not source.is_absolute():
        source = repository / source

    try:
        source = source.resolve()
        relative = source.relative_to(repository.resolve())
    except ValueError:
        return None

    return relative.as_posix(), source


def source_span(result: dict, source: bytes) -> tuple[str, int, int]:
    region = result["locations"][0]["physicalLocation"]["region"]
    start_line = int(region.get("startLine", 1))

    byte_offset = region.get("byteOffset")
    byte_length = region.get("byteLength")
    if byte_offset is not None and byte_length is not None:
        begin = int(byte_offset)
        end = begin + int(byte_length)
        return source[begin:end].decode("utf-8", errors="replace").strip(), start_line, int(
            region.get("startColumn", 0)
        )

    lines = source.decode("utf-8", errors="replace").splitlines()
    line = lines[start_line - 1] if 0 < start_line <= len(lines) else ""
    return line.strip(), start_line, int(region.get("startColumn", 0))


def enclosing_item(source_prefix: str) -> str:
    matches = list(ITEM_DECLARATION.finditer(source_prefix))
    if not matches:
        return "<file-scope>"

    declaration = matches[-1].group(0)
    declaration = declaration.split("{", 1)[0]
    return re.sub(r"\s+", " ", declaration).strip()


def blame_commit(repository: Path, relative_path: str, line: int) -> str:
    command = [
        "git",
        "-C",
        str(repository),
        "blame",
        "--line-porcelain",
        "-M",
        "-C",
        "--follow",
        "-L",
        f"{line},{line}",
        "--",
        relative_path,
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    commit = completed.stdout.splitlines()[0].split()[0]
    return commit.removeprefix("^")


def fingerprint(
    result: dict,
    relative_path: str,
    source: bytes,
    repository: Path,
) -> str:
    span, line, column = source_span(result, source)
    prefix = source[: int(
        result["locations"][0]["physicalLocation"]["region"].get("byteOffset", 0)
    )].decode("utf-8", errors="replace")
    item = enclosing_item(prefix)
    commit = blame_commit(repository, relative_path, line)

    identity = "\0".join(
        (
            FINGERPRINT_VERSION,
            result.get("ruleId", ""),
            relative_path,
            item,
            span,
            str(column),
            commit,
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} SARIF_FILE", file=sys.stderr)
        return 2

    sarif_path = Path(sys.argv[1]).resolve()
    repository = Path.cwd().resolve()
    sarif = json.loads(sarif_path.read_text(encoding="utf-8"))
    updated = 0
    skipped = 0

    for run in sarif.get("runs", []):
        for result in run.get("results", []):
            locations = result.get("locations", [])
            if not locations:
                skipped += 1
                continue

            artifact = locations[0].get("physicalLocation", {}).get("artifactLocation", {})
            uri = artifact.get("uri")
            if not isinstance(uri, str):
                skipped += 1
                continue

            resolved = normalized_path(uri, repository)
            if resolved is None:
                skipped += 1
                continue
            relative_path, source_path = resolved

            try:
                source = source_path.read_bytes()
                value = fingerprint(result, relative_path, source, repository)
            except (OSError, subprocess.CalledProcessError, IndexError, KeyError, ValueError) as error:
                print(
                    f"warning: unable to fingerprint {relative_path}: {error}",
                    file=sys.stderr,
                )
                skipped += 1
                continue

            result["partialFingerprints"] = {"primaryLocationLineHash": value}
            updated += 1

    sarif_path.write_text(json.dumps(sarif, indent=2) + "\n", encoding="utf-8")
    print(f"Added stable fingerprints to {updated} Clippy results; skipped {skipped}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
