"""Freeze and detach the no-debug smoke/final cohort in native WSL storage."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
base = Path('/home/wasilij/rustytransfer-bench')
p = argparse.ArgumentParser()
p.add_argument('--mode', choices=('smoke', 'final'), required=True)
p.add_argument('--attempt', type=int, default=1)
cli = p.parse_args()
assert cli.attempt >= 1
suffix = '' if cli.attempt == 1 else '-r' + str(cli.attempt)
tag = 'no-debug-' + cli.mode + '-20261004' + suffix
root = base / 'results' / tag
log = base / 'results' / (tag + '.launcher.log')
assert not root.exists() and not log.exists()
frozen = base / 'tools/no-debug-20261004' / ('harness-' + cli.mode + suffix)
assert not frozen.exists()
frozen.mkdir()
files = [repo/'target/run-no-debug-cohort.py', repo/'target/croc_firewall_lease.py',
         repo/'benchmarks/run_oracle_transfer.py', repo/'benchmarks/run_croc_baseline.py',
         repo/'benchmarks/summarize.py']
hashes = {}
for path in files:
    data = path.read_bytes()
    (frozen/path.name).write_bytes(data)
    hashes[path.name] = hashlib.sha256(data).hexdigest()
(frozen/'snapshot.json').write_text(json.dumps(hashes, indent=2)+'\n')
command = ['python3', str(frozen/'run-no-debug-cohort.py'),
    '--ssh-key', '/home/wasilij/.ssh/id_ed25519_oracle',
    '--endpoint-cwd', str(base),
    '--remote-input-dir', '/home/ubuntu/rustytransfer-bench/fixtures-20261002',
    '--local-input-dir', str(base),
    '--local-rusty', str(base/'build-completion-v1-20261004/rustytransfer-x86'),
    '--remote-rusty', '/home/ubuntu/rustytransfer-bench/build-completion-v1-20261004/rustytransfer-arm',
    '--local-croc', str(base/'tools/croc-11.5.4/croc-x86_64'),
    '--remote-croc', '/home/ubuntu/rustytransfer-bench/bin/croc-11.5.4',
    '--local-rusty-sha256', '48ef6ccd810721285af34d5787d61a00a9bef3a63f82c885c0799c775a41598c',
    '--remote-rusty-sha256', 'c325bf4a2a58b078ea20928bbea2e446fe9c22d907f8d459e9c728c25ef1fba9',
    '--local-croc-sha256', '2688f2e4a150e434fb9ad94a6f5b831365fe3a04f0ab52c4d73f1a7e7830355f',
    '--remote-croc-sha256', '4c23b4697bff52d98c6d5d66037ef393daf41b503da2db66f0542f14e4e63b04',
    '--output-root', str(root)]
if cli.mode == 'smoke':
    command += ['--sizes-mib', '512', '--runs', '1']
with log.open('wb') as output:
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                               stderr=subprocess.STDOUT, start_new_session=True)
record = dict(pid=process.pid, root=str(root), log=str(log), command=command,
              harness_sha256=hashes, mode=cli.mode,
              scope='No debug/profile flags; frozen official Croc and release Rustytransfer.')
(base/'results'/(tag+'.launcher.json')).write_text(json.dumps(record, indent=2)+'\n')
print(json.dumps(record))
