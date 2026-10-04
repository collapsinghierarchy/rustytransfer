"""Retain audited completion measurements and exact reproduction inputs."""
import argparse
import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = Path('/home/wasilij/rustytransfer-bench')
p = argparse.ArgumentParser()
p.add_argument('destination', type=Path)
p.add_argument('--checks', required=True)
args = p.parse_args()
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
    inventory.append(dict(path=relative.as_posix(), bytes=len(data),
                          sha256=hashlib.sha256(data).hexdigest()))

(args.destination / '.gitattributes').write_bytes(b'* -text\n')
inventory.append(dict(path='.gitattributes', bytes=8,
                      sha256=hashlib.sha256(b'* -text\n').hexdigest()))
for name in ('completion-diagnostics-20261004', 'completion-screen-20261004',
             'completion-croc-retry-20261004'):
    root = BASE / 'results' / name
    assert (root / 'audit-report.json').is_file(), root
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix in ('.json', '.jsonl', '.py', '.md', '.log'):
            retain(path, Path('cohorts') / name / path.relative_to(root))
root=BASE/'results/completion-croc-20261004'
assert (root/'interruption.json').is_file()
for path in sorted(root.rglob('*')):
    if path.is_file() and path.suffix in ('.json','.jsonl','.py','.md','.log','.txt'):
        retain(path,Path('interrupted/completion-croc-20261004')/path.relative_to(root))
for name in ('completion-interrupted-cleanup-20261004.json',
             'completion-croc-retry-20261004.launcher.json',
             'completion-croc-retry-20261004.launcher.log'):
    retain(BASE/'results'/name,Path('interrupted')/name)
root = BASE / 'build-completion-v1-20261004'
for filename in ('manifest.json', 'source.tar.gz', 'x86-build.log', 'arm-build.log'):
    retain(root / filename, Path('builds/completion-v1-20261004') / filename)
for checks_name in ('completion-20261004',args.checks):
    root = BASE / ('checks-' + checks_name)
    assert root.is_dir() and any(root.iterdir())
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix in ('.json', '.jsonl', '.sarif', '.log', '.txt', '.py'):
            relative = path.relative_to(root)
            if path.suffix == '.log' and path.parent != root:
                relative = Path('logs') / relative
            retain(path, Path('checks') / checks_name / relative)
for filename in ('metric-provenance-cost.json', 'pre-run-audit.json'):
    retain(BASE / 'results/completion-preflight-20261004' / filename,
           Path('preflight') / filename)
retain(BASE / 'results/completion-cleanup-20261004.json',
       Path('cleanup/final-cleanup.json'))
retain(BASE / 'tools/croc-11.5.4/manifest.json', Path('provenance/croc-manifest.json'))
for filename in ('build-direct-candidate.py', 'run-direct-matrix.py',
                 'prepare-completion.py', 'audit-direct-matrix.py',
                 'analyze-completion.py', 'show-completion.py', 'summarize-completion-phases.py',
                 'run-completion-croc.py', 'audit-completion-croc.py', 'launch-completion-croc.py',
                 'recover-completion-interruption.py', 'status-completion.py',
                 'audit-interrupted-completion.py',
                 'collect-croc-1g-final.py', 'croc_firewall_lease.py',
                 'measure-metric-provenance.py', 'completion-plan.md',
                 'retain-arm-build-logs.py', 'check-direct-workspace.py',
                 'audit-independent-cleanup.py', 'audit-completion-source.py',
                 'export-completion-evidence.py', 'finalize-completion-inventory.py',
                 'check-staged-completion-evidence.py'):
    retain(REPO / 'target' / filename, Path('reproduction') / filename)
retain(REPO / 'benchmarks/run_croc_baseline.py', Path('reproduction/run_croc_baseline.py'))
retain(REPO / 'benchmarks/summarize.py', Path('reproduction/summarize.py'))
(args.destination / 'artifact-inventory.json').write_text(json.dumps(inventory, indent=2)+'\n')
print(json.dumps(dict(retained=len(inventory), destination=str(args.destination))))
