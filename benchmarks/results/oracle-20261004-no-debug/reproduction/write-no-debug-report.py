"""Write the final clean report from audited data, with diagnostics kept separate."""
import argparse
import json
import statistics
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('destination',type=Path)
args=p.parse_args()
root=args.destination
cohort=root/'cohorts/no-debug-final-20261004'
audit=json.loads((cohort/'audit-report.json').read_text())
manifest=json.loads((cohort/'manifest.json').read_text())
rows=[json.loads(line) for line in (cohort/'raw.jsonl').read_text().splitlines()]
summary={(r['size_mib'],r['transport']):r for r in audit['summary']}
lines=['# Final comparison without debug flags — 2026-10-04','',
'User-requested final measurement with all application debug and benchmark',
'controls disabled. Overall runtimes and rates are reported at the user’s',
'explicit request; the prior diagnostic phase policy remains separate. No',
'production performance default was changed by these measurements.','',
'Oracle ARM sender → native WSL x86 receiver. At each size: one unscored',
'warmup per tool and five alternating measured runs per tool, sequential.',
'Rustytransfer uses the frozen optimized release binary, one QUIC connection,',
'one stream, the normal invite flow and the unchanged 256 KiB chunk default.',
'Croc uses the official pinned 11.5.4 release assets, direct local mode, four',
'TCP data connections and compression disabled. Native incompressible input',
'fixtures are identical on both hosts.','',
'| Size | Rustytransfer mean seconds | Croc mean seconds | Rustytransfer mean MiB/s | Croc mean MiB/s | Rustytransfer rate difference vs Croc |',
'| --- | ---: | ---: | ---: | ---: | ---: |']
comparisons=[]
for size in (1024,2048,4096):
    rusty=summary[(size,'rustytransfer')]
    croc=summary[(size,'croc')]
    gap=100*(1-rusty['mean_mib_per_second']/croc['mean_mib_per_second'])
    lines.append(f"| {size//1024} GiB | {rusty['mean_wall_seconds']:.2f} | {croc['mean_wall_seconds']:.2f} | {rusty['mean_mib_per_second']:.2f} | {croc['mean_mib_per_second']:.2f} | {-gap:+.2f}% |")
    comparisons.append(dict(size_mib=size,rustytransfer=rusty,croc=croc,throughput_gap_percent=gap,
                            rustytransfer_rate_difference_percent=-gap))
lines += ['',
'Rate means average the five per-run `file MiB / full wall seconds` rates;',
'mean durations are averaged separately and are not reciprocals of those',
'mean rates. A positive rate difference means Rustytransfer was faster in',
'this cohort. Full wall time begins at the first endpoint launch and ends',
'when both endpoint subprocesses have exited. It includes SSH, readiness and',
'concurrent external observation work. Independent exit watchers prevent an',
'observer poll or post-exit verification from extending that timer. External',
'fixture and received-file hashing stays outside it. Valid slow samples remain',
'included. Exact instrumented phase durations are unavailable in this clean cohort.','',
'## Spread and resources','',
'| Size | Tool | Median MiB/s | Range MiB/s | MAD MiB/s | Sender CPU s/GiB, median | Receiver CPU s/GiB, median |',
'| --- | --- | ---: | ---: | ---: | ---: | ---: |']
for size in (1024,2048,4096):
    for tool in ('rustytransfer','croc'):
        s=summary[(size,tool)]
        selected=[r for r in rows if r['role']=='sender' and r['transport']==tool and r['size_bytes']==size*1048576 and not r['warmup']]
        sender=statistics.median(r['sender_cpu_seconds']/(size/1024) for r in selected)
        receiver=statistics.median(r['receiver_cpu_seconds']/(size/1024) for r in selected)
        lines.append(f"| {size//1024} GiB | {tool} | {s['median_mib_per_second']:.2f} | {s['min_mib_per_second']:.2f}–{s['max_mib_per_second']:.2f} | {s['mad_mib_per_second']:.2f} | {sender:.2f} | {receiver:.2f} |")
progress=json.loads((cohort/'normal-croc-progress-observations.json').read_text())
lines += ['', '## Coarse Croc timings from normal output', '',
'The official binary’s ordinary progress bars show separate source-file hashing',
'and data-transfer elapsed values without debug logging. The table averages',
'each measured run’s last reported elapsed hint. Values are rounded/stale display',
'hints, rather than exact additive phase boundaries or instrumentation timers.', '',
'| Size | Source hashing elapsed hint, mean seconds | Data transfer elapsed hint, mean seconds |',
'| --- | ---: | ---: |']
for size in (1024,2048,4096):
    items=[r for r in progress['observations'] if r['size_mib']==size and not r['warmup']]
    values={name:[r['progress_elapsed_hints'][name]['displayed_elapsed_seconds'] for r in items if r['progress_elapsed_hints'][name] is not None] for name in ('source_hash','data_transfer')}
    displays={name:(f"{statistics.mean(v):.1f}" if v else 'unavailable') for name,v in values.items()}
    lines.append(f"| {size//1024} GiB | {displays['source_hash']} | {displays['data_transfer']} |")
