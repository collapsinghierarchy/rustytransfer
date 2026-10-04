"""Report all measured comparisons; this does not decide production adoption."""
import argparse
import json
import statistics
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('cohort', type=Path)
args = parser.parse_args()
audit = json.loads((args.cohort / 'audit-report.json').read_text())
config = json.loads((args.cohort / 'config.json').read_text())
comparisons = []
for group in audit['groups']:
    index = next(i for i, item in enumerate(config['groups']) if item['name'] == group['name'])
    rows = {}
    for variant in group['variants']:
        path = args.cohort / f'{index:02d}-{group["name"]}' / variant / 'rows.jsonl'
        rows[variant] = {row['run_index']: row for row in map(json.loads, path.read_text().splitlines())
                         if row['role'] == 'sender' and not row['warmup']}
    candidate = group['variants']['c4-s4']
    for control_name in ('c1-s1', 'c1-s4'):
        if control_name not in group['variants']:
            continue
        control = group['variants'][control_name]
        rate_ratio = candidate['effective_mib_per_second']['median'] / control['effective_mib_per_second']['median']
        cpu_ratios = {role: candidate[role + '_cpu_seconds_per_gib']['median'] / control[role + '_cpu_seconds_per_gib']['median']
                      for role in ('sender', 'receiver')}
        assert rows[control_name].keys() == rows['c4-s4'].keys()
        paired_ratios = [rows['c4-s4'][trial]['effective_mib_per_second'] / row['effective_mib_per_second']
                         for trial, row in rows[control_name].items()]
        comparisons.append({'group': group['name'], 'series': group['series'],
            'control': control_name, 'candidate': 'c4-s4',
            'rate_change_percent': (rate_ratio - 1) * 100,
            'cpu_per_gib_change_percent': {role: (ratio - 1) * 100 for role, ratio in cpu_ratios.items()},
            'rss_change_percent': {role: (candidate[role + '_max_rss_kib']['median'] / control[role + '_max_rss_kib']['median'] - 1) * 100
                                   for role in ('sender', 'receiver')},
            'paired_rate_change_percent': [(ratio - 1) * 100 for ratio in paired_ratios],
            'paired_median_rate_change_percent': (statistics.median(paired_ratios) - 1) * 100,
            'paired_rate_wins': sum(ratio > 1 for ratio in paired_ratios),
            'paired_count': len(paired_ratios),
            'screen_threshold_met': rate_ratio >= 1.05 or any(ratio <= 0.90 for ratio in cpu_ratios.values()),
            'scope': 'Measured comparison only; confirmation, guard cases, startup, RSS, variance and correctness remain separate gates.'})
output = args.cohort / 'comparisons.json'
assert not output.exists()
output.write_text(json.dumps(comparisons, indent=2) + '\n')
print(json.dumps(comparisons, indent=2))
