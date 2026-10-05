#!/usr/bin/env python3
"""Restore normalized source-file provenance mappings into a separate tree."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any

FIELD = "source_files_sha256"
METHOD = "path-and-sha256-records"
FIELD_RE = re.compile(r'(?P<indent>^[ \t]*)"source_files_sha256"[ \t]*:', re.MULTILINE)
HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class RestoreError(Exception):
    """An input failed validation or could not be restored safely."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_inside(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise RestoreError("normalization index contains an unsafe relative path")
    candidate = (root / Path(*rel.parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise RestoreError("a requested path escapes its selected root") from exc
    return candidate


def load_index(index_path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RestoreError("could not read the normalization index") from exc
    records = value.get("normalizations") if isinstance(value, dict) else None
    if not isinstance(records, list):
        raise RestoreError("normalization index must contain a normalizations array")

    by_path: dict[str, dict[str, Any]] = {}
    required = {
        "path", "original_sha256", "current_sha256", "original_bytes",
        "current_bytes", "method",
    }
    for record in records:
        if not isinstance(record, dict) or not required.issubset(record):
            raise RestoreError("normalization record is missing required fields")
        path = record["path"]
        if not isinstance(path, str) or not path:
            raise RestoreError("normalization record has an invalid path")
        if record["method"] != METHOD:
            continue
        if path in by_path:
            raise RestoreError("normalization index contains a duplicate path")
        for field in ("original_sha256", "current_sha256"):
            if not isinstance(record[field], str) or not HEX_SHA256_RE.fullmatch(record[field]):
                raise RestoreError("normalization record has an invalid digest")
        for field in ("original_bytes", "current_bytes"):
            if not isinstance(record[field], int) or isinstance(record[field], bool) or record[field] < 0:
                raise RestoreError("normalization record has an invalid byte count")
        by_path[path] = record
    return list(by_path.values())


def restore_json(data: bytes, relative_path: str) -> bytes:
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RestoreError("a normalized JSON file is not UTF-8") from exc

    decoder = json.JSONDecoder()
    replacements: list[tuple[int, int, str]] = []
    for match in FIELD_RE.finditer(source):
        value_start = match.end()
        while value_start < len(source) and source[value_start].isspace():
            value_start += 1
        try:
            value, value_end = decoder.raw_decode(source, value_start)
        except json.JSONDecodeError as exc:
            raise RestoreError("a provenance field is malformed JSON") from exc
        if not isinstance(value, list):
            raise RestoreError("a normalized provenance field is not a record array")
        mapping: dict[str, str] = {}
        for item in value:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise RestoreError("a provenance record has an unexpected shape")
            path, digest = item["path"], item["sha256"]
            if not isinstance(path, str) or not isinstance(digest, str) or not HEX_SHA256_RE.fullmatch(digest):
                raise RestoreError("a provenance record has an invalid path or digest")
            if path in mapping:
                raise RestoreError("a provenance array contains a duplicate source path")
            mapping[path] = digest

        indent = match.group("indent")
        rendered = json.dumps(mapping, indent=2, ensure_ascii=False)
        rendered = rendered.replace("\n", "\n" + indent)
        replacements.append((value_start, value_end, rendered))

    if not replacements:
        raise RestoreError("the JSON file contains no source_files_sha256 record array")
    restored = source
    for start, end, rendered in reversed(replacements):
        restored = restored[:start] + rendered + restored[end:]
    return restored.encode("utf-8")


def prepare_destination(destination: Path) -> Path:
    try:
        if destination.exists():
            if not destination.is_dir() or any(destination.iterdir()):
                raise RestoreError("destination must be a new or empty directory")
        else:
            destination.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise RestoreError("could not prepare the destination directory") from exc
    return destination.resolve()


def run(source: Path, destination: Path, index_path: Path) -> int:
    try:
        source_root = source.resolve(strict=True)
        if not source_root.is_dir():
            raise RestoreError("source root is not a directory")
        destination_root = prepare_destination(destination)
        records = load_index(index_path)
        for record in records:
            relative = record["path"]
            source_file = resolve_inside(source_root, relative)
            destination_file = resolve_inside(destination_root, relative)
            if not source_file.is_file():
                raise RestoreError("a normalized source file is missing")
            current = source_file.read_bytes()
            if len(current) != record["current_bytes"] or sha256(current) != record["current_sha256"]:
                raise RestoreError("a source file does not match its normalization record")
            restored = restore_json(current, relative)
            if len(restored) != record["original_bytes"] or sha256(restored) != record["original_sha256"]:
                raise RestoreError("restored source bytes do not match their recorded original")
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            if destination_file.exists():
                raise RestoreError("destination contains a path that would be overwritten")
            destination_file.write_bytes(restored)
    except (OSError, RestoreError) as exc:
        print(f"restore_provenance: {exc}", file=sys.stderr)
        return 1
    print(f"Restored {len(records)} provenance file(s) into {destination_root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="normalized repository root")
    parser.add_argument("--destination", required=True, type=Path, help="new or empty restore directory")
    parser.add_argument("--index", required=True, type=Path, help="normalization index JSON")
    args = parser.parse_args(argv)
    return run(args.source, args.destination, args.index)


if __name__ == "__main__":
    raise SystemExit(main())
