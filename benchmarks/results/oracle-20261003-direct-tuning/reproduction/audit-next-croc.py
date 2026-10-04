"""Audit the fresh final 64/512 MiB direct comparison with pinned Croc."""
import argparse
import hashlib
import importlib.util
import ipaddress
import json
import math
import re
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
for label, filename in [('runner', 'run_oracle_transfer.py'), ('helper', 'run-croc-reverse-final.py'), ('firewall_helper', 'croc_firewall_lease.py')]:
    assert runner.sha256_file(cli.root / filename) == manifest[label + '_sha256']
comparisons = []
route_excerpts = []
for size in (64, 512):
    path = cli.root / f'oracle-{size}/oracle-{size}.jsonl'
    rows = shared.load_jsonl(path)
    assert len(rows) == 24
    fixture_path = Path('/home/wasilij/rustytransfer-bench') / f'input-{size}.bin'
    digest = runner.sha256_file(fixture_path)
    assert fixture_path.stat().st_size == size * 1048576
    shared.SIZE_BYTES = size * 1048576
    trials = shared.validate_rows(rows, '141.147.1.21', firewall, rusty, croc, {'sha256': digest})
    summary = shared.summarize(trials)
    summary['size_mib'] = size
    for variant in ('iroh', 'croc'):
        measured = [row for (kind, _, warmup), row in trials.items() if kind == variant and not warmup]
        for role in ('sender', 'receiver'):
            summary['variants'][variant][role + '_cpu_seconds_per_gib'] = shared.robust([row[role + '_cpu_seconds'] / (size / 1024) for row in measured])
    summary['croc_rate_advantage_percent'] = (summary['variants']['croc']['effective_mib_per_second']['median'] / summary['variants']['iroh']['effective_mib_per_second']['median'] - 1) * 100
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
    print(summary['size_mib'], 'MiB: Rustytransfer/Croc rate', [summary['variants'][kind]['effective_mib_per_second']['median'] for kind in ('iroh', 'croc')], 'Croc advantage %', summary['croc_rate_advantage_percent'])
