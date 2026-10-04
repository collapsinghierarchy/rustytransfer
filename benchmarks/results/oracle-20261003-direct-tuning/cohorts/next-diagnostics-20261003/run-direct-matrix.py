"""Sequential schedules using the existing strict Oracle runner."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)
sys.path.insert(0, str(REPO / 'benchmarks'))
import run_oracle_transfer as runner

def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')

def main():
    global runner
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=Path)
    cli = parser.parse_args()
    config = json.loads(cli.config.read_text())
    root = Path(config['output_root'])
    root.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex[:12]
    frozen_runner = (REPO / 'benchmarks/run_oracle_transfer.py').read_bytes()
    frozen_helper = Path(__file__).read_bytes()
    (root / 'run_oracle_transfer.py').write_bytes(frozen_runner)
    module_spec = importlib.util.spec_from_file_location('frozen_oracle_runner', root / 'run_oracle_transfer.py')
    frozen_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(frozen_module)
    runner = frozen_module
    (root / 'run-direct-matrix.py').write_bytes(frozen_helper)
    write_json(root / 'config.json', config)
    manifest = {'started_at_utc': datetime.now(timezone.utc).isoformat(),
        'runner_sha256': hashlib.sha256(frozen_runner).hexdigest(),
        'helper_sha256': hashlib.sha256(frozen_helper).hexdigest(),
        'schedule': [], 'fixtures': {}, 'variants': config['variants'],
        'status': 'running', 'limits': 'Warm-cache native ext4 Oracle/WSL WAN measurements. All full hashes outside timers. No simultaneous builds or other jobs.'}
    write_json(root / 'manifest.json', manifest)
    base = Path('/home/wasilij/rustytransfer-bench')
    remote_base = '/home/ubuntu/rustytransfer-bench'
    sources = {}
    for group in config['groups']:
        size = group['size_mib']
        if str(size) not in sources:
            if size in (64, 512, 1024, 2048):
                source = base / f'input-{size}.bin'
            else:
                source = root / f'input-{size}.bin'
                count = int(size * 1024 * 1024)
                with (base / 'input-64.bin').open('rb') as origin:
                    source.write_bytes(origin.read(count))
            assert source.stat().st_size == int(size * 1024 * 1024)
            digest = runner.sha256_file(source)
            sources[str(size)] = (source, digest)
            manifest['fixtures'][str(size)] = {'path': str(source), 'size_bytes': source.stat().st_size, 'sha256': digest}
    try:
        for group_index, group in enumerate(config['groups']):
            group_root = root / f'{group_index:02d}-{group["name"]}'
            group_root.mkdir()
            args_by_variant = {}
            for variant in group['variants']:
                spec = config['variants'][variant]
                output = group_root / variant
                output.mkdir()
                (output / 'logs').mkdir()
                rp = runner.build_parser()
                args = rp.parse_args([
                    '--host', '141.147.1.21', '--ssh-key', '/home/wasilij/.ssh/id_ed25519_oracle',
                    '--rusty-sender', spec['x86_binary'], '--remote-rusty', spec['arm_binary'],
                    '--input-64', str(base / 'input-64.bin'), '--input-512', str(base / 'input-512.bin'),
                    '--output-dir', str(output), '--remote-root', remote_base + f'/run-next-{run_id}-{group_index}-{variant}',
                    '--build-id', spec.get('build_id', variant), '--storage-class', group['direction'] + '-native-ext4',
                    '--rusty-only', '--rusty-auth', 'invite', '--rusty-path', 'direct',
                    '--direction', group['direction'], '--timeout', '480', '--runs', str(group.get('runs', 5))])
                args.chunk_size = spec.get('chunk_size', 262144)
                args.payload_profile = spec.get('profile', False)
                if args.direction == 'oracle-to-wsl':
                    for size in (64, 512, 1024, 2048):
                        setattr(args, f'remote_input_{size}', remote_base + f'/fixtures-20261002/input-{size}.bin')
                runner.validate_local_args(rp, args)
                args.local_rusty_sha256 = runner.sha256_file(args.rusty_sender)
                args.remote_rusty_sha256 = runner.remote_sha256(args, args.remote_rusty)
                assert args.local_rusty_sha256 == spec['x86_binary_sha256']
                assert args.remote_rusty_sha256 == spec['arm_binary_sha256']
                runner.remote(args, f'test ! -e {runner.remote_quote(args.remote_root)} && mkdir -- {runner.remote_quote(args.remote_root)}')
                args_by_variant[variant] = args
            size = group['size_mib']
            source, digest = sources[str(size)]
            expected = int(size * 1024 * 1024)
            variants = group['variants']
            for trial in range(0, group.get('runs', 5) + 1):
                order = list(variants)
                if (trial + group.get('series', 1)) % 2 == 0:
                    order.reverse()
                if len(order) > 2:
                    offset = trial % len(order)
                    order = order[offset:] + order[:offset]
                for variant in order:
                    args = args_by_variant[variant]
                    entry = {'group': group['name'], 'direction': args.direction, 'size_mib': size, 'variant': variant, 'run_index': trial, 'warmup': trial == 0}
                    try:
                        rows, path = runner.run_rusty(args, source, size, expected, digest, args.output_dir / 'logs', trial, trial == 0)
                        runner.append_and_validate_rust_rows(args.output_dir / 'rows.jsonl', rows, 'direct', path)
                        sender = next(row for row in rows if row['role'] == 'sender')
                        assert sender['chunk_size'] == args.chunk_size
                        entry.update(status='valid', rate=sender['effective_mib_per_second'], sender_cpu=sender['sender_cpu_seconds'], receiver_cpu=sender['receiver_cpu_seconds'])
                    except Exception as error:
                        entry.update(status='excluded', reason=f'{type(error).__name__}: {error}')
                        manifest['schedule'].append(entry)
                        write_json(root / 'manifest.json', manifest)
                        print(json.dumps(entry), flush=True)
                        raise
                    manifest['schedule'].append(entry)
                    write_json(root / 'manifest.json', manifest)
                    print(json.dumps(entry), flush=True)
            for args in args_by_variant.values():
                runner.remote(args, f'rmdir -- {runner.remote_quote(args.remote_root)}')
        manifest['status'] = 'completed'
    except Exception:
        manifest['status'] = 'failed-with-diagnostics-retained'
        raise
    finally:
        manifest['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        write_json(root / 'manifest.json', manifest)

if __name__ == '__main__':
    main()
