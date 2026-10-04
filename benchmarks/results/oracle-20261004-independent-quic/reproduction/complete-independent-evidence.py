"""Write the measured acceptance decision and final source-byte audit."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = Path('/home/wasilij/rustytransfer-bench')
ROOT = REPO / 'benchmarks/results/oracle-20261004-independent-quic'
BUILD_NAME = 'independent-connections-v2-20261004'
build = json.loads((ROOT / 'builds' / BUILD_NAME / 'manifest.json').read_text())
cohort = ROOT / 'cohorts/independent-connections-screen-20261004'
audit = json.loads((cohort / 'audit-report.json').read_text())
comparisons = json.loads((cohort / 'comparisons.json').read_text())
baseline = json.loads((BASE / 'build-shared-key-streams-20261003/manifest.json').read_text())
assert all(hashlib.sha256((REPO / name).read_bytes()).hexdigest() == digest
           for name, digest in build['source_files_sha256'].items())
production = {name: digest for name, digest in baseline['source_files_sha256'].items()
              if not name.startswith('examples/')}
assert all(hashlib.sha256((REPO / name).read_bytes()).hexdigest() == digest
           for name, digest in production.items())
assert audit['accepted_transfers'] == 18 and audit['canonical_endpoint_rows'] == 36
assert audit['all_routes_verified_both'] and audit['full_hashes_verified']
assert len(comparisons) == 2 and not any(item['screen_threshold_met'] for item in comparisons)
assert all(result['returncode'] == 0 for result in json.loads((ROOT / 'checks/independent-connections-v2-20261004/results.json').read_text()))
assert len(json.loads((ROOT / 'checks/independent-connections-v2-20261004/results.json').read_text())) == 9
validation = json.loads((ROOT / 'checks/independent-connections-v2-20261004/validation.json').read_text())
assert validation['existing_workspace_clippy_findings'] == 3 and validation['new_example_clippy_findings'] == 0
local = json.loads((ROOT / 'checks/independent-connections-local-v1-20261004/results.json').read_text())
assert len(local) == 8 and all(item['status'] == 'passed' for item in local)
cleanup = json.loads((ROOT / 'cleanup/final-independent-connections-cleanup-20261004.json').read_text())
assert cleanup['original_input_chain_restored']
assert all(not value for role in ('local', 'oracle') for value in cleanup[role].values())

provenance = ROOT / 'provenance'
provenance.mkdir(exist_ok=False)
source_audit = {
    'audited_at_utc': datetime.now(timezone.utc).isoformat(),
    'frozen_source_commit': build['source_commit'],
    'source_archive_sha256': build['source_archive_sha256'],
    'frozen_source_files_checked': len(build['source_files_sha256']),
    'all_frozen_source_bytes_match_final_workspace': True,
    'production_baseline_source_commit': baseline['source_commit'],
    'production_baseline_archive_sha256': baseline['source_archive_sha256'],
    'production_source_files_checked': len(production),
    'all_production_source_bytes_unchanged': True,
    'raw_build_source_dirty': build['source_dirty'],
    'dirty_field_note': 'Raw WSL Git line-ending differences and unrelated user edits are preserved. Exact archive/file hashes identify the source; production bytes match the previous frozen build.',
}
(provenance / 'source-audit.json').write_text(json.dumps(source_audit, indent=2) + '\n')
decision = {
    'date': '2026-10-04', 'evaluation_complete': True,
    'candidate': 'four-independent-QUIC-connections/four-streams',
    'status': 'rejected-for-production-performance-acceptance',
    'reason': 'Neither control comparison reaches >=5% median throughput gain or >=10% CPU/GiB reduction. CPU/RSS increase materially against the one-stream baseline.',
    'scope': {'direction': 'oracle-to-wsl', 'size_bytes': 536870912, 'chunk_size': 262144,
              'warmups_per_variant': 1, 'measured_trials_per_variant': 5,
              'accepted_transfers_including_warmups': 18, 'endpoint_rows': 36,
              'protocol': 'shared-key-parallel/2', 'payload_keys_per_file': 1, 'application_kem_sessions_per_file': 1},
    'thresholds': {'minimum_rate_gain_percent': 5, 'minimum_cpu_per_gib_reduction_percent': 10,
                   'maximum_unexplained_guard_rate_regression_percent': 3,
                   'require_both_controls_for_independent_connection_benefit': True,
                   'require_reversed_order_confirmation_if_screen_qualifies': True,
                   'require_no_material_rss_startup_correctness_regression': True},
    'comparisons': comparisons,
    'confirmation_series_run': False, 'scored_guard_series_run': False,
    'unrun_followup_reason': 'Preregistered representative screen failed against both controls; no qualifying candidate to confirm or guard.',
    'correctness': {'full_size_and_sha256_all_transfers': True,
                    'strict_direct_payload_proof_every_connection_both_endpoints': True,
                    'distinct_connection_ids_verified': True, 'endpoint_ids_cross_checked': True,
                    'local_smokes_passed': 8, 'example_tests_passed': 16, 'python_tests_passed': 56,
                    'required_repository_commands_passed': 9,
                    'workspace_clippy_existing_findings': 3, 'new_example_clippy_findings': 0,
                    'no_scored_failures_or_sample_exclusions': True,
                    'valid_slow_samples_retained': True},
    'frozen_build': {field: build[field] for field in ('source_commit', 'source_archive_sha256', 'x86_binary_sha256', 'arm_binary_sha256')},
    'source_audit': source_audit, 'cleanup': cleanup,
    'limits': ['Only this prototype/size/direction/endpoint pair is scored.',
               'Fresh transfer only; experimental resume is unimplemented and production resume is unchanged.',
               'Prototype framing, chunk buffers and finalization differ from production; no direct historical Croc/production speed comparison.',
               'Negative screen does not establish global optimality or a fundamental QUIC throughput ceiling.',
               'Partial bootstrap guards are audited in source; physical signals cover initial accept and active payload.',
               'Failed Windows-mounted build log is retained and superseded by successful native builds; no failed scored transfer.'],
    'production_performance_defaults_changed': False,
    'desktop_or_release_artifacts_changed': False,
}
assert not (ROOT / 'acceptance.json').exists()
(ROOT / 'acceptance.json').write_text(json.dumps(decision, indent=2) + '\n')
(ROOT / 'reproduction/complete-independent-evidence.py').write_bytes(Path(__file__).read_bytes())
print(json.dumps({'status': decision['status'], 'source_files_checked': source_audit['frozen_source_files_checked'],
                  'production_files_unchanged': len(production), 'accepted_transfers': 18}))
