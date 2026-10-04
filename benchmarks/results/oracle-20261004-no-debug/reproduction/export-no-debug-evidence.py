"""Export completed clean measurements and superseded diagnostics byte for byte."""
import argparse
import hashlib
import json
import re
from pathlib import Path

repo=Path(__file__).resolve().parents[1]
base=Path('/home/wasilij/rustytransfer-bench')
p=argparse.ArgumentParser()
p.add_argument('destination',type=Path)
cli=p.parse_args()
assert not cli.destination.exists()
assert (base/'results/no-debug-final-20261004/audit-report.json').is_file()
assert (base/'results/no-debug-final-cleanup-20261004.json').is_file()
cli.destination.mkdir(parents=True)
inventory=[]

def retain(source,relative):
    data=source.read_bytes()
    assert not re.search(rb'rt1:[A-Za-z0-9_-]+',data),source
    target=cli.destination/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    assert not target.exists(),target
    target.write_bytes(data)
    inventory.append(dict(path=relative.as_posix(),bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))

def tree(source,relative):
    for path in sorted(source.rglob('*')):
        if path.is_file() and path.suffix in ('.json','.jsonl','.py','.md','.log','.txt'):
            destination=relative/path.relative_to(source)
            if path.suffix=='.log':
                destination=Path('logs')/destination
            retain(path,destination)

(cli.destination/'.gitattributes').write_bytes(b'* -text\n')
inventory.append(dict(path='.gitattributes',bytes=8,sha256=hashlib.sha256(b'* -text\n').hexdigest()))
for root in sorted((base/'results').glob('no-debug-*')):
    if root.is_dir():
        tree(root,Path('cohorts')/root.name)
    elif root.suffix in ('.json','.log'):
        retain(root,(Path('logs') if root.suffix=='.log' else Path('control'))/root.name)
tree(base/'results/large-phase-20261004',Path('superseded/large-phase-20261004'))
for name in ('debug-large-boundary-cleanup-20261004.json','large-phase-20261004.launcher.json',
             'clean-observer-live-test-20261004.json','clean-observer-live-test-r2-20261004.json'):
    retain(base/'results'/name,Path('control')/name)
tree(base/'tools/no-debug-20261004',Path('reproduction/frozen-tools'))
retain(base/'tools/croc-11.5.4/manifest.json',Path('provenance/croc-release-manifest.json'))
retain(base/'build-completion-v1-20261004/manifest.json',Path('provenance/rustytransfer-release-manifest.json'))
retain(base/'fixture-4096-20261004.json',Path('provenance/fixture-4096.json'))
for name in ('run-no-debug-cohort.py','clean-endpoint-observer.py','launch-no-debug-cohort.py',
             'audit-no-debug-cohort.py','status-no-debug-cohort.py','audit-next-cleanup.py',
             'test-clean-observer.py','test-clean-capture-live.py','refreeze-clean-observer.py',
             'prepare-clean-observer.py','large-phase-plan.md','audit-large-phase-cohort.py',
             'audit-debug-large-warmup.py','extract-stock-croc-startup.py','run-large-phase-cohort.py',
             'launch-large-phase-cohort.py','stop-debug-cohort-at-boundary.py','finalize-debug-cohort-stop.py',
             'prepare-4g-fixtures.py','export-no-debug-evidence.py','write-no-debug-report.py',
             'cleanup-no-debug-smoke-artifacts.py','finalize-no-debug-inventory.py',
             'check-staged-no-debug-evidence.py','extract-clean-croc-progress.py'):
    retain(repo/'target'/name,Path('reproduction')/name)
for name in ('run-croc-phase-clean-diagnostic.py','launch-croc-phase-clean-diagnostic.py',
             'croc-phase-build-manifest.json','go-toolchain-manifest.json'):
    retain(repo/'target'/name,Path('prepared-croc-phases')/name)
for name in ('instrumentation.patch','source-commit.txt','original-source.sha256'):
    retain(base/'tools/croc-phase-20261004'/name,Path('prepared-croc-phases')/name)
(cli.destination/'artifact-inventory.json').write_text(json.dumps(inventory,indent=2)+'\n')
print(json.dumps(dict(destination=str(cli.destination),retained=len(inventory))))
