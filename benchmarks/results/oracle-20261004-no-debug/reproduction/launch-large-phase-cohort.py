"""Launch the authorized stock 1/2/4 GiB cohort independently of chat lifetime."""
import json
import subprocess
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
base = Path('/home/wasilij/rustytransfer-bench')
root = base / 'results/large-phase-20261004'
log = base / 'results/large-phase-20261004.launcher.log'
assert not root.exists() and not log.exists()
command = ['python3', str(repo / 'target/run-large-phase-cohort.py'), '--output-root', str(root)]
with log.open('wb') as output:
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                               stderr=subprocess.STDOUT, start_new_session=True)
record = dict(pid=process.pid, root=str(root), log=str(log), command=command,
              scope='Stock pinned Croc and frozen Rustytransfer; 1/2/4 GiB, warmup plus five alternating runs each, sequential, phase-only reporting.')
(base / 'results/large-phase-20261004.launcher.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record))
