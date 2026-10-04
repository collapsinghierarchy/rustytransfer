"""Check exact staged artifact bytes and restrict the evidence commit scope."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('root',type=Path)
args=p.parse_args()
repo=Path(__file__).resolve().parents[1]
root=args.root.resolve()
relative=root.relative_to(repo.resolve()).as_posix()
staged=subprocess.check_output(['git','diff','--cached','--name-only'],cwd=repo,text=True).splitlines()
assert staged
assert all(path.startswith(relative+'/') or path=='docs/direct-transfer-efficiency-report.md' for path in staged),staged
inventory=json.loads((root/'artifact-inventory.json').read_text())
entries={entry['path']:entry for entry in inventory}
checked=0
for path in staged:
    if not path.startswith(relative+'/'):
        continue
    suffix=path[len(relative)+1:]
    blob=subprocess.check_output(['git','show',':'+path],cwd=repo)
    if suffix=='artifact-inventory.json':
        assert blob==(root/suffix).read_bytes()
        continue
    expected=entries[suffix]
    assert len(blob)==expected['bytes'] and hashlib.sha256(blob).hexdigest()==expected['sha256'],path
    checked+=1
print(json.dumps(dict(staged_artifacts_verified=checked,staged_paths=len(staged),commit_scope_verified=True)))
