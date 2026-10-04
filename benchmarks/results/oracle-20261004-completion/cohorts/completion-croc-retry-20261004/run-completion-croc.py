"""Prepare the final direct Oracle-to-WSL Croc/Rustytransfer comparison.

This ignored helper performs Oracle work when run. It uses one warm-up and
five alternating measured pairs for each of the 64 and 512 MiB fixtures.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "benchmarks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_oracle_transfer as runner  # noqa: E402
from croc_firewall_lease import OracleFirewallLease  # noqa: E402


RUNS = 5
REMOTE_BASE = "/home/ubuntu/rustytransfer-bench"


def load_manifests(rusty_path, croc_path):
    rusty = json.loads(rusty_path.read_text(encoding="utf-8"))
    croc = json.loads(croc_path.read_text(encoding="utf-8"))
    if not rusty.get("x86_binary") or not rusty.get("arm_binary"):
        raise ValueError("Rustytransfer manifest lacks final local/Oracle binaries")
    if "11.5.4" not in croc.get("local_version", "") or "11.5.4" not in croc.get("remote_version", ""):
        raise ValueError("Croc manifest must identify v11.5.4 at both endpoints")
    local_asset = next(
        asset for name, asset in croc["assets"].items() if "Linux-64bit" in name
    )
    remote_asset = next(
        asset for name, asset in croc["assets"].items() if "Linux-ARM64" in name
    )
    if not croc.get("remote_binary"):
        raise ValueError("Croc manifest lacks the installed Oracle binary path")
    return rusty, croc, local_asset, remote_asset


def create_args(cli, output_dir, remote_root, local_input_64, local_input_512):
    parser = runner.build_parser()
    args = parser.parse_args(
        [
            "--host", cli.host,
            "--user", cli.user,
            "--ssh", cli.ssh,
            "--scp", cli.scp,
            "--ssh-key", str(cli.ssh_key),
            "--croc", str(cli.local_croc),
            "--remote-croc", cli.remote_croc,
            "--croc-version", "11.5.4",
            "--rusty-sender", str(cli.local_rusty),
            "--remote-rusty", cli.remote_rusty,
            "--input-64", str(local_input_64),
            "--input-512", str(local_input_512),
            "--output-dir", str(output_dir),
            "--remote-root", remote_root,
            "--remote-input-64", cli.remote_input_64,
            "--remote-input-512", cli.remote_input_512,
            "--build-id", "completion-v1-default-croc-comparison",
            "--storage-class", "oracle-home-wsl-native-ext4",
            "--runs", str(RUNS),
            "--direction", "oracle-to-wsl",
            "--rusty-path", "direct",
            "--rusty-auth", "invite",
            "--endpoint-cwd", "/home/wasilij/rustytransfer-bench",
            "--timeout", "900",
        ]
    )
    runner.validate_local_args(parser, args)
    return args


def verify_binaries(args, rusty_manifest, croc_manifest, local_asset, remote_asset):
    args.local_rusty_sha256 = runner.sha256_file(args.rusty_sender)
    args.remote_rusty_sha256 = runner.remote_sha256(args, args.remote_rusty)
    args.local_croc_sha256 = runner.sha256_file(args.croc)
    args.remote_croc_sha256 = runner.remote_sha256(args, args.remote_croc)
    expected = (
        (args.local_rusty_sha256, rusty_manifest["x86_binary_sha256"], "local Rustytransfer"),
        (args.remote_rusty_sha256, rusty_manifest["arm_binary_sha256"], "Oracle Rustytransfer"),
        (args.local_croc_sha256, local_asset["binary_sha256"], "local Croc"),
        (args.remote_croc_sha256, remote_asset["binary_sha256"], "Oracle Croc"),
    )
    for actual, recorded, name in expected:
        if actual != recorded:
            raise RuntimeError(f"{name} hash differs from its build manifest")
    for binary in (args.croc,):
        version = subprocess.run(
            [str(binary), "--version"], check=True, capture_output=True, text=True
        ).stdout
        if "11.5.4" not in version:
            raise RuntimeError(f"unexpected local Croc version: {version.strip()}")
        global_help = subprocess.run([str(binary), "--help"], check=True, capture_output=True, text=True).stdout
        send_help = subprocess.run([str(binary), "send", "--help"], check=True, capture_output=True, text=True).stdout
        if "--local" not in global_help or "--transport" not in send_help:
            raise RuntimeError("local Croc lacks the expected global --local / send --transport options")
    remote_version = runner.remote(args, f"{runner.remote_quote(args.remote_croc)} --version")
    if "11.5.4" not in remote_version:
        raise RuntimeError(f"unexpected Oracle Croc version: {remote_version}")
    remote_global_help = runner.remote(args, f"{runner.remote_quote(args.remote_croc)} --help")
    remote_send_help = runner.remote(args, f"{runner.remote_quote(args.remote_croc)} send --help")
    if "--local" not in remote_global_help or "--transport" not in remote_send_help:
        raise RuntimeError("Oracle Croc lacks the expected global --local / send --transport options")


def run_one(args, source, size_mib, output_path, logs, digest, trial, warmup, candidate):
    expected_size = size_mib * 1024 * 1024
    if candidate == "rustytransfer":
        rows, path = runner.run_rusty(
            args, source, size_mib, expected_size, digest, logs, trial, warmup
        )
        runner.append_and_validate_rust_rows(output_path, rows, "direct", path)
    else:
        rows, path = runner.run_croc(
            args, source, size_mib, expected_size, digest, logs, trial, warmup, "auto", "direct"
        )
        runner.append_rows(output_path, rows)
    record = {"candidate": candidate, "run_index": trial, "warmup": warmup, "path": path}
    sender = next(row for row in rows if row['role'] == 'sender')
    print(json.dumps({"size_mib": size_mib, **record,
                      "rate": sender['effective_mib_per_second'],
                      "wall_seconds": sender['wall_seconds']}), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="141.147.1.21")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--scp", default="scp")
    parser.add_argument("--ssh-key", required=True, type=Path)
    parser.add_argument("--rusty-manifest", required=True, type=Path)
    parser.add_argument("--croc-manifest", required=True, type=Path)
    parser.add_argument("--local-input-64", required=True, type=Path)
    parser.add_argument("--local-input-512", required=True, type=Path)
    parser.add_argument("--remote-input-64", required=True)
    parser.add_argument("--remote-input-512", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    cli = parser.parse_args()

    rusty_manifest, croc_manifest, local_asset, remote_asset = load_manifests(
        cli.rusty_manifest, cli.croc_manifest
    )
    cli.local_rusty = Path(rusty_manifest["x86_binary"])
    cli.remote_rusty = rusty_manifest["arm_binary"]
    cli.local_croc = Path(local_asset["binary"])
    cli.remote_croc = croc_manifest["remote_binary"]
    if cli.output_root.exists():
        parser.error(f"output directory already exists: {cli.output_root}")
    if not cli.ssh_key.is_file() or not cli.local_input_64.is_file() or not cli.local_input_512.is_file():
        parser.error("SSH key and both local hash fixtures must exist")

    run_id = uuid.uuid4().hex[:12]
    cli.output_root.mkdir(parents=True)
    harness_hashes = {}
    for label, source in (
        ('runner', REPO / 'benchmarks/run_oracle_transfer.py'),
        ('helper', Path(__file__)),
        ('firewall_helper', Path(__file__).with_name('croc_firewall_lease.py')),
    ):
        data = source.read_bytes()
        (cli.output_root / source.name).write_bytes(data)
        harness_hashes[label + '_sha256'] = hashlib.sha256(data).hexdigest()
    manifest_path = cli.output_root / "comparison-manifest.json"
    audit_path = cli.output_root / "firewall-lease-audit.json"
    order_path = cli.output_root / "run-order.json"
    run_order = []
    manifest_path.write_text(
        json.dumps(
            {
                "direction": "oracle-to-wsl",
                **harness_hashes,
                "candidate_paths": {
                    "rustytransfer": "Iroh direct route, verified by both endpoints",
                    "croc": "direct TCP to Oracle sender endpoint-local Croc listeners",
                },
                "croc_mode": "global --local; send --transport auto; Tailcat/external relay disabled",
                "croc_version": "11.5.4",
                "rustytransfer_manifest": str(cli.rusty_manifest),
                "croc_manifest": str(cli.croc_manifest),
                "runs_per_size_candidate": RUNS,
                "warmups_per_size_candidate": 1,
                "completion_profile_enabled": False,
                "explicit_connection_close": False,
                "local_endpoint_working_directory": "/home/wasilij/rustytransfer-bench",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    lease_args = create_args(
        cli,
        cli.output_root,
        f"{REMOTE_BASE}/run-croc-final-{run_id}-lease",
        cli.local_input_64,
        cli.local_input_512,
    )
    verify_binaries(lease_args, rusty_manifest, croc_manifest, local_asset, remote_asset)
    runner.remote(
        lease_args,
        f"test -x {runner.remote_quote(lease_args.remote_rusty)} && "
        f"test -x {runner.remote_quote(lease_args.remote_croc)} && "
        "command -v sha256sum && command -v setsid && test -x /usr/bin/time && command -v python3",
    )

    with OracleFirewallLease(lease_args, audit_path):
        for size_mib in (64, 512):
            size_dir = cli.output_root / f"oracle-{size_mib}"
            size_dir.mkdir()
            logs = size_dir / "logs"
            logs.mkdir()
            output_path = size_dir / f"oracle-{size_mib}.jsonl"
            output_path.touch(exist_ok=False)
            args = create_args(
                cli,
                size_dir,
                f"{REMOTE_BASE}/run-croc-final-{run_id}-{size_mib}",
                cli.local_input_64,
                cli.local_input_512,
            )
            args.croc_direct_peer_ip = lease_args.croc_direct_peer_ip
            verify_binaries(args, rusty_manifest, croc_manifest, local_asset, remote_asset)
            runner.remote(
                args,
                f"test ! -e {runner.remote_quote(args.remote_root)} && "
                f"mkdir -- {runner.remote_quote(args.remote_root)}",
            )
            source = getattr(cli, f"local_input_{size_mib}")
            expected_size = size_mib * 1024 * 1024
            if source.stat().st_size != expected_size:
                raise RuntimeError(f"local {size_mib} MiB fixture has the wrong size")
            digest = runner.sha256_file(source)
            run_order.append(
                {
                    "size_mib": size_mib,
                    **run_one(args, source, size_mib, output_path, logs, digest, 0, True, "rustytransfer"),
                }
            )
            run_order.append(
                {
                    "size_mib": size_mib,
                    **run_one(args, source, size_mib, output_path, logs, digest, 0, True, "croc"),
                }
            )
            order_path.write_text(json.dumps(run_order, indent=2) + "\n", encoding="utf-8")
            for trial in range(1, RUNS + 1):
                order = ("rustytransfer", "croc") if trial % 2 else ("croc", "rustytransfer")
                for candidate in order:
                    record = run_one(
                        args, source, size_mib, output_path, logs, digest, trial, False, candidate
                    )
                    run_order.append({"size_mib": size_mib, **record})
                    order_path.write_text(json.dumps(run_order, indent=2) + "\n", encoding="utf-8")
            runner.remote(args, f"rmdir -- {runner.remote_quote(args.remote_root)}")

    print(f"Prepared comparison completed: {cli.output_root}", flush=True)


if __name__ == "__main__":
    main()
