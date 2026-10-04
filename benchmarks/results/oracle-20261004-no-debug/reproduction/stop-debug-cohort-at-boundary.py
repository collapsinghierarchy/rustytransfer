"""Stop the superseded debug cohort after the currently running Croc warmup."""
import json
import os
import signal
import time
from pathlib import Path

base = Path('/home/wasilij/rustytransfer-bench/results')
root = base / 'large-phase-20261004'
launch = json.loads((base / 'large-phase-20261004.launcher.json').read_text())
pid = launch['pid']
proc = Path('/proc') / str(pid)
argv = [p.decode() for p in (proc / 'cmdline').read_bytes().split(b'\0') if p]
assert argv == launch['command']
deadline = time.monotonic() + 1000
while time.monotonic() < deadline:
    entries = json.loads((root / 'run-order.json').read_text())
    if any(r['size_mib']==4096 and r['candidate']=='croc' and r['warmup'] for r in entries):
        # run-order is published only after the Croc lease's exact restoration.
        os.kill(pid, signal.SIGSTOP)
        audit = json.loads((root / 'leases/oracle-4096-0.json').read_text())
        assert audit['exact_input_chain_restored'] and audit['before_sha256']==audit['after_sha256']
        record = dict(parent_pid=pid, parent_stopped=True, recorded_transfers=len(entries),
                      reason='User superseded remaining debug samples with final no-debug comparison.',
                      last_croc_lease_restored_exactly=True,
                      note='Parent stopped at a completed-transfer boundary; root verifies no endpoints before terminating the superseded launcher.')
        (root / 'superseded-at-boundary.json').write_text(json.dumps(record, indent=2)+'\n')
        print(json.dumps(record), flush=True)
        break
    if not proc.exists():
        raise RuntimeError('Runner exited before requested safe boundary')
    time.sleep(.01)
else:
    raise TimeoutError('Safe boundary did not occur within the bounded wait')
