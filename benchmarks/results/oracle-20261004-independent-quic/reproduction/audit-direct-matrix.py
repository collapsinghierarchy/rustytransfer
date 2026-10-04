"""Independently audit strict routes, hashes, provenance, pairing and summaries."""
import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'benchmarks'))
import run_oracle_transfer as runner
from summarize import payload_profile_rejection_reason, connection_profile_rejection_reason

def robust(values):
    med = statistics.median(values)
    return {'median': med, 'min': min(values), 'max': max(values), 'mad': statistics.median(abs(x-med) for x in values), 'count': len(values)}

def audit_connections(row, expected_count):
    assert row['parallel_connections'] == expected_count
    connections = row['connection_evidence']
    assert len(connections) == expected_count
    assert [item['connection_index'] for item in connections] == list(range(expected_count))
    stable_ids = [item['stable_id'] for item in connections]
    assert all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in stable_ids)
    assert len(set(stable_ids)) == expected_count
    assert len({item['local_endpoint_id'] for item in connections}) == 1
    assert len({item['remote_endpoint_id'] for item in connections}) == 1
    for item in connections:
        assert item['local_endpoint_id'] and item['remote_endpoint_id']
        assert item['local_endpoint_id'] != item['remote_endpoint_id']
        assert item['path'] == item['path_start'] == item['path_end'] == 'direct'
        evidence = item['path_evidence']
        assert evidence['classification'] == 'direct' and evidence['verified'] is True
        for field in ('lagged', 'missing_path_stats', 'relay_selected'):
            assert evidence[field] is False
        assert evidence['relay_stream_tx'] == evidence['relay_stream_rx'] == 0
        for field in ('direct_stream_tx', 'direct_stream_rx'):
            assert isinstance(evidence[field], int) and not isinstance(evidence[field], bool) and evidence[field] >= 0
        assert evidence['direct_stream_tx'] + evidence['direct_stream_rx'] > 0
    assert row['path_evidence'] == connections[0]['path_evidence']

