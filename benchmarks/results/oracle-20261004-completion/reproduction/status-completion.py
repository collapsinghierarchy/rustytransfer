"""Read completion progress and process names without exposing command secrets."""
import argparse
import json
import os
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('root',type=Path,nargs='?',default=Path('/home/wasilij/rustytransfer-bench/results/completion-croc-retry-20261004'))
root=p.parse_args().root
for name in ('run-order.json','firewall-lease-audit.json'):
    path = root/name
    if path.exists():
        data = json.loads(path.read_text())
        print(name, len(data) if isinstance(data,list) else data)
for path in sorted(root.rglob('oracle-*.jsonl')):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    print(path.name, len(rows), [(row['transport'],row['run_index'],row['warmup'],
                                round(row['effective_mib_per_second'],3))
                               for row in rows if row['role']=='sender'])
active = []
for proc in Path('/proc').iterdir():
    if proc.name.isdigit():
        try:
            exe = os.readlink(proc/'exe')
            command = (proc/'cmdline').read_bytes()
        except OSError:
            continue
        if 'rustytransfer-bench/' in exe and Path(exe).name.startswith(('rustytransfer','croc')):
            active.append(dict(pid=int(proc.name), executable_name=Path(exe).name))
        elif b'run-completion-croc.py' in command and int(proc.name)!=os.getpid():
            active.append(dict(pid=int(proc.name), executable_name=Path(exe).name,
                               helper='run-completion-croc.py'))
print('Active local cohort processes',active)
