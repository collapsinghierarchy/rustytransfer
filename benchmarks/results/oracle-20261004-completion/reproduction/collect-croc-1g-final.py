"""Audit and collect one canonical 1 GiB direct Rustytransfer/Croc comparison."""

import argparse
import hashlib
import ipaddress
import json
import math
import re
import shutil
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "benchmarks"))
import run_oracle_transfer as runner  # noqa: E402


HEX256 = re.compile(r"^[0-9a-f]{64}$")
SECRET = re.compile(r"rtoracle-[^\s'\"]+|rt1:\S+")
ROUTE_LOG_LINE = re.compile(r"starting TCP server on|client\s+.*\s+connected|connected to", re.IGNORECASE)
EXPECTED_PORTS = list(range(9009, 9014))
SIZE_BYTES = 1024 * 1024 * 1024
EXPECTED_PREFIX_SHA256 = "30671134dac585f880ff30d0a898cba69535339855bd938ef68585a8d142c1de"
ARTIFACT_SUFFIXES = {".json", ".jsonl", ".md", ".log", ".txt"}


def finite_nonnegative(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def load_jsonl(path):
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            row["_line"] = line_number
            rows.append(row)
    return rows


def direct_croc_evidence(row, host, firewall):
    evidence = row.get("path_evidence")
    if not isinstance(evidence, dict) or evidence.get("kind") != "croc-local-direct-tcp" or evidence.get("verified") is not True:
        return "missing verified Croc direct-TCP path evidence"
    if row.get("direct_route_verified_both") is not True or evidence.get("local_only") is not True:
        return "Croc local-only endpoint route evidence is not verified"
    if row.get("path") != "direct" or evidence.get("transport_mode") != "auto":
        return "Croc row is not direct local-auto transport"
    if evidence.get("sender_listen_ports") != EXPECTED_PORTS:
        return "Croc sender listeners differ from ports 9009-9013"
    if evidence.get("control_target") != f"{host}:9009" or evidence.get("data_targets") != [f"{host}:{port}" for port in range(9010, 9014)]:
        return "Croc receiver targets differ from the leased Oracle endpoint"
    if evidence.get("sender_local_targets") != [f"127.0.0.1:{port}" for port in EXPECTED_PORTS]:
        return "Croc sender did not use its five local channel counterparts"
    try:
        peer = str(ipaddress.IPv4Address(evidence["sender_remote_peer_ip"]))
        network = ipaddress.IPv4Network(firewall["cidr"], strict=True)
    except (KeyError, ValueError):
        return "Croc sender peer or firewall lease is invalid"
    if network.prefixlen != 32 or peer != str(network.network_address):
        return "Croc sender peer does not match the leased WSL /32"
    if evidence.get("sender_remote_peer_connections", 0) < 5 or evidence.get("sender_loopback_peer_connections", 0) < 5:
        return "Croc logs do not prove all remote and sender-local channels"
    return None


def robust(values):
    median = statistics.median(values)
    return {
        "median": median,
        "mad": statistics.median(abs(value - median) for value in values),
        "min": min(values),
        "max": max(values),
        "count": len(values),
    }


def validate_rows(rows, host, firewall, rusty_manifest, croc_manifest, fixture,
                  *, rusty_build_id="rustytransfer-final-20261003"):
    if firewall.get("exact_input_chain_restored") is not True:
        raise ValueError("Oracle firewall INPUT chain was not restored exactly")
    local_croc = next(asset for name, asset in croc_manifest["assets"].items() if "Linux-64bit" in name)
    remote_croc = next(asset for name, asset in croc_manifest["assets"].items() if "Linux-ARM64" in name)
    expected = {
        "iroh": (rusty_manifest["arm_binary_sha256"], rusty_manifest["x86_binary_sha256"], rusty_build_id, "invite"),
        "croc": (remote_croc["binary_sha256"], local_croc["binary_sha256"], "croc-11.5.4", "croc-secret"),
    }
    grouped = {}
    for row in rows:
        transport = row.get("transport")
        if transport not in expected:
            raise ValueError(f"unexpected transport at row {row['_line']}: {transport}")
        size = row.get("size_bytes")
        reason = None
        if row.get("success") is not True or row.get("direction") != "oracle-to-wsl" or size != SIZE_BYTES:
            reason = "failed transfer or unexpected direction/size"
        digest = fixture.get("sha256")
        if not isinstance(digest, str) or not HEX256.fullmatch(digest):
            reason = reason or "fixture manifest has invalid SHA-256"
        if row.get("source_sha256") != digest or row.get("received_sha256") != digest:
            reason = reason or "source/output SHA-256 differs from the staged fixture"
        if transport == "iroh" and row.get("bytes_transferred") != SIZE_BYTES:
            reason = reason or f"endpoint did not report exactly {SIZE_BYTES // (1024 * 1024)} MiB transferred"
        if row.get("source_staging") != "pre-staged" or row.get("profile_mode") != "standard":
            reason = reason or "source staging or profile mode differs from the experiment"
        sender_hash, receiver_hash, build_id, pairing = expected[transport]
        if (row.get("sender_binary_sha256"), row.get("receiver_binary_sha256")) != (sender_hash, receiver_hash):
            reason = reason or "endpoint executable hashes differ from the final build manifests"
        if row.get("build_id") != build_id or row.get("pairing_mode") != pairing:
            reason = reason or "build or pairing provenance differs from the final manifests"
        for field in ("wall_seconds", "effective_mib_per_second", "sender_cpu_seconds", "receiver_cpu_seconds", "sender_max_rss_kib", "receiver_max_rss_kib"):
            value = row.get(field)
            if not finite_nonnegative(value) or (field == "wall_seconds" and value == 0):
                reason = reason or f"invalid timing/resource field {field}"
        if finite_nonnegative(row.get("wall_seconds")) and row["wall_seconds"] > 0:
            expected_rate = (SIZE_BYTES / (1024 * 1024)) / row["wall_seconds"]
            if not math.isclose(row.get("effective_mib_per_second", -1), expected_rate, rel_tol=1e-9, abs_tol=1e-9):
                reason = reason or f"throughput disagrees with {SIZE_BYTES // (1024 * 1024)} MiB and process wall duration"
        if transport == "iroh":
            if row.get("path") != "direct" or row.get("direct_route_verified_both") is not True or not runner.verified_direct_evidence(row):
                reason = reason or "Rustytransfer endpoint lacks strict verified direct-route evidence"
        else:
            reason = reason or direct_croc_evidence(row, host, firewall)
        if reason:
            raise ValueError(f"invalid {transport} row {row['_line']}: {reason}")
        key = (transport, row.get("run_index"), row.get("warmup"))
        grouped.setdefault(key, []).append(row)

    trials = {}
    for key, endpoints in grouped.items():
        if len(endpoints) != 2 or {row.get("role") for row in endpoints} != {"sender", "receiver"}:
            raise ValueError(f"endpoint pair missing/duplicated for {key}")
        sender = next(row for row in endpoints if row["role"] == "sender")
        receiver = next(row for row in endpoints if row["role"] == "receiver")
        for field in ("wall_seconds", "effective_mib_per_second", "size_bytes", "bytes_transferred", "source_sha256", "received_sha256", "run_index", "warmup", "sender_binary_sha256", "receiver_binary_sha256"):
            if sender.get(field) != receiver.get(field):
                raise ValueError(f"sender/receiver rows disagree on {field} for {key}")
        trials[key] = sender

    for transport in ("iroh", "croc"):
        keys = [key for key in trials if key[0] == transport]
        if len(keys) != 6 or sum(trials[key].get("warmup") is True for key in keys) != 1:
            raise ValueError(f"expected one warmup and five measured {transport} transfers")
        if {trials[key]["run_index"] for key in keys if trials[key].get("warmup") is False} != set(range(1, 6)):
            raise ValueError(f"measured {transport} run indices are incomplete")
    return trials


def summarize(trials):
    candidates = {}
    for transport in ("iroh", "croc"):
        measured = [row for (kind, _, warmup), row in trials.items() if kind == transport and warmup is False]
        candidates[transport] = {
            field: robust([row[field] for row in measured])
            for field in ("effective_mib_per_second", "wall_seconds", "sender_cpu_seconds", "receiver_cpu_seconds", "sender_max_rss_kib", "receiver_max_rss_kib")
        }
        setup = [row.get("handshake_seconds") for row in measured]
        candidates[transport]["setup_seconds"] = robust([value for value in setup if finite_nonnegative(value)]) if any(finite_nonnegative(value) for value in setup) else None
    paired = []
    for index in range(1, 6):
        rusty = trials[("iroh", index, False)]
        croc = trials[("croc", index, False)]
        paired.append({
            "run_index": index,
            "throughput_delta_percent_croc_vs_rusty": (croc["effective_mib_per_second"] / rusty["effective_mib_per_second"] - 1) * 100,
            "wall_delta_seconds_croc_minus_rusty": croc["wall_seconds"] - rusty["wall_seconds"],
        })
    return {"size_mib": 1024, "size_bytes": SIZE_BYTES, "variants": candidates, "paired_deltas": paired}


def copy_raw(source, destination):
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in ARTIFACT_SUFFIXES:
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        if path.suffix.lower() in {".log", ".txt"} and SECRET.search(data.decode("utf-8", errors="replace")):
            raise ValueError(f"unredacted transfer secret in retained diagnostic log: {path}")
        target.write_bytes(data)


def validate_actual_fixture(fixture):
    path_text = fixture.get("local_path")
    if not isinstance(path_text, str):
        raise ValueError("fixture manifest lacks its local input path")
    path = Path(path_text)
    if not path.is_file() or path.stat().st_size != SIZE_BYTES:
        raise ValueError(f"actual local fixture is missing or is not exactly {SIZE_BYTES // (1024 * 1024)} MiB")
    expected_full = fixture.get("sha256")
    if not isinstance(expected_full, str) or not HEX256.fullmatch(expected_full):
        raise ValueError("fixture manifest contains an invalid full SHA-256")
    actual_full = runner.sha256_file(path)
    actual_prefix = hashlib.sha256()
    remaining = 512 * 1024 * 1024
    with path.open("rb") as source:
        while remaining:
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError("actual local fixture ended before the 512 MiB prefix")
            actual_prefix.update(block)
            remaining -= len(block)
    prefix_hash = actual_prefix.hexdigest()
    if actual_full != expected_full:
        raise ValueError("actual local fixture SHA-256 differs from its manifest")
    if prefix_hash != EXPECTED_PREFIX_SHA256 or fixture.get("first_512_mib_sha256") != EXPECTED_PREFIX_SHA256:
        raise ValueError("actual fixture prefix does not match the established 512 MiB input hash")
    return {"path": str(path), "size_bytes": SIZE_BYTES, "sha256": actual_full, "first_512_mib_sha256": prefix_hash}


def audit_croc_logs(comparison_root, rows, host, firewall):
    network = ipaddress.IPv4Network(firewall["cidr"], strict=True)
    if network.prefixlen != 32:
        raise ValueError("firewall lease is not restricted to the WSL /32")
    source_ip = str(network.network_address)
    peer_connection = f"{source_ip} 0 {host} 0"
    trials = {}
    croc_rows = [row for row in rows if row.get("transport") == "croc"]
    for row in croc_rows:
        key = (row.get("run_index"), row.get("warmup"))
        trials.setdefault(key, []).append(row)
    audited = []
    for run_index, warmup in [(0, True), *((index, False) for index in range(1, 6))]:
        key = (run_index, warmup)
        endpoints = trials.get(key, [])
        if len(endpoints) != 2 or {row.get("role") for row in endpoints} != {"sender", "receiver"}:
            raise ValueError(f"Croc endpoint rows are incomplete for trial {key}")
        tag = "warmup" if warmup else str(run_index)
        log_dir = comparison_root / "oracle-1024" / "logs" / f"croc-oracle-to-wsl-1024mib-{tag}"
        sender_path = log_dir / "sender.log"
        receiver_path = log_dir / "receiver.log"
        if not sender_path.is_file() or not receiver_path.is_file():
            raise ValueError(f"Croc sender/receiver diagnostic logs are missing for trial {key}")
        sender_text = sender_path.read_text(encoding="utf-8", errors="replace")
        receiver_text = receiver_path.read_text(encoding="utf-8", errors="replace")
        if SECRET.search(sender_text) or SECRET.search(receiver_text):
            raise ValueError(f"unredacted Croc transfer secret in trial logs for {key}")
        parsed = runner.croc_direct_tcp_evidence(
            sender_text,
            receiver_text,
            peer_connection,
            host,
            expected_peer_ip=source_ip,
        )
        for row in endpoints:
            if row.get("path_evidence") != parsed:
                raise ValueError(f"parsed Croc log route evidence differs from endpoint row for {key}")
        audited.append({
            "run_index": run_index,
            "warmup": warmup,
            "ssh_connection_source_ip": source_ip,
            "path_evidence": parsed,
            "sender": {
                "redacted_full_log_sha256": hashlib.sha256(sender_path.read_bytes()).hexdigest(),
                "tcp_route_lines": [line for line in sender_text.splitlines() if ROUTE_LOG_LINE.search(line)],
            },
            "receiver": {
                "redacted_full_log_sha256": hashlib.sha256(receiver_path.read_bytes()).hexdigest(),
                "tcp_route_lines": [line for line in receiver_text.splitlines() if ROUTE_LOG_LINE.search(line)],
            },
        })
    if len(audited) != 6:
        raise ValueError("expected route-log evidence for the Croc warm-up and all five measured transfers")
    return audited


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-root", required=True, type=Path)
    parser.add_argument("--rusty-manifest", required=True, type=Path)
    parser.add_argument("--croc-manifest", required=True, type=Path)
    parser.add_argument("--fixture-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--host", default="141.147.1.21")
    args = parser.parse_args()
    if not args.comparison_root.is_dir() or args.output.exists():
        parser.error("comparison input must exist and output must be a new directory")
    canonical = args.comparison_root / "oracle-1024" / "oracle-1024.jsonl"
    for path in (canonical, args.comparison_root / "run-order.json", args.comparison_root / "comparison-manifest.json", args.comparison_root / "firewall-lease-audit.json", args.rusty_manifest, args.croc_manifest, args.fixture_manifest):
        if not path.is_file():
            parser.error(f"required audit input is missing: {path}")
    manifest = json.loads((args.comparison_root / "comparison-manifest.json").read_text(encoding="utf-8"))
    firewall = json.loads((args.comparison_root / "firewall-lease-audit.json").read_text(encoding="utf-8"))
    rusty = json.loads(args.rusty_manifest.read_text(encoding="utf-8"))
    croc = json.loads(args.croc_manifest.read_text(encoding="utf-8"))
    fixture = json.loads(args.fixture_manifest.read_text(encoding="utf-8"))
    rusty_manifest_hash = hashlib.sha256(args.rusty_manifest.read_bytes()).hexdigest()
    croc_manifest_hash = hashlib.sha256(args.croc_manifest.read_bytes()).hexdigest()
    fixture_manifest_hash = hashlib.sha256(args.fixture_manifest.read_bytes()).hexdigest()
    if manifest.get("rusty_manifest_sha256") != rusty_manifest_hash or manifest.get("croc_manifest_sha256") != croc_manifest_hash or manifest.get("fixture_manifest_sha256") != fixture_manifest_hash:
        raise ValueError("comparison manifest hashes do not match the exact loaded provenance files")
    if manifest.get("size_bytes") != SIZE_BYTES or manifest.get("fixture_sha256") != fixture.get("sha256"):
        raise ValueError("comparison manifest does not match the 1 GiB fixture metadata")
    actual_fixture = validate_actual_fixture(fixture)
    rows = load_jsonl(canonical)
    if len(rows) != 24:
        raise ValueError(f"expected 24 canonical endpoint rows; found {len(rows)}")
    trials = validate_rows(rows, args.host, firewall, rusty, croc, fixture)
    croc_route_audit = audit_croc_logs(args.comparison_root, rows, args.host, firewall)
    order = json.loads((args.comparison_root / "run-order.json").read_text(encoding="utf-8"))
    expected_order = [("rustytransfer", 0, True), ("croc", 0, True)]
    for index in range(1, 6):
        expected_order.extend((candidate, index, False) for candidate in (("rustytransfer", "croc") if index % 2 else ("croc", "rustytransfer")))
    actual_order = [(item.get("candidate"), item.get("run_index"), item.get("warmup")) for item in order]
    if actual_order != expected_order or len(order) != 12 or any(item.get("size_mib") != 1024 for item in order):
        raise ValueError("run-order log is missing, duplicated, or not alternating")

    report = {
        "comparison_manifest": manifest,
        "comparison": summarize(trials),
        "run_order_transfers": len(order),
        "canonical_endpoint_rows": len(rows),
        "firewall_restored_exactly": firewall.get("exact_input_chain_restored") is True,
        "actual_fixture": actual_fixture,
        "croc_route_log_audit": croc_route_audit,
        "raw_diagnostic_logs_reviewable_and_redacted": True,
        "provenance_sha256": {
            "rusty_manifest": rusty_manifest_hash,
            "croc_manifest": croc_manifest_hash,
            "fixture_manifest": fixture_manifest_hash,
        },
        "claims_limit": "One matched 1 GiB Oracle-to-WSL experiment; results are separate from 64/512 MiB groups and do not establish general WAN performance.",
    }
    args.output.mkdir(parents=True)
    copy_raw(args.comparison_root, args.output / "raw")
    shutil.copy2(args.rusty_manifest, args.output / "provenance-rusty.json")
    shutil.copy2(args.croc_manifest, args.output / "provenance-croc.json")
    shutil.copy2(args.fixture_manifest, args.output / "provenance-fixture.json")
    (args.output / "audit-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Collected validated 1 GiB comparison under {args.output}", flush=True)


if __name__ == "__main__":
    main()
