"""Audit lifecycle fields and summarize measured spans without adding medians."""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'benchmarks'))
import run_oracle_transfer as runner

parser = argparse.ArgumentParser()
parser.add_argument('root', type=Path)
args = parser.parse_args()
root = args.root
config = json.loads((root / 'config.json').read_text())
manifest = json.loads((root / 'manifest.json').read_text())
assert manifest['status'] == 'completed'
def robust(values):
    mid = statistics.median(values)
    return dict(median=mid, min=min(values), max=max(values),
                mad=statistics.median(abs(x-mid) for x in values), count=len(values))
groups = []
for index, group in enumerate(config['groups']):
    summary = dict(name=group['name'], variants={})
    all_rows = {}
    for variant in group['variants']:
        path = root / f'{index:02d}-{group["name"]}' / variant / 'rows.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        spec = config['variants'][variant]
        assert len(rows) == 2 * (group['runs'] + 1)
        for row in rows:
            assert row['completion_profile_enabled'] == spec['completion_profile']
            assert row['explicit_connection_close'] == spec['explicit_connection_close']
            if spec['completion_profile']:
                assert runner.completion_profile_rejection_reason(row) is None, row
            else:
                assert 'completion_profile' not in row
            assert row['endpoint_process_seconds'] == row[row['role'] + '_process_seconds']
            assert row['endpoint_process_seconds'] <= row['wall_seconds'] + .02
            timing = row['benchmark_timing']
            assert 0 <= timing['ready_observed_seconds'] <= timing['receiver_launch_seconds'] <= timing['pair_exit_observed_seconds']
            assert math.isclose(timing['pair_exit_observed_seconds'], row['wall_seconds'])
        scored = [row for row in rows if not row['warmup'] and row['role'] == 'sender']
        assert len(scored) == group['runs']
        all_rows[variant] = scored
        out = dict(rate=robust([row['effective_mib_per_second'] for row in scored]),
                   wall=robust([row['wall_seconds'] for row in scored]),
                   sender_cpu_gib=robust([row['sender_cpu_seconds'] / (row['size_bytes']/2**30) for row in scored]),
                   receiver_cpu_gib=robust([row['receiver_cpu_seconds'] / (row['size_bytes']/2**30) for row in scored]),
                   endpoint_profiles={})
        for role in ('sender', 'receiver'):
            endpoints = [row for row in rows if not row['warmup'] and row['role'] == role]
            stats = {'process_seconds':robust([row['endpoint_process_seconds'] for row in endpoints])}
            if spec['completion_profile']:
                fields = set.intersection(*(set(row['completion_profile']) for row in endpoints))
                stats.update({field:robust([row['completion_profile'][field] for row in endpoints])
                              for field in sorted(fields) if field.endswith('_seconds')})
                stats['post_metric_process_seconds'] = robust([
                    row['endpoint_process_seconds']-row['completion_profile']['application_wall_seconds']
                    for row in endpoints])
            out['endpoint_profiles'][role] = stats
        summary['variants'][variant] = out
    if 'baseline' in all_rows and 'explicit-close' in all_rows:
        baseline = summary['variants']['baseline']
        candidate = summary['variants']['explicit-close']
        summary['changes_percent'] = {key:(candidate[key]['median']/baseline[key]['median']-1)*100
                                      for key in ('rate', 'sender_cpu_gib', 'receiver_cpu_gib')}
        b = {row['run_index']:row for row in all_rows['baseline']}
        pairs = [row['effective_mib_per_second']/b[row['run_index']]['effective_mib_per_second']
                 for row in all_rows['explicit-close']]
        summary['paired_rate_ratios'] = robust(pairs)
        summary['paired_wins'] = sum(ratio>1 for ratio in pairs)
    groups.append(summary)
report = dict(groups=groups, limits='Spans are elapsed time, not CPU attribution; enclosing spans and endpoint lifetimes overlap; medians must not be summed. Croc exact payload/commit phases remain unavailable.')
(root / 'completion-analysis.json').write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps(report, indent=2))
