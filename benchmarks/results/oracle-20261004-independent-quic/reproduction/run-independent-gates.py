"""Capture required gates and the isolated example's validation in one bundle."""
import argparse
import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = Path('/home/wasilij/rustytransfer-bench')
parser = argparse.ArgumentParser()
parser.add_argument('name')
args = parser.parse_args()
root = BASE / ('checks-' + args.name)
gate_log = BASE / ('gate-output-' + args.name + '.log')
assert not root.exists() and not gate_log.exists()
with gate_log.open('w') as output:
    status = subprocess.run(['python3', str(REPO / 'target/check-direct-workspace.py'), args.name],
                            cwd=REPO, stdout=output, stderr=subprocess.STDOUT).returncode
assert root.exists()
(root / 'gate-output.log').write_bytes(gate_log.read_bytes())
print(gate_log.read_text()[-8000:], flush=True)
assert status == 0, status
findings = json.loads((root / 'clippy.sarif').read_text())['runs'][0]['results']
assert len(findings) == 3, findings
assert sorted(item['ruleId'] for item in findings) == ['clippy::as_conversions', 'clippy::expect_used', 'clippy::expect_used']
env = os.environ.copy()
env['CARGO_TARGET_DIR'] = str(BASE / 'target')
env['CARGO_BUILD_JOBS'] = '4'
env['PATH'] = '/home/wasilij/.cargo/bin:' + env['PATH']
for filename, command in (
    ('example-test.log', ['cargo', 'test', '--locked', '--example', 'shared_key_parallel']),
    ('example-clippy.json', ['cargo', 'clippy', '--locked', '--example', 'shared_key_parallel', '--tests', '--message-format=json']),
):
    print('Checking: ' + ' '.join(command), flush=True)
    with (root / filename).open('w') as output, (root / (filename + '.stderr.log')).open('w') as error:
        status = subprocess.run(command, cwd=REPO, env=env, stdout=output, stderr=error).returncode
    assert status == 0, (filename, (root / (filename + '.stderr.log')).read_text()[-6000:])
messages = [json.loads(line) for line in (root / 'example-clippy.json').read_text().splitlines()]
example_warnings = [item['message'] for item in messages if item.get('reason') == 'compiler-message'
    and item['message']['level'] == 'warning' and any(span['file_name'].startswith('examples/') for span in item['message']['spans'])]
assert not example_warnings, example_warnings
(root / 'validation.json').write_text(json.dumps({'required_commands_passed': 9,
    'sarif_identity_gate_passed': True, 'existing_workspace_clippy_findings': 3,
    'example_test_passed': True, 'new_example_clippy_findings': 0}, indent=2) + '\n')
print(json.dumps({'gate_bundle': str(root), 'required_commands_passed': 9,
                  'workspace_clippy_findings': 3, 'new_example_clippy_findings': 0}), flush=True)
