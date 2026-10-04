"""Check evidence byte preservation and the documentation commit scope."""
import hashlib
import json
import subprocess
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
prefix = 'benchmarks/results/oracle-20261004-completion/'
records = json.loads((repo / prefix / 'artifact-inventory.json').read_text())
expected = {prefix+item['path']:item for item in records if item['retention']=='versioned'}
raw = subprocess.check_output(['git', 'ls-files', '--stage', '-z', '--', prefix], cwd=repo)
staged = {}
for entry in raw.split(b'\0'):
    if entry:
        metadata, name = entry.split(b'\t', 1)
        mode, blob, stage = metadata.decode().split()
        assert stage == '0'
        staged[name.decode()] = blob
assert set(staged) == set(expected) | {prefix+'artifact-inventory.json'}
for name, blob in staged.items():
    data = (repo / name).read_bytes()
    if name in expected:
        record = expected[name]
        assert len(data)==record['bytes'] and hashlib.sha256(data).hexdigest()==record['sha256'], name
    assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==blob, name
changed = subprocess.check_output(['git', 'diff', '--cached', '--name-only', '-z'], cwd=repo).decode().split('\0')
allowed = {'benchmarks/README.md', 'docs/direct-transfer-efficiency-report.md'}
assert all(name.startswith(prefix) or name in allowed for name in changed if name)
print('Verified staged evidence bytes:', len(staged), 'files; no unrelated files staged')
