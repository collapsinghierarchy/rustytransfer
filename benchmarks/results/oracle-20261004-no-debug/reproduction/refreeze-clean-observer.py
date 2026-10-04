"""Preserve observer v1 and install the queued-packet validation correction."""
import hashlib
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo/'benchmarks'))
import run_oracle_transfer as runner
base=Path('/home/wasilij/rustytransfer-bench')
tool=base/'tools/no-debug-20261004'
old=tool/'previous-observer-v1'
old.mkdir()
for name in ('clean-endpoint-observer.py','manifest.json'):
    (old/name).write_bytes((tool/name).read_bytes())
source=repo/'target/clean-endpoint-observer.py'
data=source.read_bytes()
digest=hashlib.sha256(data).hexdigest()
local=tool/source.name
local.write_bytes(data)
remote='/home/ubuntu/rustytransfer-bench/tools/no-debug-20261004/clean-endpoint-observer.py'
args=SimpleNamespace(host='141.147.1.21',user='ubuntu',ssh='ssh',scp='scp',ssh_key=Path('/home/wasilij/.ssh/id_ed25519_oracle'))
runner.copy_to_remote(args,local,remote)
assert runner.remote_sha256(args,remote)==digest
record=dict(sha256=digest,local_path=str(local),shared_path=str(source),remote_path=remote,
    previous_observer_sha256=hashlib.sha256((old/source.name).read_bytes()).hexdigest(),
    correction='Revalidate captured headers in userspace and skip pre-filter queued packets instead of aborting.',
    source_role='External observer only; application binaries and commands unchanged.')
(tool/'manifest.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record))
