"""Verify retained bytes and inventory decisions, documentation and source audit."""
import argparse
import hashlib
import json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('destination', type=Path)
args = p.parse_args()
root = args.destination.resolve()
inventory_path = root / 'artifact-inventory.json'
for record in json.loads(inventory_path.read_text()):
    path = (root / record['path']).resolve()
    assert path.is_relative_to(root)
    data = path.read_bytes()
    assert len(data) == record['bytes'], record['path']
    assert hashlib.sha256(data).hexdigest() == record['sha256'], record['path']
for name in ('README.md', 'acceptance.json', 'cleanup/final-cleanup.json',
             'provenance/source-audit.json'):
    assert (root / name).is_file(), name
records = []
for path in sorted(root.rglob('*')):
    if path.is_file() and path != inventory_path:
        relative = path.relative_to(root)
        data = path.read_bytes()
        records.append(dict(path=relative.as_posix(), bytes=len(data),
                            sha256=hashlib.sha256(data).hexdigest(),
                            retention='local-only-log' if 'logs' in relative.parts else 'versioned'))
inventory_path.write_text(json.dumps(records, indent=2)+'\n')
print(json.dumps(dict(artifacts=len(records), bytes=sum(row['bytes'] for row in records),
                     versioned=sum(row['retention']=='versioned' for row in records))))
