"""Generate matching caller-owned 4 GiB AES-CTR fixtures on WSL and Oracle.

Uses the same zero-key/zero-IV AES-256-CTR stream as the 64/512/1 GiB inputs.
This helper only stages deterministic data and never starts a transfer.
"""

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "benchmarks"))
import run_oracle_transfer as runner  # noqa: E402


SIZE_MIB = 4096
SIZE_BYTES = SIZE_MIB * 1024 * 1024
PREFIX_BYTES = 512 * 1024 * 1024
EXPECTED_PREFIX_SHA256 = "30671134dac585f880ff30d0a898cba69535339855bd938ef68585a8d142c1de"
KEY_HEX = "00" * 32
IV_HEX = "00" * 16


def generation_command(destination, suffix):
    dest = shlex.quote(destination)
    temp = shlex.quote(destination + ".tmp-" + suffix)
    return (
        "set -euo pipefail; "
        f"dest={dest}; tmp={temp}; "
        "test -d \"$(dirname -- \"$dest\")\"; test ! -e \"$dest\"; test ! -e \"$tmp\"; "
        "trap 'rm -f -- \"$tmp\"' EXIT; "
        "dd if=/dev/zero bs=1048576 count=4096 status=none | "
        f"openssl enc -aes-256-ctr -K {KEY_HEX} -iv {IV_HEX} -out \"$tmp\"; "
        f"test \"$(stat -c %s -- \"$tmp\")\" = {SIZE_BYTES}; "
        "ln -- \"$tmp\" \"$dest\"; rm -- \"$tmp\"; trap - EXIT"
    )


def prefix_sha256(path):
    digest = hashlib.sha256()
    remaining = PREFIX_BYTES
    with path.open("rb") as source:
        while remaining:
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError("generated 4 GiB input ended before the baseline 512 MiB prefix")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-output", required=True, type=Path)
    parser.add_argument("--remote-output", required=True)
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--host", default="141.147.1.21")
    parser.add_argument("--ssh-key", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    if args.manifest.exists() or args.local_output.exists():
        parser.error("refusing to overwrite the manifest or caller-owned local input")
    if not args.ssh_key.is_file():
        parser.error("SSH key does not exist")

    subprocess.run(["bash", "-lc", generation_command(str(args.local_output), uuid.uuid4().hex)], check=True)
    if args.local_output.stat().st_size != SIZE_BYTES:
        raise RuntimeError("local generated fixture is not exactly 4 GiB")
    prefix_hash = prefix_sha256(args.local_output)
    if prefix_hash != EXPECTED_PREFIX_SHA256:
        raise RuntimeError("4 GiB fixture does not begin with the established 512 MiB input")
    full_hash = runner.sha256_file(args.local_output)
    remote_args = type("RemoteArgs", (), {})()
    remote_args.ssh = args.ssh
    remote_args.ssh_key = args.ssh_key
    remote_args.user = args.user
    remote_args.host = args.host
    runner.remote(remote_args, generation_command(args.remote_output, uuid.uuid4().hex))
    runner.verify_remote_file(remote_args, args.remote_output, SIZE_BYTES, full_hash)
    manifest = {
        "size_mib": SIZE_MIB,
        "size_bytes": SIZE_BYTES,
        "generation": "OpenSSL AES-256-CTR over 4 GiB of zero bytes",
        "key_hex": KEY_HEX,
        "iv_hex": IV_HEX,
        "local_path": str(args.local_output),
        "remote_path": args.remote_output,
        "sha256": full_hash,
        "first_512_mib_sha256": prefix_hash,
        "caller_owned_pre_staged_input": True,
        "generation_is_not_transfer_measurement": True,
    }
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    main()
