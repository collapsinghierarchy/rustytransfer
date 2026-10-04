"""Recover only the interrupted final Croc trial and expired scoped lease."""
import json
import re
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'benchmarks'))
import run_oracle_transfer as runner

args = SimpleNamespace(ssh='ssh', scp='scp', ssh_key=Path('/home/wasilij/.ssh/id_ed25519_oracle'),
                       user='ubuntu', host='141.147.1.21')
root = Path('/home/wasilij/rustytransfer-bench/results/completion-croc-20261004')
metadata = r'''
import json,os
from pathlib import Path
base=Path('/home/ubuntu/rustytransfer-bench')
active=[]
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit(): continue
    try:
        exe=os.readlink(proc/'exe'); cwd=Path(os.readlink(proc/'cwd'))
    except OSError: continue
    if Path(exe).name=='croc-11.5.4' and 'rustytransfer-bench/' in exe:
        assert cwd.parent.parent==base
        assert cwd.parent.name.startswith('run-croc-final-') and cwd.parent.name.endswith('-512')
        assert cwd.name=='croc-oracle-to-wsl-512mib-5'
        active.append(dict(pid=int(proc.name),run_dir=str(cwd)))
leases=[]
for path in base.glob('rt-croc-*/restored.json'):
    data=json.loads(path.read_text())
    assert data['tag']==path.parent.name
    assert data['exact_input_chain_restored']
    leases.append(dict(root=str(path.parent),audit=data))
print(json.dumps(dict(active=active,leases=leases)))
'''
found=json.loads(runner.remote(args,'python3 -c '+shlex.quote(metadata)))
assert len(found['active']) <= 1
assert len(found['leases']) == 1
logs=root/'oracle-512/logs/croc-oracle-to-wsl-512mib-5'
logs.mkdir(parents=True,exist_ok=True)
for item in found['active']:
    run_dir=item['run_dir']
    runner.terminate_remote_process_group(args,run_dir+'/sender.pid',run_dir)
    text=runner.remote(args,'cat -- '+shlex.quote(run_dir+'/sender.log'))
    secrets=set(re.findall(r'rtoracle-512-5-[0-9a-f]{12}',text))
    for secret in secrets: text=text.replace(secret,'<redacted>')
    (logs/'sender.log').write_text(text+'\n')
    for path in logs.rglob('*.log'):
        content=path.read_text(errors='replace')
        content=re.sub(r'rtoracle-512-5-[0-9a-f]{12}','<redacted>',content)
        path.write_text(content)
    remote_redact=r'''
import re,sys
from pathlib import Path
path=Path(sys.argv[1]); text=path.read_text(errors='replace')
path.write_text(re.sub(r'rtoracle-512-5-[0-9a-f]{12}','<redacted>',text))
'''
    runner.remote(args,'python3 -c '+shlex.quote(remote_redact)+' '+shlex.quote(run_dir+'/sender.log'))
    for filename in ('sender.time.json','ssh-connection.txt'):
        text=runner.remote(args,'if test -f '+shlex.quote(run_dir+'/'+filename)+'; then cat -- '+shlex.quote(run_dir+'/'+filename)+'; fi')
        if text: (logs/filename).write_text(text+'\n')
lease=found['leases'][0]
(root/'firewall-lease-audit.json').write_text(json.dumps(lease['audit'],indent=2)+'\n')
lease_root=lease['root']
assert Path(lease_root).parent==Path('/home/ubuntu/rustytransfer-bench')
runner.remote(args,'rm -- '+' '.join(shlex.quote(lease_root+'/'+name) for name in ('pid.json','ready.json','restored.json'))+' && rmdir -- '+shlex.quote(lease_root))
report=dict(status='interrupted-incomplete-unscored',recorded_transfers=23,
            reason='Runner session disappeared before final Croc trial was recorded; no trial reconstructed or selectively substituted.',
            missing_trial=dict(size_mib=512,candidate='croc',run_index=5),
            oracle_endpoint_cleanup=found['active'],
            firewall_restored_exactly=lease['audit']['exact_input_chain_restored'],
            cleanup_note='Expired bounded lease restored the original INPUT chain; final Oracle Croc waiter terminated through validated process-group cleanup.')
(root/'interruption.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
