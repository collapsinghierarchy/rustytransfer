"""Install exact external-observer bytes in scoped native benchmark tool dirs."""
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo/'benchmarks'))
import run_oracle_transfer as runner

source=Path(__file__).with_name('clean-endpoint-observer.py')
data=source.read_bytes()
digest=hashlib.sha256(data).hexdigest()
base=Path('/home/wasilij/rustytransfer-bench')
local=base/'tools/no-debug-20261004/clean-endpoint-observer.py'
assert not local.exists()
local.parent.mkdir(parents=True,exist_ok=True)
local.write_bytes(data)
remote='/home/ubuntu/rustytransfer-bench/tools/no-debug-20261004/clean-endpoint-observer.py'
args=SimpleNamespace(host='141.147.1.21',user='ubuntu',ssh='ssh',scp='scp',ssh_key=Path('/home/wasilij/.ssh/id_ed25519_oracle'))
runner.remote(args,'mkdir -p -- /home/ubuntu/rustytransfer-bench/tools/no-debug-20261004 && test ! -e '+runner.remote_quote(remote))
runner.copy_to_remote(args,local,remote)
assert runner.remote_sha256(args,remote)==digest
record=dict(sha256=digest,local_path=str(local),shared_path=str(source),remote_path=remote,
            unit_acceptance='test-clean-observer.py passed',live_kernel_acceptance='test-clean-capture-live.py passed',
            source_role='External launcher/sanitizer and read-only OS socket/UDP header observer; no app debug flags.')
(local.parent/'manifest.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record))
