"""Independently validate completed no-debug cohorts and derive summary statistics."""
import argparse
import hashlib
import json
import math
import re
import statistics
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('root',type=Path)
args=p.parse_args()
root=args.root
manifest=json.loads((root/'manifest.json').read_text())
rows=[json.loads(line) for line in (root/'raw.jsonl').read_text().splitlines()]
sizes=manifest['sizes_mib']
n=manifest['measured_per_tool_size']
assert len(rows)==len(sizes)*2*(n+1)*2
errors=root/'errors.jsonl'
assert not errors.exists() or not errors.read_text().strip(), 'Retained errors require explicit resolution.'
summary=[]
for size in sizes:
    fixture=manifest['fixtures'][str(size)]
    for transport in ('rustytransfer','croc'):
        selected=[r for r in rows if r['size_bytes']==size*1048576 and r['transport']==transport]
        assert len(selected)==2*(n+1)
        sender=[r for r in selected if r['role']=='sender']
        receiver=[r for r in selected if r['role']=='receiver']
        assert len(sender)==len(receiver)==n+1
        assert sorted(r['run_index'] for r in sender)==list(range(n+1))
        for a,b in zip(sender,receiver):
            assert {k:v for k,v in a.items() if k!='role'}=={k:v for k,v in b.items() if k!='role'}
        for row in selected:
            assert row['success'] and row['path']==('direct-bulk-sampled' if transport=='rustytransfer' else 'direct')
            assert row['source_sha256']==row['received_sha256']==fixture['sha256']
            assert row['warmup']==(row['run_index']==0)
            assert row['debug_flags_present'] is False
            assert row['debug_environment_present'] is False
            assert row['app_metrics_enabled'] is False
            assert row['local_binary_sha256']==manifest['local_binary_sha256'][transport]
            assert row['remote_binary_sha256']==manifest['remote_binary_sha256'][transport]
            for name in ('wall_seconds','sender_process_seconds','receiver_process_seconds',
                         'effective_mib_per_second'):
                assert math.isfinite(row[name]) and row[name]>0
            assert abs(row['effective_mib_per_second']-size/row['wall_seconds'])<1e-9
            assert row['sender_process_seconds']<=row['wall_seconds']+.03
            assert row['receiver_process_seconds']<=row['wall_seconds']+.03
            evidence=row['path_evidence']
            assert evidence['verified'] is True
            if transport=='croc':
                assert evidence['kind']=='live-tcp-process-sockets'
                for name,portfield,peer in (
                    ('oracle_socket_tuples','local_port',evidence['peer_addresses']['oracle_observed_wsl']),
                    ('wsl_socket_tuples','remote_port',evidence['peer_addresses']['wsl_observed_oracle'])):
                    tuples=evidence[name]
                    assert len(tuples)==4
                    assert {r[portfield] for r in tuples}=={9010,9011,9012,9013}
                    assert all(r['state']=='ESTABLISHED' and r['remote_ip']==peer for r in tuples)
                assert evidence['oracle_control_socket']['local_port']==9009
                assert evidence['wsl_control_socket']['remote_port']==9009
            else:
                assert evidence['kind']=='normal-cli-initial-path-plus-sampled-direct-udp'
                assert set(evidence['initial_selected_paths'])=={'sender','receiver'}
                for role in ('sender','receiver'):
                    captures=evidence['captures'][role]
                    assert len(captures)==2
                    for capture in captures:
                        result=capture['result']
                        assert result['complete'] and result['observed_packets']>=2
                        assert result['local_port']==capture['port']
                        port=capture['port']
                        peer=result['peer']
                        if role=='sender':
                            payload=[packet for packet in result['packets'] if packet['source_port']==port and packet['destination_ip']==peer]
                        else:
                            payload=[packet for packet in result['packets'] if packet['source_ip']==peer and packet['destination_port']==port]
                        assert len(payload)>=2
                        assert all(packet['ip_bytes']>=1000 for packet in result['packets'])
        measured=[r for r in sender if not r['warmup']]
        rates=[r['effective_mib_per_second'] for r in measured]
        seconds=[r['wall_seconds'] for r in measured]
        summary.append(dict(size_mib=size,transport=transport,measured_runs=n,
            mean_wall_seconds=statistics.mean(seconds),mean_mib_per_second=statistics.mean(rates),
            median_mib_per_second=statistics.median(rates),min_mib_per_second=min(rates),
            max_mib_per_second=max(rates),mad_mib_per_second=statistics.median(abs(r-statistics.median(rates)) for r in rates)))
trials=list((root/'trials').iterdir())
assert len(trials)==len(rows)//2
lease_count=0
for trial in trials:
    snapshots=json.loads((trial/'socket-snapshots.json').read_text())
    for role in ('sender','receiver'):
        assert snapshots[role], (trial,role)
        for record in snapshots[role]:
            proc=record['process']
            assert proc and not proc['debug_flags_present'] and not proc['debug_env_present']
            assert not any(word in proc['argv_redacted'] for word in ('--debug','--verbose','--trace','-v'))
    for role in ('sender','receiver'):
        text=(trial/(role+'.log')).read_text(errors='replace')
        assert not re.search(r'rt1:\S+|CROC_BENCH_PHASE_PROFILE|RUSTYTRANSFER_METRICS_JSONL|RUSTYTRANSFER_BENCH_',text)
        if '-rustytransfer-' in trial.name:
            assert re.search(r'Selected\s+Iroh\s+data\s+path:\s*(direct|relay)',text,re.I)
    lease=trial/'firewall-lease.json'
    if lease.is_file():
        lease_count+=1
        record=json.loads(lease.read_text())
        assert record['exact_input_chain_restored']
        assert record['before_sha256']==record['after_sha256']=='8eabe87e4569e251c3147f193c0880b3c52ae3eaf5d007db8dda219ad79a53a5'
assert lease_count==len(sizes)*(n+1)
report=dict(accepted_transfers=len(trials),canonical_endpoint_rows=len(rows),
    measured_transfers=len(sizes)*2*n,unscored_warmups=len(sizes)*2,
    live_no_debug_checks_passed=True,received_hashes_verified=True,
    leases_restored_exactly=lease_count,summary=summary,
    route_limitations=manifest['route_limitations'])
(root/'audit-report.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