lines += ['',
'The first 4 GiB Croc sample reported roughly 44 s in source hashing and 131 s',
'in data transfer, versus a 178.44 s outer wall and 177.94 s sender process',
'lifetime. The extra source-file pass explains most of that sample’s full-time',
'difference. The data window remains fast. This does not establish a general',
'Croc regression or isolate the reason hashing was costly on this host.',
'[Retained normal-output hints and log hashes](cohorts/no-debug-final-20261004/normal-croc-progress-observations.json).']
lines += ['',
'## No-debug and route verification','',
'The external launcher strips every `RUSTYTRANSFER_BENCH_*` and',
'`CROC_BENCH_*` variable, Rustytransfer metrics JSONL, custom Rust logging and',
'backtrace controls, and Go/Croc debug controls. It rejects debug/verbose/trace',
'CLI flags. Live process snapshots verify the actual executable, process group,',
'working directory, redacted argv and absence of those environment names.',
'No application profiling or metrics output was enabled for the final rows.','',
'Normal Rustytransfer can initially select an Iroh relay and later upgrade to',
'direct UDP. Removing the old `BENCH_WAIT_DIRECT` control exposed this behavior;',
'the final measurement preserves it. Both endpoints have two kernel-filtered',
'bulk-sized UDP header samples at nominal 3/10 seconds after receiver launch.',
'The samples verify the expected peer and live process-owned UDP port. They',
'prove direct bulk traffic at those observation times, rather than continuous',
'decrypted QUIC STREAM accounting or a pure-direct-from-start transfer. Initial',
'CLI path selections are retained separately in the raw evidence.','',
'Croc’s live socket evidence verifies all four established data connections',
'and the control connection on both actual endpoint processes, matching the',
'expected public peers and Oracle listener ports. Full tuples are retained;',
'NAT source ports are allowed to differ between the two observations.','',
f"All **{audit['accepted_transfers']} transfers / {audit['canonical_endpoint_rows']} endpoint rows** passed full received-size/SHA-256",
'checks, live no-debug checks and the stated route gates. Every temporary Croc',
'client `/32` lease restored the exact original INPUT chain. Final independent',
'cleanup verifies no endpoints, listeners or leases remain.','',
'[Audited statistics](cohorts/no-debug-final-20261004/audit-report.json),',
'[raw endpoint rows](cohorts/no-debug-final-20261004/raw.jsonl),',
'[configuration and hashes](cohorts/no-debug-final-20261004/manifest.json),',
'[schedule](cohorts/no-debug-final-20261004/schedule.json),',
'[final cleanup](control/no-debug-final-cleanup-20261004.json).','',
'## Superseded diagnostics and smoke tests','',
'The earlier debug-enabled 1/2 GiB phase cohorts and 4 GiB warmups were stopped',
'at a completed-transfer boundary when the user requested this clean final',
'comparison. Their valid slow samples are retained; they supply no final clean',
'score. The 4 GiB diagnostic data consists only of one unscored warmup per tool.',
'Exact stock Croc startup milestones are retained as semantic events, not an',
'additive payload/completion phase decomposition.','',
'The clean 512 MiB smoke cohort has one warmup and one measured run per tool.',
'It validates the harness and remains outside the large-file scored cohort.',
'Earlier smoke attempts exposed an unsupported Rust CLI `--version` check, an',
'overly strict initial-direct route gate, and an external packet observer issue.',
'The observer now validates received headers again and skips packets outside',
'the peer/port/size criteria, including packets potentially queued before its',
'kernel filter was attached. Original helpers, failed logs and corrected',
'observer provenance are retained. Application binaries were unchanged.','',
'[Superseded phase audit](superseded/large-phase-20261004/audit-report.json)',
'retains the diagnostic mean sender payload rates of 30.624 MiB/s at 1 GiB',
'and 30.137 MiB/s at 2 GiB, individually against their contemporaneous Croc',
'full-transfer means of 27.905 and 27.506 MiB/s. Those phase windows come from',
'the earlier instrumented cohort and cannot be assigned to the clean runs.','',
'These sequential WAN cohorts do not isolate the cost of toggling Croc debug',
'logging. Different time windows, readiness observation and normal Rust route',
'startup behavior also differ. No permanent regression, debug-cost attribution',
'or production speedup is inferred from comparing them.','',
'The user accepts a 6–10% gap for now. Further tuning is deferred; the measured',
'defaults remain unchanged. A separate source-built Croc phase diagnostic',
'workflow was prepared, but was not executed or substituted into this final',
'official-binary comparison.','',
'## Provenance and reproduction','',
'[Rustytransfer release manifest](provenance/rustytransfer-release-manifest.json)',
'pins the actual binaries and source archive. The already retained',
'[source archive and build logs](../oracle-20261004-completion/builds/completion-v1-20261004/)',
'show optimized Cargo release builds. [Official Croc assets](provenance/croc-release-manifest.json)',
'are byte-pinned. Frozen harness imports and both observer versions are retained',
'under `reproduction/frozen-tools/`, along with launcher/auditor/test sources.',
'The artifact inventory verifies exact bytes; diagnostic logs are retained',
'locally under `logs/` and excluded from Git by repository policy.','']
(root/'README.md').write_text('\n'.join(lines),encoding='utf-8')
acceptance=dict(clean_cohort_audit=audit,comparisons=comparisons,
    production_default_changed=False,additional_tuning_deferred=True,
    source_built_croc_used_in_final=False,phase_timings_available=False,
    exact_application_phase_timings_available=False,normal_croc_progress_hints_available=True)
(root/'acceptance.json').write_text(json.dumps(acceptance,indent=2)+'\n')
print(json.dumps(comparisons,indent=2))
