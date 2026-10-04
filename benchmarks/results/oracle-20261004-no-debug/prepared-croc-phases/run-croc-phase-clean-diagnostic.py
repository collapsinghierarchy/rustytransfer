#!/usr/bin/env python3
"""Run isolated Croc phase diagnostics using the clean cohort's endpoint machinery.

This is not the official Croc comparison. It invokes a source-built v11.5.4
diagnostic binary through frozen profile on/off wrappers, with no CLI debug
flags. The profile environment variable is the only intentional diagnostic
control. The shared clean cohort module is imported read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import posixpath
import re
import statistics
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_oracle_transfer as common  # noqa: E402

COHORT_PATH = HERE / "run-no-debug-cohort.py"
COHORT_SPEC = importlib.util.spec_from_file_location("clean_no_debug_cohort", COHORT_PATH)
if COHORT_SPEC is None or COHORT_SPEC.loader is None:
    raise RuntimeError(f"cannot load shared clean cohort module at {COHORT_PATH}")
cohort = importlib.util.module_from_spec(COHORT_SPEC)
COHORT_SPEC.loader.exec_module(cohort)

TOOLS = Path("/home/wasilij/rustytransfer-bench/tools/croc-phase-20261004")
REMOTE_HOME = "/home/ubuntu/rustytransfer-bench"
MARKER = "CROC_BENCH_PHASE_PROFILE "
PROFILE_ENV = "CROC_BENCH_PHASE_PROFILE"
RUNS_OVERHEAD = 5
RUNS_DIAGNOSTIC = 3
PHASE_FIELDS = ("setup_seconds", "payload_seconds", "completion_seconds", "total_seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def append_fsync(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def validate_profile(log_path: Path, role: str, enabled: bool):
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    records = []
    for line in lines:
        if MARKER in line:
            records.append(json.loads(line[line.index(MARKER) + len(MARKER):].strip()))
    if not enabled:
        if records:
            raise RuntimeError(f"profile-disabled endpoint emitted a phase marker: {log_path}")
        return None
    if len(records) != 1:
        raise RuntimeError(f"expected one phase record from {role}, found {len(records)}: {log_path}")
    record = records[0]
    if record.get("schema_version") != 1 or record.get("role") != role:
        raise RuntimeError(f"wrong phase marker identity from {role}: {record}")
    for name in PHASE_FIELDS:
        value = record.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise RuntimeError(f"invalid {name} in {role} phase marker: {record}")
    if not record.get("payload_start_observed") or not record.get("payload_end_observed"):
        raise RuntimeError(f"successful nonempty transfer lacked payload boundaries: {record}")
    if record["total_seconds"] <= 0 or record["payload_seconds"] <= 0:
        raise RuntimeError(f"nonempty transfer has nonpositive duration: {record}")
    tolerance = max(1e-6, record["total_seconds"] * 1e-6)
    additive = sum(record[name] for name in PHASE_FIELDS[:-1])
    if abs(additive - record["total_seconds"]) > tolerance:
        raise RuntimeError(f"phase spans do not add to total: {record}")
    if role == "sender":
        preparation = record.get("preparation_seconds")
        if not record.get("preparation_end_observed") or isinstance(preparation, bool) or not isinstance(preparation, (int, float)) or not math.isfinite(preparation) or preparation < 0:
            raise RuntimeError(f"sender preparation boundary/span is invalid: {record}")
        if preparation > record["total_seconds"] + tolerance:
            raise RuntimeError(f"sender preparation exceeds total operation: {record}")
    close = record.get("file_close_seconds", 0.0)
    if isinstance(close, bool) or not isinstance(close, (int, float)) or not math.isfinite(close) or close < 0:
        raise RuntimeError(f"invalid file close subspan: {record}")
    containing = record["completion_seconds"] if role == "sender" else record["payload_seconds"]
    if close > containing + tolerance:
        raise RuntimeError(f"file close subspan exceeds {role} containing span: {record}")
    if not isinstance(record.get("setup_boundary"), str) or not isinstance(record.get("payload_end_boundary"), str):
        raise RuntimeError(f"phase marker lacks boundary descriptions: {record}")
    return record


def make_snapshot(args):
    """Snapshot the wrapper's exec'd Croc core, allowing only the profile env."""
    def snapshot(clean_args, *, remote_side: bool, pgid: int, executable: str):
        core_path = args.remote_core if remote_side else str(args.local_core)
        helper = clean_args.remote_observer if remote_side else str(clean_args.observer)
        command = f"python3 {cohort.quote(helper)} snapshot --pgid {int(pgid)} --exe {cohort.quote(core_path)}"
        raw = cohort.remote(clean_args, command) if remote_side else subprocess.check_output(
            [sys.executable, str(clean_args.observer), "snapshot", "--pgid", str(pgid), "--exe", core_path],
            text=True,
        ).strip()
        result = json.loads(raw)
        process = result.get("process")
        if process is None:
            return result
        if process.get("pid", 0) <= 0 or process.get("pgid") != pgid:
            raise RuntimeError(f"unexpected Croc process identity: {process}")
        if Path(process.get("exe", "")).resolve() != Path(core_path).resolve():
            raise RuntimeError(f"wrapper did not exec the pinned Croc core: {process}")
        if process.get("debug_flags_present"):
            raise RuntimeError(f"Croc diagnostic run unexpectedly used CLI debug flags: {process}")
        # Both frozen wrappers set the variable (0 disables marker emission;
        # 1 enables it). No other observer-classified debug environment is allowed.
        expected = [PROFILE_ENV]
        if process.get("debug_env_present") != expected:
            raise RuntimeError(f"unexpected diagnostic environment on Croc core: {process}")
        return result
    return snapshot


def diagnostic_log_check(transport, sender_log, receiver_log):
    """Allow the expected profile marker, but still reject debug CLI flags."""
    sender_text = sender_log.read_text(encoding="utf-8", errors="replace")
    receiver_text = receiver_log.read_text(encoding="utf-8", errors="replace")
    for path, text in ((sender_log, sender_text), (receiver_log, receiver_text)):
        scrubbed = "\n".join(line for line in text.splitlines() if MARKER not in line)
        if re.search(r"(--debug|--verbose|--trace|\s-v(?:\s|$))", scrubbed):
            raise RuntimeError(f"Croc diagnostic emitted or echoed a debug CLI flag: {path}")
    return sender_text, receiver_text


def manifest_validate(cli):
    manifest = json.loads(cli.build_manifest.read_text(encoding="utf-8"))
    if manifest.get("official_tag") != "v11.5.4" or manifest.get("go_version") != "go1.27.0":
        raise RuntimeError("diagnostic build manifest does not pin Croc v11.5.4 and Go 1.27.0")
    if manifest.get("diagnostic_build_is_byte_identical_to_official") is not False:
        raise RuntimeError("manifest must identify this as a source-built diagnostic, not the official release binary")
    if manifest.get("transfer_debug_flags") != []:
        raise RuntimeError("diagnostic build manifest unexpectedly lists transfer debug flags")
    local_hash = sha256(cli.local_core)
    remote_hash = common.remote_sha256(cli.ssh_args, cli.remote_core)
    if local_hash != manifest["binary_sha256"].get("linux-amd64") or remote_hash != manifest["binary_sha256"].get("linux-arm64"):
        raise RuntimeError("actual Croc core hashes differ from the frozen build manifest")
    if str(cli.local_core) != manifest["binary_paths"].get("linux-amd64") or cli.remote_core != manifest["binary_paths"].get("linux-arm64"):
        raise RuntimeError("Croc core paths differ from the frozen build manifest")
    paths = manifest["wrapper_paths"]
    hashes = manifest["wrapper_sha256"]
    wrapper_inputs = (
        (cli.local_off, paths["linux-amd64"]["off"], hashes["linux-amd64"]["off"], "local off"),
        (cli.local_on, paths["linux-amd64"]["on"], hashes["linux-amd64"]["on"], "local on"),
    )
    for path, expected_path, expected_hash, label in wrapper_inputs:
        if str(path) != expected_path or sha256(path) != expected_hash:
            raise RuntimeError(f"{label} wrapper differs from frozen manifest")
    for path, expected_path, expected_hash, label in (
        (cli.remote_off, paths["linux-arm64"]["off"], hashes["linux-arm64"]["off"], "remote off"),
        (cli.remote_on, paths["linux-arm64"]["on"], hashes["linux-arm64"]["on"], "remote on"),
    ):
        if path != expected_path or common.remote_sha256(cli.ssh_args, path) != expected_hash:
            raise RuntimeError(f"{label} wrapper differs from frozen manifest")
    local_off = cli.local_off.read_text(encoding="utf-8")
    local_on = cli.local_on.read_text(encoding="utf-8")
    remote_off = common.remote(cli.ssh_args, f"cat -- {cohort.quote(cli.remote_off)}")
    remote_on = common.remote(cli.ssh_args, f"cat -- {cohort.quote(cli.remote_on)}")
    for off, on, core, label in (
        (local_off, local_on, str(cli.local_core), "local"),
        (remote_off, remote_on, cli.remote_core, "remote"),
    ):
        if off.replace("CROC_BENCH_PHASE_PROFILE=0", "CROC_BENCH_PHASE_PROFILE=MODE") != on.replace("CROC_BENCH_PHASE_PROFILE=1", "CROC_BENCH_PHASE_PROFILE=MODE"):
            raise RuntimeError(f"{label} wrappers differ beyond the profiling value")
        if PROFILE_ENV not in off or core not in off:
            raise RuntimeError(f"{label} wrappers do not execute the declared core/profile mode")
    if manifest.get("source_archive_sha256") != (TOOLS / "original-source.sha256").read_text().split()[0]:
        raise RuntimeError("source archive identity differs from the frozen manifest")
    if manifest.get("instrumentation_patch_sha256") != sha256(TOOLS / "instrumentation.patch"):
        raise RuntimeError("instrumentation patch differs from the frozen manifest")
    tool_manifest = TOOLS / "go-toolchain-manifest.json"
    if manifest.get("toolchain_manifest_sha256") != sha256(tool_manifest):
        raise RuntimeError("Go toolchain manifest differs from the frozen build manifest")
    local_observer_hash = sha256(cli.observer)
    remote_observer_hash = common.remote_sha256(cli.ssh_args, cli.remote_observer)
    if remote_observer_hash != local_observer_hash:
        raise RuntimeError("native and Oracle endpoint observer helpers differ")
    return manifest, local_hash, remote_hash


def stats(values):
    center = statistics.median(values)
    return {
        "mean_seconds": statistics.mean(values),
        "median_seconds": center,
        "range_seconds": [min(values), max(values)],
        "mad_seconds": statistics.median(abs(value - center) for value in values),
    }


def summarize(schedule):
    result = {
        "scope": "Croc source-build phase diagnostics and same-binary profile overhead only; no official release or Rustytransfer score comparison",
        "groups": [],
        "phase_rate_note": "phase rates are arithmetic means of per-run size_mib/phase_seconds; preparation and file-close are non-additive subspans",
        "overhead_interpretation": "profile on/off uses the same source-built core; broad paired spread remains uncertain and accepts no default or performance change",
    }
    for size, mode in ((512, "disabled"), (512, "enabled"), (1024, "enabled"), (2048, "enabled"), (4096, "enabled")):
        items = [row for row in schedule if row["size_mib"] == size and row["mode"] == mode and not row["warmup"]]
        if not items:
            continue
        group = {"size_mib": size, "profile_mode": mode, "measured_runs": len(items),
                 "mean_wall_seconds": statistics.mean(row["wall_seconds"] for row in items),
                 "median_wall_seconds": statistics.median(row["wall_seconds"] for row in items),
                 "wall_range_seconds": [min(row["wall_seconds"] for row in items), max(row["wall_seconds"] for row in items)],
                 "wall_mad_seconds": stats([row["wall_seconds"] for row in items])["mad_seconds"],
                 "arithmetic_mean_mib_per_second": statistics.mean(size / row["wall_seconds"] for row in items)}
        if mode == "enabled":
            for role in ("sender", "receiver"):
                profiles = [row["profiles"][role] for row in items]
                group[f"{role}_phase_stats"] = {
                    name: {
                        **stats([profile[name] for profile in profiles]),
                        "mean_mib_per_second": (
                            statistics.mean(size / profile[name] for profile in profiles if profile[name] > 0)
                            if any(profile[name] > 0 for profile in profiles) else None
                        ),
                    }
                    for name in PHASE_FIELDS
                }
                group[f"{role}_mean_preparation_seconds"] = statistics.mean(
                    profile.get("preparation_seconds", 0.0) for profile in profiles)
                group[f"{role}_mean_file_close_seconds"] = statistics.mean(
                    profile.get("file_close_seconds", 0.0) for profile in profiles)
        result["groups"].append(group)
    off = {row["run_index"]: row for row in schedule if row["size_mib"] == 512 and row["mode"] == "disabled" and not row["warmup"]}
    on = {row["run_index"]: row for row in schedule if row["size_mib"] == 512 and row["mode"] == "enabled" and not row["warmup"]}
    pairs = []
    for index in sorted(set(off) & set(on)):
        off_row, on_row = off[index], on[index]
        pairs.append({"run_index": index,
                      "wall_delta_percent_enabled_vs_disabled": 100 * (on_row["wall_seconds"] / off_row["wall_seconds"] - 1),
                      "rate_delta_percent_enabled_vs_disabled": 100 * ((512 / on_row["wall_seconds"]) / (512 / off_row["wall_seconds"]) - 1)})
    result["paired_overhead"] = {
        "pairs": pairs,
        "mean_wall_delta_percent_enabled_vs_disabled": statistics.mean(
            row["wall_delta_percent_enabled_vs_disabled"] for row in pairs) if pairs else None,
        "median_wall_delta_percent_enabled_vs_disabled": statistics.median(
            row["wall_delta_percent_enabled_vs_disabled"] for row in pairs) if pairs else None,
        "mean_rate_delta_percent_enabled_vs_disabled": statistics.mean(
            row["rate_delta_percent_enabled_vs_disabled"] for row in pairs) if pairs else None,
        "median_rate_delta_percent_enabled_vs_disabled": statistics.median(
            row["rate_delta_percent_enabled_vs_disabled"] for row in pairs) if pairs else None,
    }
    return result


def run_one(cli, args, manifest, hashes, schedule, size, index, warmup, enabled):
    mode = "enabled" if enabled else "disabled"
    args.profile_enabled = enabled
    args.local_croc = cli.local_on if enabled else cli.local_off
    args.remote_croc = cli.remote_on if enabled else cli.remote_off
    args.local_croc_sha256 = hashes["local_core"]
    args.remote_croc_sha256 = hashes["remote_core"]
    args.local_croc_wrapper_sha256 = sha256(args.local_croc)
    args.remote_croc_wrapper_sha256 = common.remote_sha256(cli.ssh_args, args.remote_croc)
    trials_before = set((args.output_root / "trials").iterdir())
    lease_args = SimpleNamespace(**vars(args))
    lease_args.croc_secret = f"rt-phase-{size}-{index}-{uuid.uuid4().hex[:14]}"
    source = cli.local_input_dir / f"input-{size}.bin"
    fixture = manifest["fixtures"][str(size)]
    rows = cohort.run_trial(args, lease_args, size, source, fixture["sha256"], "croc", index, warmup, args.output_root)
    new_dirs = sorted(set((args.output_root / "trials").iterdir()) - trials_before)
    if len(new_dirs) != 1:
        raise RuntimeError(f"could not identify exactly one completed trial directory: {new_dirs}")
    trial_dir = new_dirs[0]
    # Persist the raw completed transfer rows before phase parsing can reject a marker.
    raw_rows = []
    for row in rows:
        role = row["role"]
        raw_rows.append({**row,
                         "transport_mode": f"source-built-phase-diagnostic-{mode}",
                         "phase_diagnostic_build": "croc-v11.5.4-instrumented",
                         "phase_profile_enabled": enabled,
                         "profile_environment": {PROFILE_ENV: "1" if enabled else "0"},
                         "diagnostic_environment_present": [PROFILE_ENV],
                         "debug_environment_present": True,
                         "debug_flags_present": False,
                         "app_metrics_enabled": enabled,
                         "rustytransfer_metrics_jsonl_enabled": False,
                         "core_binary_sha256": hashes["local_core"] if role == "receiver" else hashes["remote_core"],
                         "wrapper_sha256": args.local_croc_wrapper_sha256 if role == "receiver" else args.remote_croc_wrapper_sha256,
                         "local_binary_path": str(cli.local_core),
                         "remote_binary_path": cli.remote_core,
                         "local_launch_wrapper_path": str(args.local_croc),
                         "remote_launch_wrapper_path": args.remote_croc,
                         "phase_profile": None,
                         "phase_profile_validation": "pending"})
    append_fsync(cli.output_root / "raw-endpoint-rows.jsonl", raw_rows)
    try:
        profiles = {role: validate_profile(trial_dir / f"{role}.log", role, enabled)
                    for role in ("sender", "receiver")}
        for row in rows:
            profile = profiles[row["role"]]
            if profile is not None:
                process_seconds = row.get("sender_process_seconds") if row["role"] == "sender" else row.get("receiver_process_seconds")
                if isinstance(process_seconds, bool) or not isinstance(process_seconds, (int, float)) or not math.isfinite(process_seconds) or process_seconds < 0:
                    raise RuntimeError(f"missing process lifetime for {row['role']}: {row}")
                if profile["total_seconds"] > process_seconds + 0.02:
                    raise RuntimeError(f"application span exceeds process lifetime: {profile} / {process_seconds}")
    except Exception as error:
        failure = {"size_mib": size, "mode": mode, "run_index": index, "warmup": warmup, "error": str(error), "trial_dir": str(trial_dir)}
        append_fsync(cli.output_root / "phase-validation-errors.jsonl", [failure])
        for row in raw_rows:
            row["phase_profile_validation"] = "failed"
            row["phase_profile_error"] = str(error)
        append_fsync(cli.output_root / "phase-validation-events.jsonl", raw_rows)
        raise
    for row in raw_rows:
        profile = profiles[row["role"]]
        row["phase_profile"] = profile
        row["phase_profile_validation"] = "accepted"
    append_fsync(cli.output_root / "phase-validation-events.jsonl", raw_rows)
    profile_row_path = cli.output_root / f"{size}mib-{mode}.jsonl"
    append_fsync(profile_row_path, [
        {**row,
         "transport_mode": f"source-built-phase-diagnostic-{mode}",
         "phase_diagnostic_build": "croc-v11.5.4-instrumented",
         "phase_profile_enabled": enabled,
         "diagnostic_environment_present": [PROFILE_ENV],
         "debug_environment_present": True,
         "debug_flags_present": False,
         "app_metrics_enabled": enabled,
         "rustytransfer_metrics_jsonl_enabled": False,
         "core_binary_sha256": hashes["local_core"] if row["role"] == "receiver" else hashes["remote_core"],
         "wrapper_sha256": args.local_croc_wrapper_sha256 if row["role"] == "receiver" else args.remote_croc_wrapper_sha256,
         "local_binary_path": str(cli.local_core),
         "remote_binary_path": cli.remote_core,
         "local_launch_wrapper_path": str(args.local_croc),
         "remote_launch_wrapper_path": args.remote_croc,
         "phase_profile": profiles[row["role"]]}
        for row in rows])
    schedule_row = {"size_mib": size, "mode": mode, "run_index": index, "warmup": warmup,
                    "wall_seconds": rows[0]["wall_seconds"], "path_evidence": rows[0]["path_evidence"],
                    "profiles": profiles, "trial_dir": str(trial_dir), "raw_rows": str(profile_row_path.name)}
    schedule.append(schedule_row)
    append_fsync(cli.output_root / "schedule.jsonl", [schedule_row])
    (cli.output_root / "schedule.json").write_text(json.dumps(schedule, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"size_mib": size, "mode": mode, "run_index": index, "warmup": warmup,
                      "wall_seconds": schedule_row["wall_seconds"], "path": "direct", "profiles": profiles}, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="141.147.1.21")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--scp", default="scp")
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--observer", type=Path, default=cohort.OBSERVER_LOCAL)
    parser.add_argument("--remote-observer", default=cohort.OBSERVER_REMOTE)
    parser.add_argument("--endpoint-cwd", type=Path, required=True)
    parser.add_argument("--remote-root", default=f"{REMOTE_HOME}/tools/croc-phase-20261004")
    parser.add_argument("--remote-input-dir", required=True)
    parser.add_argument("--local-input-dir", type=Path, required=True)
    parser.add_argument("--local-off", type=Path, required=True)
    parser.add_argument("--local-on", type=Path, required=True)
    parser.add_argument("--remote-off", required=True)
    parser.add_argument("--remote-on", required=True)
    parser.add_argument("--local-core", type=Path, required=True)
    parser.add_argument("--remote-core", required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    cli = parser.parse_args()
    if cli.output_root.exists():
        parser.error("output root must be new")
    if not cli.ssh_key.is_file() or not cli.endpoint_cwd.is_dir() or not cli.build_manifest.is_file():
        parser.error("SSH key, endpoint cwd, or frozen diagnostic build manifest is missing")
    if not cli.local_input_dir.is_dir() or not 1 <= cli.timeout <= 900:
        parser.error("local fixtures must exist and timeout must be within 1..900 seconds")
    if posixpath.commonpath((REMOTE_HOME, posixpath.normpath(cli.remote_root))) != REMOTE_HOME:
        parser.error("remote root must stay inside the benchmark home")
    cli.output_root.mkdir(parents=True)
    (cli.output_root / "trials").mkdir()
    args = SimpleNamespace(host=cli.host, user=cli.user, ssh=cli.ssh, scp=cli.scp, ssh_key=cli.ssh_key,
                           endpoint_cwd=cli.endpoint_cwd, remote_root=cli.remote_root,
                           remote_input_dir=cli.remote_input_dir, local_input_dir=cli.local_input_dir,
                           output_root=cli.output_root, timeout=cli.timeout,
                           local_rusty=Path("/unused/rustytransfer"), remote_rusty="/unused/rustytransfer",
                           local_rusty_sha256="0" * 64, remote_rusty_sha256="0" * 64,
                           local_croc=cli.local_off, remote_croc=cli.remote_off,
                           local_croc_sha256="0" * 64, remote_croc_sha256="0" * 64,
                           observer=cli.observer, remote_observer=cli.remote_observer,
                           oracle_peer=cli.host, wsl_public_peer=None,
                           sample_offsets=(3.0, 10.0), raw_jsonl=cli.output_root / "clean-raw.jsonl",
                           errors_path=cli.output_root / "errors.jsonl")
    args.errors_path.touch(exist_ok=False)
    args.ssh_args = SimpleNamespace(host=cli.host, user=cli.user, ssh=cli.ssh, scp=cli.scp, ssh_key=cli.ssh_key)
    cli.ssh_args = args.ssh_args
    args.remote_core = cli.remote_core
    args.local_core = cli.local_core
    manifest, local_hash, remote_hash = manifest_validate(cli)
    args.local_croc_sha256 = local_hash
    args.remote_croc_sha256 = remote_hash
    args.remote_root = posixpath.join(cli.remote_root, f"phase-diagnostic-{uuid.uuid4().hex[:10]}")
    cohort.remote(args, f"test -d {cohort.quote(cli.remote_root)} && mkdir -- {cohort.quote(args.remote_root)}")
    cohort.observer_snapshot = make_snapshot(args)
    cohort.check_transfer_logs = diagnostic_log_check
    fixture_map = {}
    for size in (512, 1024, 2048, 4096):
        path = cli.local_input_dir / f"input-{size}.bin"
        if path.is_file() and path.stat().st_size == size * 1024 * 1024:
            fixture_map[str(size)] = {"sha256": sha256(path), "bytes": size * 1024 * 1024}
        else:
            parser.error(f"missing or wrong-sized native fixture: {path}")
    for size, fixture in fixture_map.items():
        remote_path = f"{cli.remote_input_dir}/input-{size}.bin"
        out = cohort.remote(args, f"stat -c %s -- {cohort.quote(remote_path)} && sha256sum -- {cohort.quote(remote_path)}")
        lines = out.splitlines()
        if int(lines[0]) != fixture["bytes"] or lines[1].split()[0] != fixture["sha256"]:
            parser.error(f"local/remote diagnostic fixture mismatch for {size} MiB")
        fixture["local"] = str(cli.local_input_dir / f"input-{size}.bin")
        fixture["remote"] = remote_path
    # Add verified fixture identities to the in-memory build manifest for trial use.
    manifest["fixtures"] = fixture_map
    manifest["host"] = cli.host
    manifest["remote_core_path"] = cli.remote_core
    manifest["local_core_path"] = str(cli.local_core)
    manifest["source_build_provenance"] = {
        "source_commit": json.loads(cli.build_manifest.read_text())["source_commit"],
        "source_archive_sha256": json.loads(cli.build_manifest.read_text())["source_archive_sha256"],
        "instrumentation_patch_sha256": json.loads(cli.build_manifest.read_text())["instrumentation_patch_sha256"],
        "toolchain_manifest_sha256": json.loads(cli.build_manifest.read_text())["toolchain_manifest_sha256"],
        "build_manifest_sha256": sha256(cli.build_manifest),
    }
    manifest["run_plan"] = {"overhead": {"size_mib": 512, "warmup_per_mode": 1, "measured_pairs": RUNS_OVERHEAD,
                                          "order": "warm both modes, then alternate mode order with same run_index"},
                            "phase_diagnostic": {"sizes_mib": [1024, 2048, 4096], "warmups_per_size": 1,
                                                 "measured_per_size": RUNS_DIAGNOSTIC, "mode": "enabled"},
                            "debug_cli_flags": [], "only_allowed_profile_environment": PROFILE_ENV}
    manifest["phase_boundary_semantics"] = {
        "setup": "ends at the first sender data-worker or receiver non-ping boundary; preparation/setup work can overlap other workers",
        "sender_payload": "ends at queue onDone after all Comm.Send calls return; this measures local send completion, not peer delivery or commit",
        "receiver_payload": "ends after final recipient GetFileReady(true), which includes the last plaintext write, per-file close, and final checks, before TypeFinished",
        "completion": "sender confirmation/wait follows its send boundary; receiver TypeFinished signaling/cleanup follows its GetFileReady boundary, not final commit work",
        "file_close": "non-additive subspan; sender close is inside completion and receiver close is inside payload; receiver close alone is not a sync_data or durable-publication equivalent",
        "total": "instrumented application operation lifetime; compared with GNU time process lifetime, not the outer SSH pair wall",
        "limitations": "profile mode adds a per-chunk boolean check even when disabled; on/off measures this same-source-build incremental cost, not equivalence to the official binary",
        "cross_tool_comparison": "Croc-specific boundaries are not treated as equivalent to Rustytransfer phase boundaries",
    }
    manifest["output_notes"] = {
        "clean_raw_jsonl": "unmodified shared-runner transfer rows retained for traceability; this diagnostic run is not part of the official clean comparison",
        "raw_endpoint_rows": "diagnostic-enriched endpoint records persisted before marker validation",
    }
    manifest["harness_sha256"] = {"this_script": sha256(Path(__file__).resolve()),
                                  "clean_harness": sha256(COHORT_PATH),
                                  "oracle_runner": sha256(Path(common.__file__)),
                                  "croc_log_helper": sha256(HERE / "run_croc_baseline.py"),
                                  "summarizer_dependency": sha256(HERE / "summarize.py"),
                                  "lease_helper": sha256(HERE / "croc_firewall_lease.py"),
                                  "endpoint_observer": sha256(args.observer),
                                  "remote_endpoint_observer": common.remote_sha256(args.ssh_args, args.remote_observer),
                                  "build_manifest": sha256(cli.build_manifest)}
    (cli.output_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for path in (cli.build_manifest, TOOLS / "instrumentation.patch", TOOLS / "go-toolchain-manifest.json"):
        (cli.output_root / path.name).write_bytes(path.read_bytes())
    schedule = []
    cohort_complete = False
    try:
        for enabled in (False, True):
            run_one(cli, args, manifest, {"local_core": local_hash, "remote_core": remote_hash}, schedule, 512, 0, True, enabled)
        for index in range(1, RUNS_OVERHEAD + 1):
            modes = (True, False) if index % 2 == 0 else (False, True)
            for enabled in modes:
                run_one(cli, args, manifest, {"local_core": local_hash, "remote_core": remote_hash}, schedule, 512, index, False, enabled)
        for size in (1024, 2048, 4096):
            run_one(cli, args, manifest, {"local_core": local_hash, "remote_core": remote_hash}, schedule, size, 0, True, True)
            for index in range(1, RUNS_DIAGNOSTIC + 1):
                run_one(cli, args, manifest, {"local_core": local_hash, "remote_core": remote_hash}, schedule, size, index, False, True)
        cohort_complete = True
    finally:
        if cohort_complete:
            cohort.remote(args, f"rmdir -- {cohort.quote(args.remote_root)}")
        else:
            print(f"Incomplete diagnostic remote artifacts retained at {args.remote_root}", file=sys.stderr)
    (cli.output_root / "summary.json").write_text(json.dumps(summarize(schedule), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
