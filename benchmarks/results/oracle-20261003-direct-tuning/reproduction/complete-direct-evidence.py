"""Write decisions and final tables only after all required audits exist."""
import argparse
import hashlib
import json
import statistics
from pathlib import Path

BASE = Path('/home/wasilij/rustytransfer-bench')
REPO = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('destination', type=Path)
cli = p.parse_args()
assert cli.destination.is_dir()

def audit(name):
    return json.loads((BASE / 'results' / f'next-{name}-20261003' / 'audit-report.json').read_text())

def median(variant, field):
    return variant[field]['median']

def change(candidate, baseline):
    return (candidate / baseline - 1) * 100

names = ['old-profile', 'diagnostics', 'chunks-screen', 'chunks-remaining', 'chunk-confirm-upload64', 'chunk-startup-retry', 'chunk-startup64-upload', 'copy-screen', 'window-screen', 'profile-overhead', 'shared-key-streams', 'croc-final']
reports = {name: audit(name) for name in names}
confirmation = BASE / 'results/next-shared-key-streams-confirm-20261003/audit-report.json'
if confirmation.exists():
    reports['shared-key-streams-confirm'] = json.loads(confirmation.read_text())
cleanup = json.loads((BASE / 'results/final-direct-tuning-cleanup-20261003.json').read_text())
assert cleanup['original_input_chain_restored']
for report in reports.values():
    assert report['all_routes_verified_both'] and report['full_hashes_verified']

# Keep paired evidence visible alongside the target-scenario median gate.
paired_changes = []
for name, index in [('chunks-remaining', 2), ('chunk-confirm-upload64', 0)]:
    root = BASE / 'results' / f'next-{name}-20261003'
    config = json.loads((root / 'config.json').read_text())
    group = config['groups'][index]
    rates = {}
    for variant in ('chunk-256k', 'chunk-1024k'):
        path = root / f'{index:02d}-{group["name"]}' / variant / 'rows.jsonl'
        rates[variant] = {row['run_index']: row['effective_mib_per_second'] for row in map(json.loads, path.read_text().splitlines()) if row['role'] == 'sender' and not row['warmup']}
    paired_changes.extend(change(rates['chunk-1024k'][trial], rates['chunk-256k'][trial]) for trial in range(1, 6))

parallel = []
for name in ('shared-key-streams', 'shared-key-streams-confirm'):
    if name not in reports:
        continue
    group = reports[name]['groups'][0]
    one, four = (group['variants'][f'streams-{count}'] for count in (1, 4))
    parallel.append({'series': group['series'], 'rate_change_percent': change(median(four, 'effective_mib_per_second'), median(one, 'effective_mib_per_second')), 'sender_cpu_per_gib_change_percent': change(median(four, 'sender_cpu_seconds_per_gib'), median(one, 'sender_cpu_seconds_per_gib')), 'receiver_cpu_per_gib_change_percent': change(median(four, 'receiver_cpu_seconds_per_gib'), median(one, 'receiver_cpu_seconds_per_gib'))})

checks = BASE / 'checks-shared-key-final-20261003'
assert all(result['rate_change_percent'] < 5 and result['sender_cpu_per_gib_change_percent'] > -10 and result['receiver_cpu_per_gib_change_percent'] > -10 for result in parallel)
assert all(item['returncode'] == 0 for item in json.loads((checks / 'results.json').read_text()))
assert len(json.loads((checks / 'clippy.sarif').read_text())['runs'][0]['results']) == 3
smoke_root = BASE / 'checks-shared-key-example-20261003/final-readiness-v3'
for count in (1, 4):
    smoke = json.loads((smoke_root / f'success-{count}/smoke.json').read_text())
    assert all(smoke['strict_direct_evidence'].values())
for name in ('failure-truncated-source', 'cancel-sender-payload', 'cancel-receiver-payload'):
    smoke = json.loads((smoke_root / name / 'smoke.json').read_text())
    assert smoke['part_removed'] and smoke['output_absent'] and smoke['sender_status'] != 0 and smoke['receiver_status'] != 0

