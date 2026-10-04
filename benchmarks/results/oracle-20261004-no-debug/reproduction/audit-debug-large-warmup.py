"""Audit the two unscored 4 GiB warmups retained when user superseded debug runs."""
import json
import sys
from pathlib import Path

repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo/'benchmarks'))
import run_oracle_transfer as runner

base=Path('/home/wasilij/rustytransfer-bench')
root=base/'results/large-phase-20261004'
fixture=json.loads((root/'fixture-manifest.json').read_text())['4096']
source=Path(fixture['local_path'])
assert source.stat().st_size==fixture['size_bytes']==4294967296
assert runner.sha256_file(source)==fixture['sha256']
rows=[json.loads(line) for line in (root/'oracle-4096/oracle-4096.jsonl').read_text().splitlines()]
assert len(rows)==4
rusty=json.loads((root/'rusty-manifest.json').read_text())
croc=json.loads((root/'croc-manifest.json').read_text())
local=next(a for n,a in croc['assets'].items() if 'Linux-64bit' in n)
arm=next(a for n,a in croc['assets'].items() if 'Linux-ARM64' in n)
for kind in ('iroh','croc'):
    pair=[r for r in rows if r['transport']==kind]
    assert len(pair)==2 and {r['role'] for r in pair}=={'sender','receiver'}
    for row in pair:
        assert row['warmup'] is True and row['run_index']==0 and row['success']
        assert row['size_bytes']==fixture['size_bytes']
        assert row['source_sha256']==row['received_sha256']==fixture['sha256']
        assert row['source_staging']=='pre-staged' and row['direct_route_verified_both']
        assert row['sender_binary_sha256']==(rusty['arm_binary_sha256'] if kind=='iroh' else arm['binary_sha256'])
        assert row['receiver_binary_sha256']==(rusty['x86_binary_sha256'] if kind=='iroh' else local['binary_sha256'])
        if kind=='iroh':
            assert row['bytes_transferred']==fixture['size_bytes'] and runner.verified_direct_evidence(row)
    if kind=='croc':
        lease=json.loads((root/'leases/oracle-4096-0.json').read_text())
        assert lease['exact_input_chain_restored'] and lease['before_sha256']==lease['after_sha256']
        peer=lease['cidr'].removesuffix('/32')
        logs=root/'oracle-4096/logs/croc-oracle-to-wsl-4096mib-warmup'
        proof=runner.croc_direct_tcp_evidence((logs/'sender.log').read_text(),(logs/'receiver.log').read_text(),
                                            f'{peer} 0 141.147.1.21 22','141.147.1.21',peer)
        assert all(r['path_evidence']==proof for r in pair)
report=dict(accepted_scored_transfers=0,unscored_warmups=2,full_hashes_and_routes_verified=True,
            canonical_endpoint_rows=4,reason='User superseded remaining debug runs with no-debug final comparison.',
            overall_retained_transfers=26,complete_phase_cohort_transfers=24)
(root/'unscored-4096-warmup-audit.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
