"""Bounded orchestration of the existing Oracle runner; no transfer implementation."""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path('benchmarks').resolve()))
import run_oracle_transfer as runner

base = Path('/home/wasilij/rustytransfer-bench')
remote_base = '/home/ubuntu/rustytransfer-bench'
root = base / 'results' / 'arm-comparison-20261002'
root.mkdir(exist_ok=False)
parser = runner.build_parser()
common = parser.parse_args([
    '--host', '141.147.1.21', '--ssh-key', '/home/wasilij/.ssh/id_ed25519_oracle',
    '--rusty-sender', str(base / 'bin/rustytransfer-profile-20261002'),
    '--remote-rusty', remote_base + '/bin/rustytransfer-profile-20261002',
    '--input-64', str(base / 'input-64.bin'), '--input-512', str(base / 'input-512.bin'),
    '--output-dir', str(root), '--remote-root', remote_base + '/run-20261002-arm-comparison',
    '--build-id', 'placeholder', '--storage-class', 'placeholder',
    '--rusty-only', '--rusty-auth', 'invite', '--rusty-path', 'direct',
    '--runs', '5', '--timeout', '360',
])
runner.validate_local_args(parser, common)
common.local_rusty_sha256 = runner.sha256_file(common.rusty_sender)
sources = {size: (base / f'input-{size}.bin') for size in (64, 512)}
source_hashes = {size: runner.sha256_file(path) for size, path in sources.items()}
manifest = {'source_hashes': source_hashes, 'x86_binary_sha256': common.local_rusty_sha256,
            'profile_mode': 'standard', 'runs_per_series_variant_case': 5, 'series': 2,
            'ordering': 'warmups baseline/candidate, then alternating paired trials; second series reverses first order',
            'remote_binaries': {}}
for variant, filename in [('baseline', 'rustytransfer-profile-20261002'), ('candidate', 'rustytransfer-arm-20261002')]:
    manifest['remote_binaries'][variant] = {'path': remote_base + '/bin/' + filename,
        'sha256': runner.remote_sha256(common, remote_base + '/bin/' + filename)}
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

for series in (1, 2):
    # Evaluate the identified ARM sender case first, then the receiver case.
    for direction in ('oracle-to-wsl', 'wsl-to-oracle'):
        args_by_variant = {}
        for variant in ('baseline', 'candidate'):
            args = copy.copy(common)
            args.direction = direction
            args.build_id = f'arm-{variant}-series{series}-20261002'
            args.storage_class = 'oracle-ext4-to-wsl-ext4' if direction == 'oracle-to-wsl' else 'wsl-ext4-to-oracle-ext4'
            args.remote_rusty = manifest['remote_binaries'][variant]['path']
            args.remote_rusty_sha256 = manifest['remote_binaries'][variant]['sha256']
            args.output_dir = root / f'series{series}-{direction}-{variant}'
            args.output_dir.mkdir()
            args.remote_root = remote_base + f'/run-20261002-arm-series{series}-{direction}-{variant}'
            if direction == 'oracle-to-wsl':
                args.remote_input_64 = remote_base + '/fixtures-20261002/input-64.bin'
                args.remote_input_512 = remote_base + '/fixtures-20261002/input-512.bin'
            runner.validate_local_args(parser, args)
            runner.remote(args, f'test ! -e {runner.remote_quote(args.remote_root)} && mkdir -- {runner.remote_quote(args.remote_root)}')
            (args.output_dir / 'logs').mkdir()
            args_by_variant[variant] = args
        for size in (64, 512):
            for trial in range(0, 6):
                order = ('baseline', 'candidate') if (trial + series) % 2 else ('candidate', 'baseline')
                for variant in order:
                    args = args_by_variant[variant]
                    rows, path = runner.run_rusty(args, sources[size], size, size * 1024 * 1024,
                        source_hashes[size], args.output_dir / 'logs', trial, trial == 0)
                    runner.append_and_validate_rust_rows(args.output_dir / f'oracle-{size}.jsonl', rows, 'direct', path)
                    sender = next(row for row in rows if row['role'] == 'sender')
                    print(f'series={series} direction={direction} size={size} variant={variant} trial={trial} '
                          f'MiB_s={sender["effective_mib_per_second"]:.3f} sender_cpu={sender["sender_cpu_seconds"]:.2f} '
                          f'receiver_cpu={sender["receiver_cpu_seconds"]:.2f} verified={sender["direct_route_verified_both"]}', flush=True)
        for args in args_by_variant.values():
            runner.remote(args, f'rmdir -- {runner.remote_quote(args.remote_root)}')
print(f'Complete: {root}', flush=True)