decision = {
    'performance_defaults_changed': False,
    'accepted_infrastructure_commits': {'diagnostics': 'f9412e8', 'chunk_correctness': 'c78d5d6', 'bounded_opt_in_windows': '3356360', 'shared_key_example_and_runner': '37a2bad'},
    'gate': {'throughput_percent': 5, 'cpu_per_gib_reduction_percent': 10, 'confirmation_series': 2, 'unexplained_guard_rate_regression_limit_percent': 3, 'variance_policy': 'Increase samples or reject the claim when variance obscures a result.'},
    'owned_chunk': {'decision': 'rejected', 'reason': 'Rate gain did not confirm; sender CPU increased in both series. Runtime patch is unshipped.'},
    'receive_window': {'decision': 'no-default-change', 'reason': 'Gains were below threshold, with higher CPU and RSS.'},
    'chunk_512k': {'decision': 'no-default-change', 'reason': '512MiB upload regressed 14.6% despite 64MiB upload improvement.'},
    'chunk_1m': {'decision': 'no-default-change', 'target_median_gate_met': True, 'target': '64MiB WSL-to-Oracle', 'series_rate_changes_percent': [6.487835, 12.009656], 'paired_rate_changes_percent': paired_changes, 'paired_wins': sum(value > 0 for value in paired_changes), 'paired_trials': len(paired_changes), 'paired_change_median_percent': statistics.median(paired_changes), 'reason': 'Positive target medians are retained. Wide WAN ranges and only five of ten paired wins leave a robust general default benefit uncertain; RSS increased. Existing opt-in sizes remain available.'},
    'pipeline': {'decision': 'not-implemented', 'reason': 'Fresh elapsed profiles show small file/copy spans and dominant transport waits, without demonstrated application starvation.'},
    'parallel': {'decision': 'reject-production-expansion', 'protocol': 'shared-key-parallel/1', 'connections': 1, 'stream_counts': [1, 4], 'payload_keys_per_file': 1, 'kem_sessions_per_file': 1, 'series': parallel, 'reason': 'Four streams failed the representative throughput and CPU gates. Keep the separately versioned fresh-transfer-only example as experimental evidence. One connection does not reproduce four independent TCP congestion budgets; multiple connections and experimental resume remain unimplemented.'},
    'audited_valid_transfers_including_warmups': sum(report['accepted_transfers'] for report in reports.values()),
    'audited_endpoint_rows': sum(report['canonical_endpoint_rows'] for report in reports.values()),
    'audit_scopes': {name: report.get('audit_scope', 'complete-cohort') for name, report in reports.items()},
    'excluded_records': ['Two incomplete 4KiB-upload groups with receiver zero STREAM frames; retained and unscored.', 'Stale deterministic-mtime build detected by executable hashes before any transfer.', 'Unrun independent-session parallel draft discarded for violating the shared payload-key requirement.', 'Earlier local mixed-route smokes and failed cancellation-smoke setup retained; final corrected v3 smokes pass.'],
    'validation': {'workspace_gates_passed': True, 'existing_clippy_findings': 3, 'benchmark_python_tests': 53, 'example_unit_tests': 13, 'example_clippy_new_findings': 0, 'final_local_strict_one_and_four_stream_smokes_passed': True, 'truncation_and_sigterm_cleanup_passed': True, 'final_cleanup': cleanup},
    'limitations': ['Warm-cache sequential WAN cohorts on this endpoint pair; network variation is substantial.', 'Elapsed profiles do not provide sampled CPU attribution; Oracle perf permission is unavailable.', 'CPU is per-process user plus system time, not whole-host energy or all network background work.', 'No native Windows/macOS or physical LAN performance measurement.', '4KiB upload startup could not be scored under unchanged strict route proof; 64KiB guard used instead.', 'Experimental parallel resume and multiple-connection behavior remain unimplemented; production resume remains unchanged.'],
}
# Derive exact median percentages rather than storing rounded display values.
for position, report in enumerate((reports['chunks-remaining'], reports['chunk-confirm-upload64'])):
    group = report['groups'][2 if position == 0 else 0]['variants']
    decision['chunk_1m']['series_rate_changes_percent'][position] = change(median(group['chunk-1024k'], 'effective_mib_per_second'), median(group['chunk-256k'], 'effective_mib_per_second'))
(cli.destination / 'acceptance.json').write_text(json.dumps(decision, indent=2) + '\n')

