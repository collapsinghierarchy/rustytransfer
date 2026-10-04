"""Verify immutable retained bytes, then inventory the final decision."""
import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('destination', type=Path)
args = parser.parse_args()
root = args.destination.resolve()
inventory_path = root / 'artifact-inventory.json'
for record in json.loads(inventory_path.read_text()):
    path = (root / record['path']).resolve()
    assert path.is_relative_to(root)
    data = path.read_bytes()
    assert len(data) == record['bytes'], record['path']
    assert hashlib.sha256(data).hexdigest() == record['sha256'], record['path']
for filename in ('README.md', 'acceptance.json', 'cleanup/final-independent-connections-cleanup-20261004.json'):
    assert (root / filename).is_file(), filename
records = []
for path in sorted(root.rglob('*')):
    if not path.is_file() or path == inventory_path:
        continue
    relative = path.relative_to(root)
    data = path.read_bytes()
    records.append({'path': relative.as_posix(), 'bytes': len(data),
                    'sha256': hashlib.sha256(data).hexdigest(),
                    'retention': 'local-only-log' if 'logs' in relative.parts else 'versioned'})
inventory_path.write_text(json.dumps(records, indent=2) + '\n')
print(json.dumps({'artifacts': len(records), 'bytes': sum(r['bytes'] for r in records),
                  'versioned': sum(r['retention'] == 'versioned' for r in records)}))
