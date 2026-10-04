#!/usr/bin/env python3
"""Verify hashes and extraction of all historical benchmark archives."""
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    index = json.loads((ROOT / "index.json").read_text())
    count = 0
    for entry in index["archives"]:
        bundle = ROOT / entry["file"]
        assert digest(bundle) == entry["sha256"], bundle
        expected = {member["path"] for member in entry["members"]}
        with tempfile.TemporaryDirectory() as temp:
            with tarfile.open(bundle) as archive:
                assert set(archive.getnames()) == expected, bundle
                archive.extractall(temp, filter="data")
            for member in entry["members"]:
                path = Path(temp) / member["path"]
                assert path.stat().st_size == member["bytes"], path
                assert digest(path) == member["sha256"], path
                count += 1
    print(f"Verified {len(index['archives'])} archives and {count} extracted members")


if __name__ == "__main__":
    main()
