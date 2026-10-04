"""Direct 1/2/4 GiB phase-reference cohort; stock Croc and frozen Rustytransfer."""
import argparse
import hashlib
import importlib.util
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = Path('/home/wasilij/rustytransfer-bench')
sys.path.insert(0, str(REPO / 'benchmarks'))
sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location('common', Path(__file__).with_name('run-completion-croc.py'))
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
runner = common.runner


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-root', required=True, type=Path)
    p.add_argument('--sizes', nargs='+', type=int, default=[1024, 2048, 4096])
    cli = p.parse_args()
    assert set(cli.sizes) <= {1024, 2048, 4096} and len(set(cli.sizes)) == len(cli.sizes)
    assert not cli.output_root.exists()
    cli.host, cli.user, cli.ssh, cli.scp = '141.147.1.21', 'ubuntu', 'ssh', 'scp'
    cli.ssh_key = Path('/home/wasilij/.ssh/id_ed25519_oracle')
    cli.rusty_manifest = BASE / 'build-completion-v1-20261004/manifest.json'
    cli.croc_manifest = BASE / 'tools/croc-11.5.4/manifest.json'
    rusty, croc, local_asset, remote_asset = common.load_manifests(cli.rusty_manifest, cli.croc_manifest)
    cli.local_rusty, cli.remote_rusty = Path(rusty['x86_binary']), rusty['arm_binary']
    cli.local_croc, cli.remote_croc = Path(local_asset['binary']), croc['remote_binary']
    cli.local_input_64, cli.local_input_512 = BASE / 'input-64.bin', BASE / 'input-512.bin'
    cli.remote_input_64 = common.REMOTE_BASE + '/fixtures-20261002/input-64.bin'
    cli.remote_input_512 = common.REMOTE_BASE + '/fixtures-20261002/input-512.bin'
    cli.output_root.mkdir(parents=True)
    harness = {}
    for label, source in [('runner', REPO / 'benchmarks/run_oracle_transfer.py'),
                          ('helper', Path(__file__)),
                          ('common_helper', Path(__file__).with_name('run-completion-croc.py')),
                          ('firewall_helper', Path(__file__).with_name('croc_firewall_lease.py'))]:
        data = source.read_bytes()
        (cli.output_root / source.name).write_bytes(data)
        harness[label + '_sha256'] = hashlib.sha256(data).hexdigest()
    manifests = {'rusty': rusty, 'croc': croc}
    for label, value in manifests.items():
        (cli.output_root / (label + '-manifest.json')).write_text(json.dumps(value, indent=2) + '\n')
    run_id = uuid.uuid4().hex[:12]
    order = []
    fixture_records = {}
    manifest = dict(prepared_at_utc=now(), sizes_mib=cli.sizes, **harness,
                    runs_per_size_candidate=5, warmups_per_size_candidate=1,
                    direction='oracle-to-wsl', completion_profile_enabled=False,
                    explicit_connection_close=False,
                    local_endpoint_working_directory=str(BASE),
                    comparison_policy='Rustytransfer phases individually versus stock Croc full arithmetic mean; no overall Rustytransfer/Croc rate comparison.',
                    firewall_lease_scope='One Croc transfer per lease, 1200 s maximum; each restores exact original INPUT.')
    (cli.output_root / 'comparison-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    probe = common.create_args(cli, cli.output_root, common.REMOTE_BASE + '/unused-' + run_id,
                               cli.local_input_64, cli.local_input_512)
    common.verify_binaries(probe, rusty, croc, local_asset, remote_asset)
    for size in cli.sizes:
        source = BASE / f'input-{size}.bin'
        remote = common.REMOTE_BASE + f'/fixtures-20261002/input-{size}.bin'
        assert source.stat().st_size == size * 1048576
        digest = runner.sha256_file(source)
        runner.verify_remote_file(probe, remote, size * 1048576, digest)
        fixture_records[str(size)] = dict(size_mib=size, size_bytes=size * 1048576,
                                         sha256=digest, local_path=str(source), remote_path=remote,
                                         caller_owned_pre_staged_input=True)
    (cli.output_root / 'fixture-manifest.json').write_text(json.dumps(fixture_records, indent=2) + '\n')
    (cli.output_root / 'status.json').write_text(json.dumps(dict(state='running', started_at_utc=now()), indent=2) + '\n')
    manifest['run_started_at_utc'] = now()
    (cli.output_root / 'comparison-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (cli.output_root / 'leases').mkdir()
    for size in cli.sizes:
        size_dir = cli.output_root / f'oracle-{size}'
        size_dir.mkdir()
        logs = size_dir / 'logs'
        logs.mkdir()
        raw = size_dir / f'oracle-{size}.jsonl'
        raw.touch()
        args = common.create_args(cli, size_dir, common.REMOTE_BASE + f'/run-large-phase-{run_id}-{size}',
                                  cli.local_input_64, cli.local_input_512)
        setattr(args, f'remote_input_{size}', fixture_records[str(size)]['remote_path'])
        common.verify_binaries(args, rusty, croc, local_asset, remote_asset)
        runner.remote(args, f'test ! -e {runner.remote_quote(args.remote_root)} && mkdir -- {runner.remote_quote(args.remote_root)}')
        for trial in range(6):
            candidates = ('rustytransfer', 'croc') if trial == 0 or trial % 2 else ('croc', 'rustytransfer')
            for candidate in candidates:
                source = BASE / f'input-{size}.bin'
                digest = fixture_records[str(size)]['sha256']
                lease_path = cli.output_root / f'leases/oracle-{size}-{trial}.json'
                if candidate == 'croc':
                    with common.OracleFirewallLease(args, lease_path, lease_seconds=1200):
                        record = common.run_one(args, source, size, raw, logs, digest, trial, trial == 0, candidate)
                    record['firewall_lease_audit'] = lease_path.relative_to(cli.output_root).as_posix()
                else:
                    record = common.run_one(args, source, size, raw, logs, digest, trial, trial == 0, candidate)
                order.append(dict(size_mib=size, finished_at_utc=now(), **record))
                (cli.output_root / 'run-order.json').write_text(json.dumps(order, indent=2) + '\n')
        runner.remote(args, f'rmdir -- {runner.remote_quote(args.remote_root)}')
        print(json.dumps(dict(size_mib=size, size_complete=True, at_utc=now())), flush=True)
    (cli.output_root / 'status.json').write_text(json.dumps(dict(success=True, finished_at_utc=now(), recorded_transfers=len(order)), indent=2) + '\n')


if __name__ == '__main__':
    main()