readme = (REPO / 'target/direct-tuning-README.md').read_text()
pending = 'Pending: remaining chunk cases, profiling overhead, parallel screen, fresh final\nCroc comparison, cleanup audit and final acceptance record. Remove this pending\nline only after all requested work has completed.\n'
assert pending in readme
readme = readme.replace(pending, '')
readme += '\n## Shared-key parallel screen\n\nOne authenticated KEM exchange supplies one shared AES-256-GCM payload key\nto one or four data streams on one QUIC connection. Global chunk counters\npartition nonce use; AAD binds the version, session, manifest/ranges, stream,\nindex, offset and length. The experimental ALPN/framing is isolated from\nproduction. Each stream keeps one bounded chunk operation in flight; output\nuses disjoint file offsets. A 500ms stable-direct readiness wait applies equally\nto both modes and stays inside startup timing. Strict evidence ends before\nFIN/ACK; cancellation aborts and joins siblings and removes owned partials.\n\nThis fresh-transfer prototype has no resume implementation and uses separate\nencryption/decryption buffers. Its one-stream control shares that implementation.\nRates compare one versus four streams within the prototype, not a drop-in\nreplacement for production or four independent Croc TCP flows.\n\n| Series | One / four streams MiB/s | Rate change | Sender CPU s/GiB, one / four | Receiver CPU s/GiB, one / four | Sender RSS KiB, one / four | Receiver RSS KiB, one / four |\n| --- | ---: | ---: | ---: | ---: | ---: | ---: |\n'
for result in parallel:
    report = reports['shared-key-streams' if result['series'] == 1 else 'shared-key-streams-confirm']
    one, four = (report['groups'][0]['variants'][f'streams-{count}'] for count in (1, 4))
    values = [f"{median(one, field):.3f} / {median(four, field):.3f}" for field in ('effective_mib_per_second', 'sender_cpu_seconds_per_gib', 'receiver_cpu_seconds_per_gib', 'sender_max_rss_kib', 'receiver_max_rss_kib')]
    readme += f"| {result['series']} | {values[0]} | {result['rate_change_percent']:+.2f}% | {' | '.join(values[1:])} |\n"
readme += '\nFour streams failed the representative throughput and CPU gates. No confirmation\nseries is needed for accepting this candidate. The valid slow one-stream trial\nremains included; its first-pair apparent rate gain did not persist.\n'
readme += '\nNo production parallel architecture is accepted or shipped. Final local\none/four-stream strict route/hash checks and protocol truncation/SIGTERM\ncancellation checks pass. Prior mixed-route and smoke-setup failures remain\nexcluded diagnostics. SIGKILL cannot run cleanup and may leave an experimental\npartial; the prototype makes no resume claim.\n\n## Final direct Croc comparison\n\nFrozen production CLI defaults (256 KiB, profile/window controls unset) compare\nto official Croc 11.5.4 with compression off and four TCP data channels. Each\nsize/tool has one warmup and five alternating scored runs. All 24 transfers\npassed complete hashes and actual direct-route proof.\n\n| File | Rustytransfer / Croc MiB/s | Croc rate advantage | Sender CPU s/GiB, Rustytransfer / Croc | Receiver CPU s/GiB, Rustytransfer / Croc |\n| --- | ---: | ---: | ---: | ---: |\n'
for comparison in reports['croc-final']['comparisons']:
    rust, croc = (comparison['variants'][name] for name in ('iroh', 'croc'))
    values = [f"{median(rust, field):.3f} / {median(croc, field):.3f}" for field in ('effective_mib_per_second', 'sender_cpu_seconds_per_gib', 'receiver_cpu_seconds_per_gib')]
    readme += f"| {comparison['size_mib']} MiB | {values[0]} | {comparison['croc_rate_advantage_percent']:+.2f}% | {values[1]} | {values[2]} |\n"
readme += '\nAudit JSON retains all rate/time/CPU/RSS ranges and MADs, full executable and\nfixture provenance, direct route excerpts, excluded records and exact firewall\nrestoration. The bounded lease allowed only the dynamically observed SSH\nclient IPv4 /32 on TCP9009–9013. Independent final checks found no benchmark\nendpoints, listeners or leases, and the original INPUT chain hash matched.\n\nAll required workspace/fmt/wasm, 53 benchmark Python tests, security identity\nself-tests, Clippy/SARIF identity/report gates (exactly three existing findings),\nand cargo-deny checks pass. The example adds 13 focused tests with no new\nexample Clippy findings. CPU metrics are per-process user plus system time,\nnot whole-host energy or all network background work. No physical LAN or\nnative Windows/macOS performance claim is made. See [acceptance.json](acceptance.json).\n'
(cli.destination / 'README.md').write_text(readme)
print(json.dumps({'valid_transfers_including_warmups': decision['audited_valid_transfers_including_warmups'], 'paired_1m_wins': decision['chunk_1m']['paired_wins'], 'parallel': parallel}))
