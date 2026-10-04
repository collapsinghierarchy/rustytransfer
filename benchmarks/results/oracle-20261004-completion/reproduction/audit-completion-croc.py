"""Audit the fresh final 64/512 MiB direct comparison with pinned Croc."""
import argparse
import hashlib
import importlib.util
import ipaddress
import json
import math
import re
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'benchmarks'))
import run_oracle_transfer as runner
spec = importlib.util.spec_from_file_location('shared_audit', Path(__file__).with_name('collect-croc-1g-final.py'))
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)

p = argparse.ArgumentParser()
p.add_argument('root', type=Path)
p.add_argument('--rusty-manifest', required=True, type=Path)
p.add_argument('--croc-manifest', required=True, type=Path)
cli = p.parse_args()
rusty = json.loads(cli.rusty_manifest.read_text())
croc = json.loads(cli.croc_manifest.read_text())
firewall = json.loads((cli.root / 'firewall-lease-audit.json').read_text())
peer = str(ipaddress.IPv4Network(firewall['cidr'], strict=True).network_address)
assert firewall['cidr'].endswith('/32')
assert firewall['exact_input_chain_restored']
assert firewall['before_sha256'] == firewall['after_sha256']
order = json.loads((cli.root / 'run-order.json').read_text())
manifest = json.loads((cli.root / 'comparison-manifest.json').read_text())
for label, filename in [('runner', 'run_oracle_transfer.py'), ('helper', 'run-completion-croc.py'), ('firewall_helper', 'croc_firewall_lease.py')]:
    assert runner.sha256_file(cli.root / filename) == manifest[label + '_sha256']
