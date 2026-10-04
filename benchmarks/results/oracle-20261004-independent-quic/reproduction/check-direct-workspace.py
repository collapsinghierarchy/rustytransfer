"""Repository gates, run only while no timed transfer is active."""
import argparse
import json
import os
import subprocess
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
os.chdir(repo)
p = argparse.ArgumentParser()
p.add_argument('name')
p.add_argument('--start-index', type=int, default=0)
cli = p.parse_args()
root = Path('/home/wasilij/rustytransfer-bench') / ('checks-' + cli.name)
root.mkdir(exist_ok=False)
env = os.environ.copy()
env['CARGO_TARGET_DIR'] = '/home/wasilij/rustytransfer-bench/target'
env['CARGO_BUILD_JOBS'] = '4'
env['PATH'] = '/home/wasilij/.cargo/bin:' + env['PATH']
commands = [
    ['cargo', 'fmt', '--all', '--', '--check'],
    ['cargo', 'test', '--workspace', '--locked'],
    ['cargo', 'check', '-p', 'rustytransfer-wasm', '--target', 'wasm32-unknown-unknown', '--locked'],
    ['python3', '-m', 'unittest', 'discover', '-s', 'benchmarks', '-p', 'test_*.py'],
    ['python3', '.github/scripts/tests/test_rustscan.py'],
    ['python3', '.github/scripts/tests/test_identity.py'],
    ['python3', '.github/scripts/tests/test_report_gate.py'],
    ['cargo', 'clippy', '--workspace', '--lib', '--bins', '--all-features', '--locked', '--message-format=json'],
    ['/home/wasilij/rustytransfer-bench/tools/cargo-deny-0.20.2-x86_64-unknown-linux-musl/cargo-deny', 'check', 'advisories', 'bans', 'licenses', 'sources'],
]
results = []
for index, cmd in enumerate(commands):
    if index < cli.start_index:
        continue
    print('Checking: ' + ' '.join(cmd), flush=True)
    output = root / ('clippy.json' if cmd[1:2] == ['clippy'] else f'{index:02d}.log')
    with output.open('w') as log, (root / f'{index:02d}.stderr.log').open('w') as error:
        result = subprocess.run(cmd, env=env, stdout=log, stderr=error)
    results.append({'command': cmd, 'returncode': result.returncode, 'stdout': str(output), 'stderr': str(root / f'{index:02d}.stderr.log')})
    (root / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    if result.returncode:
        print(output.read_text()[-6000:] + (root / f'{index:02d}.stderr.log').read_text()[-6000:], flush=True)
        raise SystemExit(result.returncode)
report = root / 'clippy.sarif'
with (root / 'clippy.json').open('rb') as source, report.open('wb') as output:
    subprocess.run(['clippy-sarif'], env=env, stdin=source, stdout=output, check=True)
for cmd in ([ 'python3', '.github/scripts/stabilize-clippy-sarif.py', str(report), '--stats', '--fail-on-skipped'],
            ['python3', '.github/scripts/check-clippy-report.py', str(report), '--min-results', '3'],
            ['python3', '.github/scripts/tests/test_identity.py', str(report), str(repo)]):
    subprocess.run(cmd, env=env, check=True)
findings = json.loads(report.read_text())['runs'][0]['results']
print('Clippy findings:', len(findings), flush=True)
for item in findings:
    print(item['ruleId'], item['message']['text'], flush=True)
