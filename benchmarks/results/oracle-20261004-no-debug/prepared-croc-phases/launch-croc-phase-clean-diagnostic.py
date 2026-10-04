#!/usr/bin/env python3
"""Freeze the isolated Croc phase-diagnostic inputs; optionally launch a later run.

Default operation only creates an immutable native WSL snapshot. After review,
pass --execute with endpoint/fixture arguments to run that frozen snapshot.
This launcher never changes the clean cohort helper or official benchmark assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_TARGET = Path("/mnt/c/Users/wasil/Documents/GitHub/rustytransfer/target")
HELPER_SNAPSHOT = Path("/home/wasilij/rustytransfer-bench/results/large-phase-20261004")
TOOLS = Path("/home/wasilij/rustytransfer-bench/tools/croc-phase-20261004")
OBSERVER_SHA256 = "771becac9d05a57167a94f3dc2c5e20974a36dc4cb1e2d5ed4c55d416df441c3"
DEFAULT_FREEZE = Path("/home/wasilij/rustytransfer-bench/results/croc-phase-clean-diagnostic-20261004/frozen")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def copy_frozen(source: Path, destination: Path, entries: dict) -> None:
    if not source.is_file():
        raise RuntimeError(f"required frozen input is missing: {source}")
    shutil.copyfile(source, destination)
    entries[str(destination.name)] = {
        "source": str(source),
        "frozen_path": str(destination),
        "sha256": sha256(destination),
        "source_sha256": sha256(source),
    }


def run_frozen(args, parser) -> int:
    manifest_path = args.freeze_dir / "freeze-manifest.json"
    if not args.freeze_dir.is_dir() or not manifest_path.is_file():
        parser.error("--execute requires an existing prepared freeze directory")
    sidecar = args.freeze_dir / "freeze-manifest.sha256"
    if not sidecar.is_file() or sidecar.read_text(encoding="ascii").split()[0] != sha256(manifest_path):
        parser.error("freeze manifest checksum is missing or invalid")
    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, record in source_manifest["helper_dependencies"].items():
        frozen_path = Path(record["frozen_path"])
        if not frozen_path.is_file() or sha256(frozen_path) != record["sha256"]:
            parser.error(f"frozen dependency was modified after preparation: {name}")
    required = (args.ssh_key, args.endpoint_cwd, args.local_input_dir, args.output_root)
    if any(value is None for value in required) or not args.remote_input_dir:
        parser.error("--execute requires --ssh-key, --endpoint-cwd, --local-input-dir, --remote-input-dir, and --output-root")
    if args.output_root.exists():
        parser.error(f"diagnostic output root must be new: {args.output_root}")
    local_wrappers = source_manifest["local_wrappers"]
    remote_wrappers = source_manifest["remote_wrappers"]
    command = [
        sys.executable, str(args.freeze_dir / "run-croc-phase-clean-diagnostic.py"),
        "--host", args.host, "--user", args.user, "--ssh", args.ssh, "--scp", args.scp,
        "--ssh-key", str(args.ssh_key),
        "--observer", str(args.freeze_dir / "clean-endpoint-observer.py"),
        "--remote-observer", "/home/ubuntu/rustytransfer-bench/tools/no-debug-20261004/clean-endpoint-observer.py",
        "--endpoint-cwd", str(args.endpoint_cwd), "--remote-root", args.remote_root,
        "--remote-input-dir", args.remote_input_dir, "--local-input-dir", str(args.local_input_dir),
        "--local-off", local_wrappers["off"], "--local-on", local_wrappers["on"],
        "--remote-off", remote_wrappers["off"], "--remote-on", remote_wrappers["on"],
        "--local-core", source_manifest["local_core_path"], "--remote-core", source_manifest["remote_core_path"],
        "--build-manifest", str(args.freeze_dir / "croc-phase-build-manifest.json"),
        "--output-root", str(args.output_root),
    ]
    print(json.dumps({"launch_command": command}, indent=2), flush=True)
    return subprocess.call(command)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-dir", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--execute", action="store_true", help="run an already frozen diagnostic snapshot")
    parser.add_argument("--host", default="141.147.1.21")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--scp", default="scp")
    parser.add_argument("--ssh-key", type=Path)
    parser.add_argument("--endpoint-cwd", type=Path)
    parser.add_argument("--remote-input-dir")
    parser.add_argument("--local-input-dir", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--remote-root", default="/home/ubuntu/rustytransfer-bench/tools/croc-phase-20261004")
    args = parser.parse_args()

    if args.execute:
        return run_frozen(args, parser)
    if args.freeze_dir.exists():
        parser.error(f"freeze directory must be new: {args.freeze_dir}")
    args.freeze_dir.mkdir(parents=True)
    files = {}
    copy_plan = (
        (REPO_TARGET / "run-croc-phase-clean-diagnostic.py", "run-croc-phase-clean-diagnostic.py"),
        (REPO_TARGET / "run-no-debug-cohort.py", "run-no-debug-cohort.py"),
        (REPO_TARGET / "croc_firewall_lease.py", "croc_firewall_lease.py"),
        (HELPER_SNAPSHOT / "run_oracle_transfer.py", "run_oracle_transfer.py"),
        (HELPER_SNAPSHOT / "run_croc_baseline.py", "run_croc_baseline.py"),
        (HELPER_SNAPSHOT / "summarize.py", "summarize.py"),
        (REPO_TARGET / "clean-endpoint-observer.py", "clean-endpoint-observer.py"),
        (REPO_TARGET / "croc-phase-build-manifest.json", "croc-phase-build-manifest.json"),
        (REPO_TARGET / "go-toolchain-manifest.json", "go-toolchain-manifest.json"),
        (TOOLS / "instrumentation.patch", "instrumentation.patch"),
        (TOOLS / "original-source.sha256", "original-source.sha256"),
        (TOOLS / "source-commit.txt", "source-commit.txt"),
        (TOOLS / "croc-v11.5.4-original.tar.gz", "croc-v11.5.4-original.tar.gz"),
    )
    for source, name in copy_plan:
        copy_frozen(source, args.freeze_dir / name, files)
    if files["clean-endpoint-observer.py"]["sha256"] != OBSERVER_SHA256:
        shutil.rmtree(args.freeze_dir)
        parser.error("clean endpoint observer differs from the reviewed v2 frozen SHA256")

    build = json.loads((args.freeze_dir / "croc-phase-build-manifest.json").read_text(encoding="utf-8"))
    build_path = (args.freeze_dir / "croc-phase-build-manifest.json").resolve()
    source_manifest = {
        "schema_version": 1,
        "freeze_dir": str(args.freeze_dir.resolve()),
        "operation": "native helper/dependency/source snapshot only; no transfer is launched unless --execute is supplied",
        "build_manifest_path": str(build_path),
        "build_manifest_sha256": sha256(build_path),
        "source_commit": build["source_commit"],
        "source_archive_path": str(TOOLS / "croc-v11.5.4-original.tar.gz"),
        "source_archive_sha256": build["source_archive_sha256"],
        "instrumentation_patch_path": str(args.freeze_dir / "instrumentation.patch"),
        "instrumentation_patch_sha256": sha256(args.freeze_dir / "instrumentation.patch"),
        "toolchain_manifest_path": str(args.freeze_dir / "go-toolchain-manifest.json"),
        "toolchain_manifest_sha256": sha256(args.freeze_dir / "go-toolchain-manifest.json"),
        "helper_dependencies": files,
        "local_core_path": build["binary_paths"]["linux-amd64"],
        "local_core_sha256": build["binary_sha256"]["linux-amd64"],
        "remote_core_path": build["binary_paths"]["linux-arm64"],
        "remote_core_sha256": build["binary_sha256"]["linux-arm64"],
        "local_wrappers": build["wrapper_paths"]["linux-amd64"],
        "local_wrapper_sha256": build["wrapper_sha256"]["linux-amd64"],
        "remote_wrappers": build["wrapper_paths"]["linux-arm64"],
        "remote_wrapper_sha256": build["wrapper_sha256"]["linux-arm64"],
        "observer_sha256": OBSERVER_SHA256,
    }
    freeze_manifest = args.freeze_dir / "freeze-manifest.json"
    freeze_manifest.write_text(json.dumps(source_manifest, indent=2) + "\n", encoding="utf-8")
    (args.freeze_dir / "freeze-manifest.sha256").write_text(sha256(freeze_manifest) + "  freeze-manifest.json\n", encoding="ascii")
    print(json.dumps({"frozen": str(args.freeze_dir), "manifest": str(freeze_manifest),
                      "manifest_sha256": sha256(freeze_manifest), "execute": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
