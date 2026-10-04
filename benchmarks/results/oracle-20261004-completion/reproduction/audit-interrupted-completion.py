"""Audit recorded successful pairs without scoring an incomplete cohort."""
import hashlib
import importlib.util
import ipaddress
import json
import math
import re
import sys
from pathlib import Path

repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo/'benchmarks'))
import run_oracle_transfer as runner
spec=importlib.util.spec_from_file_location('shared',Path(__file__).with_name('collect-croc-1g-final.py'))
shared=importlib.util.module_from_spec(spec); spec.loader.exec_module(shared)
base=Path('/home/wasilij/rustytransfer-bench')
root=base/'results/completion-croc-20261004'
rusty=json.loads((base/'build-completion-v1-20261004/manifest.json').read_text())
croc=json.loads((base/'tools/croc-11.5.4/manifest.json').read_text())
firewall=json.loads((root/'firewall-lease-audit.json').read_text())
assert firewall['exact_input_chain_restored'] and firewall['before_sha256']==firewall['after_sha256']
count=0
for size in (64,512):
    rows=[json.loads(line) for line in (root/f'oracle-{size}/oracle-{size}.jsonl').read_text().splitlines()]
    assert len(rows)==(24 if size==64 else 22)
    pairs={}
    fixture_digest=runner.sha256_file(base/f'input-{size}.bin')
    for row in rows:
        assert row['success'] and row['size_bytes']==size*1048576
        if row['transport']=='iroh':
            assert row['bytes_transferred']==size*1048576
        assert row['source_sha256']==row['received_sha256']==fixture_digest
        assert math.isclose(row['effective_mib_per_second'],size/row['wall_seconds'])
        assert row['local_endpoint_working_directory']==str(base)
        assert row['profile_mode']=='standard' and row['source_staging']=='pre-staged'
        assert row['direction']=='oracle-to-wsl' and row['path']=='direct'
        if row['transport']=='iroh':
            assert row['build_id']=='completion-v1-default-croc-comparison'
            assert runner.verified_direct_evidence(row) and row['direct_route_verified_both']
            assert not row['completion_profile_enabled'] and not row['explicit_connection_close']
            expected=(rusty['arm_binary_sha256'],rusty['x86_binary_sha256'])
        else:
            assert row['transport']=='croc' and row['build_id']=='croc-11.5.4'
            assert shared.direct_croc_evidence(row,'141.147.1.21',firewall) is None
            remote=next(asset for name,asset in croc['assets'].items() if 'Linux-ARM64' in name)
            local=next(asset for name,asset in croc['assets'].items() if 'Linux-64bit' in name)
            expected=(remote['binary_sha256'],local['binary_sha256'])
        assert (row['sender_binary_sha256'],row['receiver_binary_sha256'])==expected
        pairs.setdefault((row['transport'],row['run_index'],row['warmup']),[]).append(row)
        count+=1
    assert len(pairs)==(12 if size==64 else 11)
    expected_keys={(kind,index,index==0) for kind in ('iroh','croc') for index in range(6)}
    if size==512: expected_keys.remove(('croc',5,False))
    assert set(pairs)==expected_keys
    for endpoints in pairs.values():
        assert len(endpoints)==2 and {row['role'] for row in endpoints}=={'sender','receiver'}
        for key in ('wall_seconds','effective_mib_per_second','source_sha256','received_sha256'):
            assert endpoints[0][key]==endpoints[1][key]
    peer=str(ipaddress.IPv4Network(firewall['cidr'],strict=True).network_address)
    for (kind,index,warmup),endpoints in pairs.items():
        if kind!='croc': continue
        tag='warmup' if warmup else str(index)
        logs=root/f'oracle-{size}/logs/croc-oracle-to-wsl-{size}mib-{tag}'
        sender=(logs/'sender.log').read_text()
        receiver=(logs/'receiver.log').read_text()
        actual=runner.croc_direct_tcp_evidence(sender,receiver,
            f'{peer} 0 141.147.1.21 22','141.147.1.21',peer)
        assert all(row['path_evidence']==actual for row in endpoints)
for path in root.rglob('*.log'):
    assert not shared.SECRET.search(path.read_text(errors='replace')),path
report=dict(status='incomplete-cohort-unscored',recorded_successful_transfers=count//2,
            endpoint_rows=count,recorded_pairs_have_full_hashes_and_both_endpoint_direct_evidence=True,
            no_performance_acceptance_or_final_medians=True,
            missing=dict(size_mib=512,transport='croc',run_index=5),
            redacted_logs=True,firewall_restored_exactly=True,
            collector_attempts=[dict(status='failed',reason="First collector assumed Croc emitted bytes_transferred and raised KeyError; no rows changed."),
                                dict(status='passed',correction='Require endpoint byte count for Rustytransfer; Croc size was checked externally by the runner, with matching full-file SHA-256 retained.')])
(root/'partial-audit.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