comparisons = []
route_excerpts = []
for size in (64, 512):
    path = cli.root / f'oracle-{size}/oracle-{size}.jsonl'
    rows = shared.load_jsonl(path)
    assert len(rows) == 24
    for row in rows:
        assert row['local_endpoint_working_directory'] == '/home/wasilij/rustytransfer-bench'
        assert row['endpoint_process_seconds'] == row[row['role'] + '_process_seconds']
        assert row['endpoint_process_seconds'] <= row['wall_seconds'] + .02
        timing = row['benchmark_timing']
        assert 0 <= timing['ready_observed_seconds'] <= timing['receiver_launch_seconds'] <= timing['pair_exit_observed_seconds']
        assert math.isclose(timing['pair_exit_observed_seconds'], row['wall_seconds'])
        if row['transport'] == 'iroh':
            assert row['completion_profile_enabled'] is False
            assert row['explicit_connection_close'] is False
            assert 'completion_profile' not in row
    fixture_path = Path('/home/wasilij/rustytransfer-bench') / f'input-{size}.bin'
    digest = runner.sha256_file(fixture_path)
    assert fixture_path.stat().st_size == size * 1048576
    shared.SIZE_BYTES = size * 1048576
    trials = shared.validate_rows(rows, '141.147.1.21', firewall, rusty, croc, {'sha256': digest},
                                  rusty_build_id='completion-v1-default-croc-comparison')
    croc_scored=[row for (kind, _, warmup), row in trials.items() if kind=='croc' and not warmup]
    croc_mean=statistics.mean(row['effective_mib_per_second'] for row in croc_scored)
    summary=dict(size_mib=size, comparison_policy='Rustytransfer phases individually versus Croc arithmetic mean full-transfer MiB/s; no overall Rustytransfer/Croc throughput comparison.',
                 croc_reference=dict(mean_full_mib_per_second=croc_mean,
                                     full_mib_per_second=shared.robust([row['effective_mib_per_second'] for row in croc_scored])),
                 resources={}, rustytransfer_phases={})
    for variant in ('iroh', 'croc'):
        measured = [row for (kind, _, warmup), row in trials.items() if kind == variant and not warmup]
        summary['resources'][variant]={}
        for role in ('sender', 'receiver'):
            summary['resources'][variant][role + '_cpu_seconds_per_gib'] = shared.robust([row[role + '_cpu_seconds'] / (size / 1024) for row in measured])
            summary['resources'][variant][role + '_max_rss_kib'] = shared.robust([row[role + '_max_rss_kib'] for row in measured])
        summary['resources'][variant]['ready_observed_seconds'] = shared.robust([row['benchmark_timing']['ready_observed_seconds'] for row in measured])
    for role in ('sender','receiver'):
        endpoints=[row for row in rows if row['transport']=='iroh' and row['role']==role and not row['warmup']]
        phases={}
        for field in ('handshake_seconds','payload_seconds','shutdown_seconds','remaining_outer_seconds'):
            seconds=[row[field] if field!='remaining_outer_seconds' else row['wall_seconds']-sum(row[key] for key in ('handshake_seconds','payload_seconds','shutdown_seconds')) for row in endpoints]
            assert all(value>0 for value in seconds)
            rates=[size/value for value in seconds]
            mean_rate=statistics.mean(rates)
            phases[field]=dict(seconds=shared.robust(seconds),mean_seconds=statistics.mean(seconds),
                               mean_normalized_mib_per_second=mean_rate,
                               normalized_mib_per_second=shared.robust(rates),
                               ratio_to_croc_full_mean=mean_rate/croc_mean,
                               limitation='Only payload is a data-transfer window. Other rates normalize the whole file size by an overhead duration; they are comparison indices, not actual wire throughput.')
        summary['rustytransfer_phases'][role]=phases
    comparisons.append(summary)
    for (kind, index, warmup), row in trials.items():
        if kind != 'croc':
            continue
        tag = 'warmup' if warmup else str(index)
        logs = cli.root / f'oracle-{size}/logs/croc-oracle-to-wsl-{size}mib-{tag}'
        sender = (logs / 'sender.log').read_text()
        receiver = (logs / 'receiver.log').read_text()
        assert not shared.SECRET.search(sender) and not shared.SECRET.search(receiver)
        actual = runner.croc_direct_tcp_evidence(sender, receiver, f'{peer} 0 141.147.1.21 22', '141.147.1.21', peer)
        assert actual == row['path_evidence']
        endpoints = {}
        for role, content in (('sender', sender), ('receiver', receiver)):
            endpoints[role] = {'redacted_full_log_sha256': runner.sha256_file(logs / f'{role}.log'), 'tcp_route_lines': [line for line in content.splitlines() if re.search(r'starting TCP server|client\s+.*\s+connected|connected to', line)]}
        assert runner.croc_direct_tcp_evidence('\n'.join(endpoints['sender']['tcp_route_lines']), '\n'.join(endpoints['receiver']['tcp_route_lines']), f'{peer} 0 141.147.1.21 22', '141.147.1.21', peer) == actual
        route_excerpts.append({'size_mib': size, 'run_index': index, 'warmup': warmup, 'endpoints': endpoints})
    expected = [('rustytransfer', 0, True), ('croc', 0, True)]
    for index in range(1, 6):
        expected.extend((name, index, False) for name in (('rustytransfer', 'croc') if index % 2 else ('croc', 'rustytransfer')))
    actual_order = [(entry['candidate'], entry['run_index'], entry['warmup']) for entry in order if entry['size_mib'] == size]
    assert actual_order == expected
report = {'canonical_endpoint_rows': 48, 'accepted_transfers': 24, 'all_routes_verified_both': True, 'full_hashes_verified': True, 'all_valid_slow_samples_retained': True, 'firewall_restored_exactly': True, 'comparisons': comparisons, 'rusty_manifest_sha256': runner.sha256_file(cli.rusty_manifest), 'croc_manifest_sha256': runner.sha256_file(cli.croc_manifest)}
(cli.root / 'audit-report.json').write_text(json.dumps(report, indent=2) + '\n')
(cli.root / 'croc-route-evidence.json').write_text(json.dumps(route_excerpts, indent=2) + '\n')
for summary in comparisons:
    print(summary['size_mib'], 'MiB: Croc full mean',summary['croc_reference']['mean_full_mib_per_second'])
    for phase,stats in summary['rustytransfer_phases']['sender'].items():
        print(phase,'mean seconds',stats['mean_seconds'],'mean normalized MiB/s',stats['mean_normalized_mib_per_second'],'Croc-reference ratio',stats['ratio_to_croc_full_mean'])
