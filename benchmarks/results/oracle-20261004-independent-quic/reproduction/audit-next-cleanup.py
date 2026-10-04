"""Independent read-only endpoint, listener, lease and firewall cleanup audit."""
import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'benchmarks'))
import run_oracle_transfer as runner

METADATA = r'''
import json, os
from pathlib import Path
active = []
leases = []
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit():
        continue
    try:
        executable = os.readlink(proc / 'exe')
        command = (proc / 'cmdline').read_bytes().split(b'\0')
        cwd = os.readlink(proc / 'cwd')
    except OSError:
        continue
    name = Path(executable).name
    if 'rustytransfer-bench/' in executable and (name.startswith('rustytransfer') or name.startswith('croc')):
        active.append({'pid': int(proc.name), 'binary_name': name})
    if Path(cwd).name.startswith('rt-croc-') and Path(executable).name.startswith('python'):
        leases.append({'pid': int(proc.name), 'lease_directory': Path(cwd).name})
listeners = []
for name in ('tcp', 'tcp6'):
    for line in (Path('/proc/net') / name).read_text().splitlines()[1:]:
        fields = line.split()
        port = int(fields[1].split(':')[1], 16)
        if fields[3] == '0A' and 9009 <= port <= 9013:
            listeners.append(port)
print(json.dumps({'active_benchmark_endpoints': active, 'croc_tcp_listen_ports': sorted(listeners), 'active_firewall_leases': leases}))
'''

p = argparse.ArgumentParser()
p.add_argument('output', type=Path)
p.add_argument('--expected-chain-sha256', default='8eabe87e4569e251c3147f193c0880b3c52ae3eaf5d007db8dda219ad79a53a5')
cli = p.parse_args()
assert not cli.output.exists()
args = SimpleNamespace(ssh='ssh', scp='scp', ssh_key=Path('/home/wasilij/.ssh/id_ed25519_oracle'), user='ubuntu', host='141.147.1.21')
local = json.loads(subprocess.check_output(['python3', '-c', METADATA], text=True))
oracle = json.loads(runner.remote(args, 'python3 -c ' + shlex.quote(METADATA)))
chain = runner.remote(args, 'sudo -n /usr/sbin/iptables -S INPUT') + '\n'
digest = hashlib.sha256(chain.encode()).hexdigest()
assert digest == cli.expected_chain_sha256
for endpoint in (local, oracle):
    assert not endpoint['active_benchmark_endpoints']
    assert not endpoint['croc_tcp_listen_ports']
    assert not endpoint['active_firewall_leases']
report = {'local': local, 'oracle': oracle, 'original_input_chain_restored': True, 'oracle_input_chain_sha256': digest}
cli.output.parent.mkdir(parents=True, exist_ok=True)
cli.output.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
