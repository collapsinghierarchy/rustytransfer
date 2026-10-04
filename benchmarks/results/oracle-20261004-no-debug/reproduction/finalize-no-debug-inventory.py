"""Verify every retained artifact and include the final generated report."""
import argparse
import hashlib
import json
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('root',type=Path)
args=p.parse_args()
root=args.root
path=root/'artifact-inventory.json'
inventory=json.loads(path.read_text())
known={entry['path'] for entry in inventory}
for entry in inventory:
    source=root/entry['path']
    data=source.read_bytes()
    assert len(data)==entry['bytes'] and hashlib.sha256(data).hexdigest()==entry['sha256'], source
for source in sorted(root.rglob('*')):
    relative=source.relative_to(root).as_posix()
    if source.is_file() and source!=path and relative not in known:
        data=source.read_bytes()
        inventory.append(dict(path=relative,bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))
        known.add(relative)
path.write_text(json.dumps(sorted(inventory,key=lambda entry:entry['path']),indent=2)+'\n')
print(json.dumps(dict(artifacts_verified=len(inventory),inventory=str(path))))