def main():
    p = argparse.ArgumentParser()
    p.add_argument('root', type=Path)
    p.add_argument('--completed-groups-only', action='store_true')
    args = p.parse_args()
    root = args.root
    config = json.loads((root / 'config.json').read_text())
    manifest = json.loads((root / 'manifest.json').read_text())
    assert manifest['status'] == 'completed' or (args.completed_groups_only and manifest['status'] == 'failed-with-diagnostics-retained')
    assert runner.sha256_file(root / 'run_oracle_transfer.py') == manifest['runner_sha256']
    assert runner.sha256_file(root / 'run-direct-matrix.py') == manifest['helper_sha256']
    all_summaries = []
    total = 0
    unaudited_groups = []
    for index, group in enumerate(config['groups']):
        scheduled = [entry for entry in manifest['schedule'] if entry['group'] == group['name']]
        complete = len(scheduled) == len(group['variants']) * (group.get('runs', 5) + 1) and all(entry['status'] == 'valid' for entry in scheduled)
        if args.completed_groups_only and not complete:
            unaudited_groups.append({'name': group['name'], 'retained_schedule': scheduled, 'reason': 'Incomplete group retained as diagnostics; no scored summary or route acceptance for this group.'})
            continue
        assert complete
        group_summary = {'name': group['name'], 'direction': group['direction'], 'size_mib': group['size_mib'], 'series': group.get('series', 1), 'variants': {}}
        actual_staging = 'pre-staged' if group['direction'] == 'oracle-to-wsl' and group['size_mib'] in (64, 512, 1024, 2048) else 'per-trial'
        group_summary['actual_source_staging'] = actual_staging
        size = int(group['size_mib'] * 1024 * 1024)
        fixture = manifest['fixtures'][str(group['size_mib'])]
        assert fixture['size_bytes'] == size
        for variant in group['variants']:
            spec = config['variants'][variant]
            output = root / f'{index:02d}-{group["name"]}' / variant
            rows = [json.loads(line) for line in (output / 'rows.jsonl').read_text().splitlines() if line.strip()]
            assert len(rows) == (group.get('runs', 5) + 1) * 2
            total += len(rows)
            paired = {}
            hashes = (spec['x86_binary_sha256'], spec['arm_binary_sha256'])
            if group['direction'] == 'oracle-to-wsl':
                hashes = hashes[::-1]
            for row in rows:
                assert row['success'] and row['path'] == 'direct' and row['direct_route_verified_both'] and runner.verified_direct_evidence(row)
                assert row['source_sha256'] == row['received_sha256'] == fixture['sha256']
                assert row['size_bytes'] == row['bytes_transferred'] == size
                assert row['chunk_size'] == spec.get('chunk_size', 262144)
                assert row['profile_mode'] == ('payload-profile' if spec.get('profile') else 'standard')
                assert row.get('stream_window_bytes') == spec.get('stream_window_bytes')
                if spec.get('experimental_streams') is not None:
                    assert row['experimental_protocol_version'] == spec.get('experimental_protocol_version', 'shared-key-parallel/1')
                    assert row['parallel_streams'] == spec['experimental_streams']
                    assert row['payload_key_count'] == row['kem_sessions'] == 1
                if spec.get('experimental_connections') is not None:
                    audit_connections(row, spec['experimental_connections'])
                assert row['direction'] == group['direction'] and row['pairing_mode'] == 'invite'
                if row['source_staging'] != actual_staging:
                    assert actual_staging == 'per-trial' and group['direction'] == 'oracle-to-wsl' and group['size_mib'] < 64
                    group_summary['staging_metadata_correction'] = 'Legacy runner derived staging from the 64MiB option rather than actual trial size. Small fixtures were staged per trial; raw rows retain the original label. Fixture hashes, timing exclusion and matched staging across variants remain verified.'
                assert (row['sender_binary_sha256'], row['receiver_binary_sha256']) == hashes
                assert math.isclose(row['effective_mib_per_second'], group['size_mib'] / row['wall_seconds'], rel_tol=1e-10)
                for field in ('sender_cpu_seconds', 'receiver_cpu_seconds', 'sender_max_rss_kib', 'receiver_max_rss_kib'):
                    assert math.isfinite(row[field]) and row[field] >= 0
                if spec.get('profile'):
                    assert payload_profile_rejection_reason(row['payload_profile'], size, row['payload_seconds']) is None
                    assert connection_profile_rejection_reason(row.get('path_evidence')) is None
                    stats = row['path_evidence'].get('connection_stats')
                    if stats is not None:
                        assert stats['samples'] >= 2
                        assert stats['send_stalls_over_10ms'] <= stats['send_stalls_over_1ms'] <= stats['send_wait_calls']
                        for prefix in ('lost_packets', 'lost_bytes'):
                            assert stats[prefix + '_delta'] == max(0, stats[prefix + '_end'] - stats[prefix + '_start'])
                        for prefix in ('rtt', 'congestion_window'):
                            unit = '_us' if prefix == 'rtt' else '_bytes'
                            endpoints = [stats[prefix + suffix + unit] for suffix in ('_start', '_end')]
                            endpoints = [x for x in endpoints if x is not None]
                            if endpoints:
                                assert stats[prefix + '_min' + unit] <= min(endpoints)
                                assert stats[prefix + '_max' + unit] >= max(endpoints)
                paired.setdefault((row['run_index'], row['warmup']), []).append(row)
            assert set(paired) == {(0, True)} | {(trial, False) for trial in range(1, group.get('runs', 5)+1)}
            for endpoints in paired.values():
                assert len(endpoints) == 2 and {x['role'] for x in endpoints} == {'sender', 'receiver'}
                assert endpoints[0]['wall_seconds'] == endpoints[1]['wall_seconds']
                if spec.get('experimental_connections') is not None:
                    sender = next(row for row in endpoints if row['role'] == 'sender')
                    receiver = next(row for row in endpoints if row['role'] == 'receiver')
                    for left, right in zip(sender['connection_evidence'], receiver['connection_evidence'], strict=True):
                        assert left['connection_index'] == right['connection_index']
                        assert left['local_endpoint_id'] == right['remote_endpoint_id']
                        assert left['remote_endpoint_id'] == right['local_endpoint_id']
            measured = [row for row in rows if row['role'] == 'sender' and not row['warmup']]
            summary = {field: robust([row[field] for row in measured]) for field in ('effective_mib_per_second', 'wall_seconds', 'sender_cpu_seconds', 'receiver_cpu_seconds', 'sender_max_rss_kib', 'receiver_max_rss_kib', 'handshake_seconds', 'payload_seconds', 'shutdown_seconds')}
            for role in ('sender', 'receiver'):
                summary[role + '_cpu_seconds_per_gib'] = robust([row[role + '_cpu_seconds'] / (size / 1073741824) for row in measured])
            summary['endpoint_profiles'] = [{'run_index': row['run_index'], 'warmup': row['warmup'], 'role': row['role'], 'payload_seconds': row['payload_seconds'], 'payload_profile': row.get('payload_profile'), 'connection_stats': (row.get('path_evidence') or {}).get('connection_stats')} for row in rows if spec.get('profile')]
            summary['redacted_logs'] = []
            for path in sorted((output / 'logs').rglob('*.log')):
                text = path.read_text(errors='replace')
                assert 'rt1:' not in text
                if spec.get('experimental_streams') is not None:
                    version = spec.get('experimental_protocol_version', 'shared-key-parallel/1')
                    if spec.get('experimental_connections') is None:
                        expected_mode = f"Applied experimental mode: version={version} streams={spec['experimental_streams']} payload_keys=1 kem_sessions=1 same_connection=true"
                    else:
                        expected_mode = f"Applied experimental mode: version={version} streams={spec['experimental_streams']} connections={spec['experimental_connections']} payload_keys=1 kem_sessions=1"
                    assert text.count(expected_mode) == 1
                applied = 'Applied benchmark Iroh receive windows:'
                window = spec.get('stream_window_bytes')
                if window is None:
                    assert applied not in text
                else:
                    assert text.count(f'{applied} stream={window} bytes, connection=5000000 bytes') == 1
                summary['redacted_logs'].append({'path': str(path.relative_to(root)), 'sha256': runner.sha256_file(path)})
            group_summary['variants'][variant] = summary
        all_summaries.append(group_summary)
    report = {'audit_scope': 'completed-groups-only' if args.completed_groups_only else 'complete-cohort', 'unaudited_groups': unaudited_groups, 'canonical_endpoint_rows': total, 'accepted_transfers': total//2, 'all_routes_verified_both': True,
        'full_hashes_verified': True, 'all_valid_slow_samples_retained': True,
        'config_sha256': runner.sha256_file(root / 'config.json'), 'manifest_sha256': runner.sha256_file(root / 'manifest.json'), 'groups': all_summaries}
    (root / 'audit-report.json').write_text(json.dumps(report, indent=2) + '\n')
    for group in all_summaries:
        print(group['name'], group['direction'], group['size_mib'])
        for variant, summary in group['variants'].items():
            print(variant, 'rate', round(summary['effective_mib_per_second']['median'], 3), 'CPU/GiB sender/receiver', round(summary['sender_cpu_seconds_per_gib']['median'], 3), round(summary['receiver_cpu_seconds_per_gib']['median'], 3), 'RSS', summary['sender_max_rss_kib']['median'], summary['receiver_max_rss_kib']['median'])
    print('Audited endpoint rows:', total)

if __name__ == '__main__':
    main()
