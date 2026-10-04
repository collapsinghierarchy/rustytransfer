"""Retain independent-connection measurements and exact reproduction inputs."""
import argparse
import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = Path('/home/wasilij/rustytransfer-bench')
parser = argparse.ArgumentParser()
parser.add_argument('destination', type=Path)
parser.add_argument('--cohort', action='append', default=[])
parser.add_argument('--build', action='append', default=[])
parser.add_argument('--checks', action='append', default=[])
parser.add_argument('--cleanup', action='append', default=[])
args = parser.parse_args()
assert not args.destination.exists()
args.destination.mkdir(parents=True)
inventory = []

def retain(origin, relative):
    data = origin.read_bytes()
    if origin.suffix in ('.json', '.jsonl', '.log', '.txt', '.md', '.py'):
        assert not re.search(rb'rt1:[A-Za-z0-9_-]+', data), origin
    target = args.destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    assert not target.exists(), target
    target.write_bytes(data)
    inventory.append({'path': relative.as_posix(), 'bytes': len(data),
                      'sha256': hashlib.sha256(data).hexdigest()})

(args.destination / '.gitattributes').write_bytes(b'* -text\n')
inventory.append({'path': '.gitattributes', 'bytes': 8,
                  'sha256': hashlib.sha256(b'* -text\n').hexdigest()})
for cohort in args.cohort:
    root = BASE / 'results' / cohort
    assert (root / 'audit-report.json').is_file(), root
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix in ('.json', '.jsonl', '.py', '.md', '.log'):
            retain(path, Path('cohorts') / cohort / path.relative_to(root))
for name in args.build:
    root = BASE / ('build-' + name)
    for filename in ('manifest.json', 'source.tar.gz', 'excluded-build.json', 'x86-build.log', 'arm-build.log'):
        path = root / filename
        if path.is_file():
            retain(path, Path('builds') / name / filename)
for name in args.checks:
    root = BASE / ('checks-' + name)
    assert root.is_dir() and any(root.iterdir()), root
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix in ('.json', '.jsonl', '.sarif', '.log', '.txt', '.py'):
            relative = path.relative_to(root)
            if path.suffix == '.log' and path.parent != root:
                relative = Path('logs') / relative
            retain(path, Path('checks') / name / relative)
for name in args.cleanup:
    retain(BASE / 'results' / name, Path('cleanup') / name)
for filename in ('build-direct-candidate.py', 'run-direct-matrix.py',
                 'prepare-independent-connections.py', 'audit-direct-matrix.py',
                 'analyze-independent-connections.py',
                 'export-independent-evidence.py', 'finalize-independent-inventory.py',
                 'retain-arm-build-logs.py', 'check-direct-workspace.py',
                 'run-independent-gates.py',
                 'check-staged-independent-evidence.py',
                 'root-independent-smoke.py', 'audit-next-cleanup.py',
                 'independent-connections-plan.md'):
    retain(REPO / 'target' / filename, Path('reproduction') / filename)
(args.destination / 'artifact-inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
print(json.dumps({'retained': len(inventory), 'destination': str(args.destination)}))
