"""Write the phase-reference evidence record and append the efficiency report."""
import json
import statistics
from pathlib import Path

repo=Path(__file__).resolve().parents[1]
root=repo/'benchmarks/results/oracle-20261004-completion'
build=json.loads((root/'builds/completion-v1-20261004/manifest.json').read_text())
comparison=json.loads((root/'cohorts/completion-croc-retry-20261004/audit-report.json').read_text())
screen=json.loads((root/'cohorts/completion-screen-20261004/completion-analysis.json').read_text())['groups'][0]
diagnostic=json.loads((root/'cohorts/completion-diagnostics-20261004/completion-analysis.json').read_text())['groups'][0]
checks=json.loads((root/'checks/completion-final-20261004/results.json').read_text())
assert len(checks)==9 and all(item['returncode']==0 for item in checks)
findings=json.loads((root/'checks/completion-final-20261004/clippy.sarif').read_text())['runs'][0]['results']
assert sorted(item['ruleId'] for item in findings)==['clippy::as_conversions','clippy::expect_used','clippy::expect_used']
assert comparison['accepted_transfers']==24 and comparison['full_hashes_verified']
labels={'handshake_seconds':'Setup + handshake','payload_seconds':'Payload','shutdown_seconds':'Completion','remaining_outer_seconds':'Remaining outer time'}
tables=[]
payload_rows=[]
for cohort in comparison['comparisons']:
    size=cohort['size_mib']; reference=cohort['croc_reference']['mean_full_mib_per_second']
    table=[f'### {size} MiB — Croc full-transfer mean: {reference:.3f} MiB/s','',
           '| Rustytransfer sender phase | Mean seconds (range) | Mean per-run phase rate, MiB/s | Rate / Croc full mean |',
           '| --- | ---: | ---: | ---: |']
    for field,phase in cohort['rustytransfer_phases']['sender'].items():
        durations=phase['seconds']
        table.append(f"| {labels[field]} | {phase['mean_seconds']:.3f} ({durations['min']:.3f}–{durations['max']:.3f}) | {phase['mean_normalized_mib_per_second']:.3f} | {phase['ratio_to_croc_full_mean']:.3f}× |")
    tables.append('\n'.join(table))
    for role in ('sender','receiver'):
        phase=cohort['rustytransfer_phases'][role]['payload_seconds']
        payload_rows.append(f"| {size} MiB {role} payload | {phase['mean_normalized_mib_per_second']:.3f} | {reference:.3f} | {phase['ratio_to_croc_full_mean']:.3f}× |")

