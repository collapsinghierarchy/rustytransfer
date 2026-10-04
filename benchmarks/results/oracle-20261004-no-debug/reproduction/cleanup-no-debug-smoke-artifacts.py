"""Verify then remove only generated outputs/transients from failed smoke runs."""
import hashlib
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo/'benchmarks'))
import run_oracle_transfer as common
base=Path('/home/wasilij/rustytransfer-bench')
assert (base/'results/no-debug-final-20261004/audit-report.json').is_file()
cleanup=json.loads((base/'results/no-debug-final-cleanup-20261004.json').read_text())
assert cleanup['original_input_chain_restored']
assert all(not value for role in ('local','oracle') for value in cleanup[role].values())
removed=[]
known=[]
expected='30671134dac585f880ff30d0a898cba69535339855bd938ef68585a8d142c1de'
for name in ('no-debug-smoke-20261004-r2','no-debug-smoke-20261004-r3','no-debug-smoke-20261004-r4'):
    root=base/'results'/name
    for trial in (root/'trials').iterdir():
        known.append(trial.name)
        output=trial/'out/input-512.bin'
        if not output.exists():
            continue
        assert output.resolve().is_relative_to(root.resolve()) and output.parent.name=='out'
        assert output.stat().st_size==536870912
        digest=common.sha256_file(output)
        assert digest==expected
        record=dict(path=str(output),bytes=output.stat().st_size,sha256=digest,
                    classification='Unscored smoke output; full file verified after clean cohort ended.')
        (trial/'retained-output-audit.json').write_text(json.dumps(record,indent=2)+'\n')
        output.unlink()
        output.parent.rmdir()
        removed.append(record)
remote_script=r'''
import json,re,sys
from pathlib import Path
root=Path('/home/ubuntu/rustytransfer-bench/no-debug-cohort').resolve()
known=set(json.loads(sys.argv[1]))
removed=[]
for parent in root.iterdir():
    if not parent.is_dir() or not re.fullmatch(r'cohort-[a-f0-9]{10}',parent.name):
        continue
    assert parent.resolve().parent==root
    for trial in list(parent.iterdir()):
        if trial.name not in known:
            continue
        assert trial.is_dir() and trial.resolve().parent==parent.resolve()
        paths=list(trial.iterdir())
        allowed={'sender.pid','sender.time.json','udp-sample-3s-sender.json','udp-sample-10s-sender.json'}
        assert all(path.is_file() and path.name in allowed and path.resolve().parent==trial.resolve() for path in paths)
        records={path.name:path.read_text() for path in paths}
        for path in paths:
            path.unlink()
        trial.rmdir()
        removed.append({'trial':trial.name,'retained_metadata':records})
    if not list(parent.iterdir()):
        parent.rmdir()
print(json.dumps(removed))
'''
args=SimpleNamespace(host='141.147.1.21',user='ubuntu',ssh='ssh',scp='scp',ssh_key=Path('/home/wasilij/.ssh/id_ed25519_oracle'))
remote=json.loads(common.remote(args,'python3 -c '+shlex.quote(remote_script)+' '+shlex.quote(json.dumps(known))))
record=dict(local_received_outputs=removed,remote_generated_metadata=remote,
            source_fixtures_preserved=True,recursive_delete_used=False)
(base/'results/no-debug-smoke-artifact-cleanup-20261004.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(dict(local_outputs_removed=len(removed),remote_trials_cleaned=len(remote))))
