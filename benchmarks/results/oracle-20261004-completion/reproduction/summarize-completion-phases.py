"""Compact phase tables and an additive decomposition of one actual run."""
import json
import statistics
from pathlib import Path

base = Path('/home/wasilij/rustytransfer-bench/results')
screen = base / 'completion-screen-20261004'
group = json.loads((screen / 'completion-analysis.json').read_text())['groups'][0]
for name, stats in group['variants'].items():
    print(name, 'rate', stats['rate'], 'receiver commit',
          stats['endpoint_profiles']['receiver']['receiver_commit_seconds'],
          'receiver endpoint close', stats['endpoint_profiles']['receiver']['endpoint_close_seconds'])
    path = screen / '00-s1-completion-oracle-to-wsl-512' / name / 'rows.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    senders = [row for row in rows if not row['warmup'] and row['role']=='sender']
    typical = sorted(senders, key=lambda row:row['wall_seconds'])[len(senders)//2]
    phases = {key:typical[key] for key in ('handshake_seconds','payload_seconds','shutdown_seconds')}
    phases['remaining_outer_seconds'] = typical['wall_seconds']-sum(phases.values())
    print('Actual median-wall sample', typical['run_index'], 'wall',typical['wall_seconds'], phases)
    for role in ('sender','receiver'):
        row = next(row for row in rows if row['run_index']==typical['run_index'] and row['role']==role)
        print(role, row['completion_profile'])
root = base / 'completion-croc-retry-20261004'
if (root / 'audit-report.json').exists():
    for summary in json.loads((root / 'audit-report.json').read_text())['comparisons']:
        print(summary['size_mib'],'Croc full mean',summary['croc_reference']['mean_full_mib_per_second'])
        print('Rustytransfer phases',summary['rustytransfer_phases'])
    rows=[json.loads(line) for line in (root/'oracle-512/oracle-512.jsonl').read_text().splitlines()]
    scored=[row for row in rows if row['transport']=='iroh' and row['role']=='sender' and not row['warmup']]
    ordered=sorted(scored,key=lambda row:row['wall_seconds'])
    for row in (ordered[len(ordered)//2],ordered[-1]):
        print('Fresh comparison actual sample',row['run_index'],
              {key:row[key] for key in ('wall_seconds','handshake_seconds','payload_seconds','shutdown_seconds')},
              'outer remainder',row['wall_seconds']-sum(row[key] for key in ('handshake_seconds','payload_seconds','shutdown_seconds')))