baseline=screen['variants']['baseline']; candidate=screen['variants']['explicit-close']
diagnostic_native=diagnostic['variants']['baseline']['endpoint_profiles']['receiver']['post_metric_process_seconds']['median']
diagnostic_git=diagnostic['variants']['baseline-workspace']['endpoint_profiles']['receiver']['post_metric_process_seconds']['median']
commit=baseline['endpoint_profiles']['receiver']['receiver_commit_seconds']
close=baseline['endpoint_profiles']['receiver']['endpoint_close_seconds']
body=f'''# Completion diagnostics and Croc phase reference — 2026-10-04

No completion performance default is accepted. Explicit QUIC close failed the
representative screen. The new measurements identify receiver file commit as a
larger usual completion cost than endpoint close, and expose a benchmark
working-directory confound in historical metrics-enabled runs.

Current reporting follows the user's requested policy: **compare each
Rustytransfer phase individually against Croc's arithmetic mean full-transfer
throughput; do not compare Rustytransfer's overall throughput against Croc.**
Historical raw records retain their original measurements.

## Fresh phase-reference cohort

The stock pinned Croc 11.5.4 and production Rustytransfer binary use identical
native fixtures and the same local non-Git cwd, `/home/wasilij/rustytransfer-bench`.
Direction is Oracle ARM sender → WSL x86 receiver. Each product and size has
one unscored warmup and five alternating measured runs. Payload and completion
profiling, explicit close and window controls are disabled. Rustytransfer uses
one connection, one stream and the unchanged 256 KiB chunk default.

Croc reference = arithmetic mean of the five `file MiB / full wall seconds`
rates. Rustytransfer phase mean = arithmetic mean of five `file MiB / phase
seconds` rates. Mean durations and mean rates are separate averages and cannot
be inverted to obtain one another. Only payload is a file-data timing window;
setup, completion and remaining outer rates normalize an overhead duration,
and are **comparison indices, not actual wire throughput**. The tables contain
no Rustytransfer overall-rate comparison. Exact Croc payload/commit markers are
unavailable; its full-transfer mean is intentionally the reference.

{'\n\n'.join(tables)}

| Rustytransfer payload phase | Mean MiB/s | Croc full mean MiB/s | Phase / reference |
| --- | ---: | ---: | ---: |
{'\n'.join(payload_rows)}

All 24 transfers / 48 endpoint rows passed full-file SHA-256, size and strict
both-endpoint direct-route checks. Valid slow samples remain included. The
slowest 512 MiB Rustytransfer run had payload **22.793 s** and completion
**4.097 s**; completion alone does not explain every slow observation.
Setup includes CLI/transport setup plus application handshake. Remaining outer
time is the residual outside the chosen endpoint's existing phase timers,
including process/SSH orchestration, metric emission and exit work; it is not
a new network phase. Receiver and sender lifetimes overlap and must not be added.

The [audited phase report](cohorts/completion-croc-retry-20261004/audit-report.json)
retains means, medians, ranges/MAD, both roles, resources, raw rows, schedule,
frozen harness snapshots and [actual Croc route excerpts](cohorts/completion-croc-retry-20261004/croc-route-evidence.json).
CPU remains a separate efficiency goal: 512 MiB sender/receiver CPU medians
are 13.26/14.34 s/GiB for Rustytransfer and 3.72/12.16 for Croc.

## Rustytransfer-only explicit-close screen

Same frozen executable pair for baseline and candidate; both enable coarse
completion profiling, with payload profiling off. One warmup and five alternating
512 MiB Oracle-to-WSL samples per mode. Candidate requests QUIC close only after
existing authenticated confirmation, commit and successful stream-delivery
conditions, and still awaits bounded endpoint close. No timeout, key, framing,
resume, cancellation or NAT-traversal guarantee is relaxed.

| Rustytransfer mode | Median MiB/s (range; MAD) | Sender / receiver CPU s/GiB |
| --- | ---: | ---: |
| Baseline | {baseline['rate']['median']:.3f} ({baseline['rate']['min']:.3f}–{baseline['rate']['max']:.3f}; {baseline['rate']['mad']:.3f}) | {baseline['sender_cpu_gib']['median']:.2f} / {baseline['receiver_cpu_gib']['median']:.2f} |
| Explicit close | {candidate['rate']['median']:.3f} ({candidate['rate']['min']:.3f}–{candidate['rate']['max']:.3f}; {candidate['rate']['mad']:.3f}) | {candidate['sender_cpu_gib']['median']:.2f} / {candidate['receiver_cpu_gib']['median']:.2f} |

Median rates changed {screen['changes_percent']['rate']:.2f}%; paired median rate
change was {(screen['paired_rate_ratios']['median']-1)*100:.2f}%, with
{screen['paired_wins']}/5 paired wins. CPU did not improve. Neither >=5% repeatable
rate nor >=10% CPU/GiB saving passed. This is not a universal slowdown claim;
the failed screen ends acceptance, with no reversed confirmation or scored
direction/startup/resume guard campaign. Default close remains unchanged.
All 12 transfers / 24 rows passed hashes, size, strict direct evidence and
requested/applied-mode plus span-bound checks.

Baseline receiver commit median was **{commit['median']:.3f} s**
(range {commit['min']:.3f}–{commit['max']:.3f}); receiver endpoint close median
was **{close['median']:.3f} s** (range {close['min']:.3f}–{close['max']:.3f}).
Occasional larger endpoint-close delays are retained. Commit includes
`sync_data`, hard-link publication, handle drop and partial-file removal;
the current aggregate cannot isolate sync cost. The sender's receive-finish
wait overlaps the receiver's commit and is not a separate additive cost.
These finer profiles are from an earlier measurement window than the fresh
Croc reference, and do not establish Croc's phase timings.

Rustytransfer waits for `sync_data` before publishing its received file.
The pinned [Croc receiver completion path](https://github.com/schollz/croc/blob/v11.5.4/src/croc/croc.go#L3544-L3557)
closes its received file without an explicit `Sync` in that path. This is a
relevant behavior difference, not proof that all observed variance is sync.
The isolated parallel prototype also flushes without the production durable
commit, and uses a different metric writer; its earlier 27.374 MiB/s result
cannot establish a production optimization against Croc.

## Working-directory diagnostic

CLI metric emission runs `git rev-parse HEAD` and `git status --porcelain`
after the new application-lifetime timestamp. Untimed Git status checks took
3.224/5.317/3.298 seconds in the Windows-mounted checkout, versus roughly 1 ms
outside Git. One warmup and one measured 64 MiB transfer per diagnostic mode
used the same binary. The measured receiver process tail after the application
timestamp was **{diagnostic_git:.3f} s** in the checkout versus
**{diagnostic_native:.3f} s** natively. This tail includes metric emission and
exit work; source inspection and separate Git diagnostics identify the costly
provenance scan. The single diagnostic is not a performance acceptance series.

`--endpoint-cwd` makes the local cwd explicit for both products. Native endpoint
Git fields can be null; frozen source/executable provenance is authoritative.
Outer timers remain inclusive. This is a **benchmark correction**, not an
ordinary-transfer optimization: without `RUSTYTRANSFER_METRICS_JSONL`, the
writer returns before Git. Historical scan costs cannot be retroactively
subtracted, and fresh-vs-historical differences are not production gains.
All six diagnostic transfers / twelve rows passed hashes/direct/profile checks.

## Provenance, validation and interruption

Measured frozen source is based on `{build['source_commit']}` with uncommitted
instrumentation archived exactly; implementation commit is `4e807a8`.

| Artifact | SHA-256 |
| --- | --- |
| Source archive | `{build['source_archive_sha256']}` |
| x86 executable | `{build['x86_binary_sha256']}` |
| ARM executable | `{build['arm_binary_sha256']}` |

[Build manifest and archive](builds/completion-v1-20261004/manifest.json)
record Rust 1.97.0 and all 56 input files. The [source audit](provenance/source-audit.json)
verifies every archive member. Final source differs in exactly four files for
post-measurement lint cleanup: the same two profiling booleans are grouped in
a private `ProfileOptions`, and a nested Iroh `if let` is collapsed. The exact
[patch](provenance/post-measurement-lint-cleanup.patch) and final byte hashes are
retained. No measured gain is attributed to those cleanup edits.

Final workspace, formatting, WASM, **64 Python benchmark tests**, security
scanner/identity/report self-tests, cargo-deny and Clippy/SARIF checks pass.
Clippy has exactly the three existing findings (`expect_used` twice,
`as_conversions` once). The initial full check had two new warnings, corrected
before the final passing bundle; both bundles remain retained. The focused
transfer suite passes 15 tests, including delayed confirmation and explicit-close
ordering. Initial duplicate-method/Instant compile errors and a targeted strict
Clippy run hitting existing crypto warnings are recorded in the developer note.

The first Croc comparison lost its runner session before the final trial.
Its **23 recorded successful transfers / 46 rows remain unscored**, with a
[partial audit](interrupted/completion-croc-20261004/partial-audit.json), redacted
logs and interruption reason. The bounded lease expired and restored INPUT;
the remaining Oracle Croc waiter was terminated through validated process-group
cleanup. The initial partial collector assumed a Croc endpoint byte-count field,
failed, and was corrected; no raw rows changed. The complete retry supplies the
final phase reference. There are **42 fully audited complete-cohort transfers**
in this record; the interrupted set is excluded from that total.

The [final independent cleanup](cleanup/final-cleanup.json) found no benchmark
endpoints, TCP listeners or leases, with original INPUT hash
`8eabe87e4569e251c3147f193c0880b3c52ae3eaf5d007db8dda219ad79a53a5`.
Croc leases were bounded and restricted to the dynamically derived client /32;
OCI was unchanged. Builds, tests, fixture generation and external verification
did not overlap timed transfers. Desktop/device work remains stopped; user edits
and pending extension/release artifacts remain intact.

## Next experiments and reproduction

First split receiver commit into sync, publication and cleanup spans. If sync
dominates, evaluate one bounded background writeback/sync operation while payload
continues, preserving the mandatory final sync before publication and draining
work on cancellation. A simple overlap with FIN/FIN_ACK can only hide the brief
control exchange, so it is unlikely to remove seconds by itself. Investigate
setup only after separating bind/online/address discovery from connection waits;
current ready-observed medians were similar for both products. Retain payload
stall/loss diagnostics for slow payload observations. No new scheduling or
protocol change is accepted by this record.

[`reproduction/`](reproduction) contains cohort preparation, strict collectors,
source/build helpers, cleanup/lease handling, phase reporting and the original
preregistered plan plus reporting amendment. Helpers expect their original
`target/` location; copy them there and keep `benchmarks/` on the Python path.
Use frozen per-cohort runner/helper snapshots and the archived measured source
for exact code provenance. Later reporting helpers implement the user's phase
policy. Repeating WAN conditions and storage latency is not guaranteed.

Large binaries/fixtures remain in native benchmark storage; full redacted per-run
logs remain local and ignored by Git. The inventory hashes retained artifacts;
structured rows, schedules, source archive, source differences, checks, route
excerpts, decisions and cleanup are versioned.
'''
(root/'README.md').write_text(body)
acceptance=dict(accepted_performance_defaults=[],explicit_close_screen='rejected-no-repeatable-threshold-gain',
                screen_changes_percent=screen['changes_percent'],paired_rate_change_percent=(screen['paired_rate_ratios']['median']-1)*100,
                confirmation_and_scored_guards='not-run-after-failed-screen',
                benchmark_cwd_correction='accepted-infrastructure-only',
                comparison_policy='Only Rustytransfer phases individually versus Croc arithmetic mean full-transfer throughput.',
                croc_references=[dict(size_mib=item['size_mib'],mean_full_mib_per_second=item['croc_reference']['mean_full_mib_per_second']) for item in comparison['comparisons']],
                completed_cohort_transfers=42,interrupted_unscored_transfers=23,
                implementation_commit='4e807a8',measured_build='completion-v1-20261004',
                final_source_diff='post-measurement-lint-cleanup-only; archived patch and hashes',
                validation='final-required-gates-pass; exactly-three-existing-clippy-findings',
                next_priority='Split durable commit; conditionally test bounded overlap while keeping final sync and publication guarantees.')
