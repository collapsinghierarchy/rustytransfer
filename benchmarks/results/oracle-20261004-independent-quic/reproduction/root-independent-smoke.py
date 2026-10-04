"""Untimed local correctness checks; every attempt gets a fresh evidence directory."""
import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'benchmarks'))
import run_oracle_transfer as runner

parser = argparse.ArgumentParser()
parser.add_argument('name')
parser.add_argument('--binary', type=Path, default=Path('/home/wasilij/rustytransfer-bench/target/release/examples/shared_key_parallel'))
args = parser.parse_args()
assert args.name.replace('-', '').isalnum()
ROOT = Path('/home/wasilij/rustytransfer-bench') / ('checks-' + args.name)
ROOT.mkdir(exist_ok=False)
(ROOT / 'root-independent-smoke.py').write_bytes(Path(__file__).read_bytes())
BIN = args.binary
CHUNK = 262144
results = []

def sha(path):
    return runner.sha256_file(path)

def source(path, size):
    block = b'\xa5' + os.urandom(1048575)
    with path.open('wb') as output:
        while size:
            output.write(block[:min(size, len(block))])
            size -= min(size, len(block))

def wait_until(predicate, processes, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if any(process.poll() is not None for process in processes):
            raise RuntimeError('endpoint exited before test reached requested phase')
        time.sleep(0.01)
    raise RuntimeError('bounded smoke phase wait timed out')

def start(command, trial, role):
    env = os.environ.copy()
    env['RUSTYTRANSFER_BENCH_PATH_EVIDENCE'] = '1'
    env['RUSTYTRANSFER_METRICS_JSONL'] = str(trial / (role + '.jsonl'))
    env.pop('RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE', None)
    env.pop('RUSTYTRANSFER_BENCH_STREAM_WINDOW_BYTES', None)
    with (trial / (role + '.log')).open('w') as log:
        return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)

def sender(trial, fixture, connections=4, streams=4):
    process = start([str(BIN), '--transport', 'iroh', 'send', '--direct',
        '--chunk-size', str(CHUNK), '--connections', str(connections), '--streams', str(streams),
        '--file', str(fixture), '--identity-file', str(trial / 'identity.key')], trial, 'sender')
    try:
        def invite():
            match = re.search(r'Direct invite:\s*(rt1:\S+)', (trial / 'sender.log').read_text())
            return match.group(1) if match else None
        return process, wait_until(invite, [process], 30)
    except Exception:
        stop([process])
        raise

def receiver(trial, invite):
    return start([str(BIN), '--transport', 'iroh', 'recv', '--invite', invite,
        '--out', str(trial / 'received.bin')], trial, 'receiver')

def stop(processes):
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

def record(name, work):
    trial = ROOT / name
    trial.mkdir()
    try:
        detail = work(trial)
        results.append({'name': name, 'status': 'passed', **detail})
    except Exception as error:
        results.append({'name': name, 'status': 'failed', 'reason': f'{type(error).__name__}: {error}'})
        raise
    finally:
        for log in trial.glob('*.log'):
            runner.redact_direct_invites(log)
        (ROOT / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        print(json.dumps(results[-1]), flush=True)

def successful(trial, fixture, connections, streams):
    processes = []
    try:
        sent, invite = sender(trial, fixture, connections, streams)
        processes.append(sent)
        received = receiver(trial, invite)
        processes.append(received)
        statuses = [process.wait(timeout=120) for process in processes]
        assert statuses == [0, 0], statuses
        expected = sha(fixture)
        assert (trial / 'received.bin').stat().st_size == fixture.stat().st_size
        assert sha(trial / 'received.bin') == expected
        metrics = [json.loads((trial / (role + '.jsonl')).read_text().splitlines()[0]) for role in ('sender', 'receiver')]
        for metric in metrics:
            assert metric['experimental_protocol_version'] == 'shared-key-parallel/2'
            assert (metric['parallel_connections'], metric['parallel_streams'], metric['payload_key_count'], metric['kem_sessions']) == (connections, streams, 1, 1)
        assert runner.validate_experimental_connection_pair(*metrics, connections), 'missing strict per-connection direct proof'
        return {'connections': connections, 'streams': streams, 'bytes': fixture.stat().st_size,
            'sha256': expected, 'binary_sha256': sha(BIN), 'full_hash_verified': True,
            'strict_direct_verified_every_connection_both_ends': True,
            'distinct_connection_ids': {role: [item['stable_id'] for item in metric['connection_evidence']]
                                        for role, metric in zip(('sender', 'receiver'), metrics)}}
    finally:
        stop(processes)

def failing(trial, fixture, mode):
    processes = []
    try:
        sent, invite = sender(trial, fixture)
        processes.append(sent)
        if mode == 'initial-accept':
            sent.send_signal(signal.SIGTERM)
            status = sent.wait(timeout=10)
            assert status != 0
            return {'phase': mode, 'sender_status': status, 'bounded_exit_seconds': 10}
        received = receiver(trial, invite)
        processes.append(received)
        part = trial / 'received.bin.shared-key-part'
        if mode == 'truncate':
            wait_until(part.exists, processes)
            with fixture.open('r+b') as damaged:
                damaged.truncate(0)
        else:
            def payload_started():
                try:
                    with part.open('rb') as output:
                        return output.read(1) == b'\xa5'
                except FileNotFoundError:
                    return False
            wait_until(payload_started, processes)
            target = sent if mode == 'sender-payload' else received
            target.send_signal(signal.SIGTERM)
            assert target.wait(timeout=10) != 0
        statuses = [process.wait(timeout=40) for process in processes]
        assert all(status != 0 for status in statuses), statuses
        assert not part.exists() and not (trial / 'received.bin').exists()
        return {'phase': mode, 'connections': 4, 'streams': 4, 'sender_status': statuses[0],
                'receiver_status': statuses[1], 'partial_removed': True, 'output_absent': True}
    finally:
        stop(processes)

try:
    normal = ROOT / 'source-8mib.bin'
    tiny = ROOT / 'source-64kib.bin'
    large = ROOT / 'source-cancel-512mib.bin'
    truncated = ROOT / 'source-truncate-128mib.bin'
    source(normal, 8 * 1048576)
    source(tiny, 65536)
    for connections, streams in ((1, 1), (1, 4), (4, 4)):
        record(f'success-c{connections}-s{streams}', lambda trial, c=connections, s=streams: successful(trial, normal, c, s))
    record('success-empty-lanes-c4-s4', lambda trial: successful(trial, tiny, 4, 4))
    source(truncated, 128 * 1048576)
    record('truncated-source-c4-s4', lambda trial: failing(trial, truncated, 'truncate'))
    record('cancel-initial-accept-c4-s4', lambda trial: failing(trial, normal, 'initial-accept'))
    source(large, 512 * 1048576)
    record('cancel-sender-payload-c4-s4', lambda trial: failing(trial, large, 'sender-payload'))
    record('cancel-receiver-payload-c4-s4', lambda trial: failing(trial, large, 'receiver-payload'))
finally:
    for log in ROOT.rglob('*.log'):
        runner.redact_direct_invites(log)
    for fixture in ROOT.glob('source-*.bin'):
        fixture.unlink()
