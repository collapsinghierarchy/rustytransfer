"""Retain audited rows, provenance and reproduction helpers in the repository."""
import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = Path('/home/wasilij/rustytransfer-bench')
p = argparse.ArgumentParser()
p.add_argument('destination', type=Path)
p.add_argument('--cohort', action='append', default=[])
p.add_argument('--build', action='append', default=[])
p.add_argument('--checks', action='append', default=[])
cli = p.parse_args()
assert not cli.destination.exists()
cli.destination.mkdir(parents=True)
inventory = []
attributes = cli.destination / '.gitattributes'
attributes.write_bytes(b'* -text\n')
inventory.append({'path': '.gitattributes', 'bytes': attributes.stat().st_size, 'sha256': hashlib.sha256(attributes.read_bytes()).hexdigest()})

def retain(origin, relative):
    target = cli.destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    data = origin.read_bytes()
    if origin.suffix in ('.json', '.jsonl', '.log', '.txt', '.md'):
        assert not re.search(r'rt1:[A-Za-z0-9_-]+', data.decode('utf-8', errors='replace')), origin
    target.write_bytes(data)
    inventory.append({'path': str(relative), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})

for cohort in cli.cohort:
    root = BASE / 'results' / cohort
    assert (root / 'audit-report.json').exists(), root
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix in ('.json', '.jsonl', '.py', '.md', '.log'):
            retain(path, Path('cohorts') / cohort / path.relative_to(root))
for name in cli.build:
    root = BASE / ('build-' + name)
    for filename in ('manifest.json', 'source.tar.gz', 'excluded-build.json', 'x86-build.log', 'arm-build.log'):
        path = root / filename
        if path.exists():
            retain(path, Path('builds') / name / filename)
for name in cli.checks:
    root = BASE / ('checks-' + name)
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix in ('.json', '.jsonl', '.sarif', '.log', '.txt', '.py'):
            relative = path.relative_to(root)
            if path.suffix == '.log' and path.parent != root:
                relative = Path('logs') / relative
            retain(path, Path('checks') / name / relative)
for filename in ('final-build-20261003.json', 'tools/croc-11.5.4/manifest.json'):
    retain(BASE / filename, Path('provenance') / filename)
for filename in ('build-direct-candidate.py', 'run-direct-matrix.py', 'prepare-direct-matrix.py', 'audit-direct-matrix.py', 'complete-direct-evidence.py', 'export-direct-evidence.py', 'direct-tuning-README.md', 'retain-arm-build-logs.py', 'check-direct-workspace.py', 'shared-key-local-smoke.py', 'audit-next-cleanup.py', 'excluded-parallel-independent-sessions.json', 'audit-next-croc.py', 'run-croc-reverse-final.py', 'croc_firewall_lease.py', 'collect-croc-1g-final.py', 'owned-chunk-candidate.patch', 'owned-chunk-candidate.md'):
    origin = REPO / 'target' / filename
    if origin.exists():
        retain(origin, Path('reproduction') / filename)
retain(REPO / 'target/finalize-direct-inventory.py', Path('reproduction/finalize-direct-inventory.py'))
(cli.destination / 'artifact-inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
print('Retained', len(inventory), 'artifacts under', cli.destination)