(root/'acceptance.json').write_text(json.dumps(acceptance,indent=2)+'\n')
note=dict(initial_compile_errors=['duplicate take_endpoint_close_seconds method','std::time::Instant / tokio::time::Instant mismatch'],
          corrections='Fixed before frozen measured build; no scored transfer affected.',
          focused_transfer_tests='cargo test -p rustytransfer-transfer: 15 passed',
          workspace_check='cargo check --workspace: passed',
          strict_targeted_clippy='Failed on two existing expect_used findings in crates/crypto/src/mac.rs',
          filtered_targeted_clippy='cargo clippy -p rustytransfer-transfer -p rustytransfer-native --lib -- -D warnings -A clippy::expect_used -A clippy::as_conversions: passed',
          log_availability='Developer had no saved logs for these targeted checks; root full initial and final gate logs are retained.')
(root/'checks/developer-check-note.json').write_text(json.dumps(note,indent=2)+'\n')
report=repo/'docs/direct-transfer-efficiency-report.md'
heading='## Completion diagnostics and phase-only Croc reference — 2026-10-04'
text=report.read_text()
assert heading not in text
section=f'''

{heading}

Current Croc reporting follows the requested phase policy: compare each
Rustytransfer phase individually with Croc's arithmetic mean full-transfer
rate; do not compare Rustytransfer's overall throughput with Croc. Historical
tables above remain their original cohort records.

| Rustytransfer payload phase | Mean MiB/s | Croc full mean MiB/s | Phase / reference |
| --- | ---: | ---: | ---: |
{'\n'.join(payload_rows)}

The fresh 512 MiB sender phase means were setup/handshake **0.925 s**, payload
**17.812 s**, completion **2.930 s**, and remaining outer time **0.445 s**.
Phase rates average each run's file MiB / phase seconds. Setup and completion
rates are normalized indices, not actual data throughput. The slowest retained
payload window was 22.793 s; completion alone cannot explain every slow run.

The Rustytransfer-only explicit-close screen failed: baseline/candidate medians
25.391/23.827 MiB/s, with paired median change +0.42% and no CPU saving. No
default change, confirmation or scored guard campaign was accepted. Coarse
baseline profiles measured receiver commit median 1.788 s (1.016–3.726), versus
endpoint-close median 0.032 s (0.023–1.388). The sender's stream wait overlaps
receiver commit. Those diagnostics are from a different window than the fresh
Croc reference and do not supply Croc payload/commit phases.

Metric emission exposed a working-directory confound: Git status took
3.2–5.3 s in the Windows-mounted checkout. A 64 MiB diagnostic measured a
3.125 s receiver tail after the application timestamp there, versus 0.003 s
outside Git. `--endpoint-cwd` now fixes the local cwd for both products. This is
benchmark infrastructure, not an ordinary-transfer improvement; old scan costs
cannot be retroactively subtracted. Outer timing stays inclusive.

Receiver commit includes durable `sync_data` before hard-link publication.
The pinned [Croc completion path](https://github.com/schollz/croc/blob/v11.5.4/src/croc/croc.go#L3544-L3557)
closes without an explicit `Sync` in that path. Split commit into sync and
publication costs next; conditionally test bounded writeback overlap while
preserving mandatory final sync, cancellation and publication guarantees.
Similar ready-observed medians do not currently isolate startup as the gap's
cause. CPU efficiency remains a separate goal.

All 42 complete-cohort transfers passed full hashes and direct-route checks.
An interrupted 23-transfer set stays unscored; its complete retry provides the
phase reference. Final workspace/WASM/formatting/security/dependency gates and
64 Python tests pass, with exactly three existing Clippy findings. The measured
source archive and the four-file post-measurement lint-cleanup patch are both
retained; no rate gain is attributed to that cleanup. Original INPUT was restored
exactly, with no endpoints/listeners/leases left. Desktop/device and release
boundaries remain unchanged.

[Full phase tables, provenance, raw rows, acceptance and reproduction](../benchmarks/results/oracle-20261004-completion/README.md)
record implementation commit `4e807a8` and exact measured frozen binaries.
'''
report.write_text(text+section)
print('Wrote phase-reference report, acceptance decision and efficiency-report update.')
