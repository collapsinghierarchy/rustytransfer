"""Terminate the stopped, superseded launcher after independently clean endpoints."""
import json
import os
import signal
from pathlib import Path

base = Path('/home/wasilij/rustytransfer-bench/results')
launch = json.loads((base/'large-phase-20261004.launcher.json').read_text())
cleanup = json.loads((base/'debug-large-boundary-cleanup-20261004.json').read_text())
assert cleanup['original_input_chain_restored']
assert all(not value for role in ('local','oracle') for value in cleanup[role].values())
pid = launch['pid']
proc = Path('/proc') / str(pid)
assert (proc/'stat').read_text().split()[2]=='T'
assert [p.decode() for p in (proc/'cmdline').read_bytes().split(b'\0') if p]==launch['command']
children=[]
for text in (proc/'task'/str(pid)/'children').read_text().split():
    child=Path('/proc')/text
    state=(child/'stat').read_text().split()[2]
    if state!='Z':
        argv=[p.decode() for p in (child/'cmdline').read_bytes().split(b'\0') if p]
        assert Path(argv[0]).name=='ssh'
        command=argv[-1]
        assert '/home/ubuntu/rustytransfer-bench/run-large-phase-' in command
        assert command.startswith('mkdir -- ') and 'setsid' not in command
        os.kill(int(text),signal.SIGTERM)
    children.append(dict(pid=int(text),state=state,scope='completed or pre-transfer metadata child'))
os.kill(pid,signal.SIGKILL)
record=dict(parent_pid=pid,terminated=True,children=children,
            reason='User requires final comparison without debug flags; completed diagnostics retained.')
(base/'large-phase-20261004'/'launcher-termination.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record))
