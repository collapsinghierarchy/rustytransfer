# Final comparison without debug flags — 2026-10-04

User-requested final measurement with all application debug and benchmark
controls disabled. Overall runtimes and rates are reported at the user’s
explicit request; the prior diagnostic phase policy remains separate. No
production performance default was changed by these measurements.

Oracle ARM sender → native WSL x86 receiver. At each size: one unscored
warmup per tool and five alternating measured runs per tool, sequential.
Rustytransfer uses the frozen optimized release binary, one QUIC connection,
one stream, the normal invite flow and the unchanged 256 KiB chunk default.
Croc uses the official pinned 11.5.4 release assets, direct local mode, four
TCP data connections and compression disabled. Native incompressible input
fixtures are identical on both hosts.

| Size | Rustytransfer mean seconds | Croc mean seconds | Rustytransfer mean MiB/s | Croc mean MiB/s | Rustytransfer rate difference vs Croc |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 GiB | 37.53 | 34.90 | 27.34 | 29.34 | -6.84% |
| 2 GiB | 71.99 | 68.13 | 28.46 | 30.06 | -5.32% |
| 4 GiB | 135.88 | 177.28 | 30.14 | 23.12 | +30.41% |

Rate means average the five per-run `file MiB / full wall seconds` rates;
mean durations are averaged separately and are not reciprocals of those
mean rates. A positive rate difference means Rustytransfer was faster in
this cohort. Full wall time begins at the first endpoint launch and ends
when both endpoint subprocesses have exited. It includes SSH, readiness and
concurrent external observation work. Independent exit watchers prevent an
observer poll or post-exit verification from extending that timer. External
fixture and received-file hashing stays outside it. Valid slow samples remain
included. Exact instrumented phase durations are unavailable in this clean cohort.

## Spread and resources

| Size | Tool | Median MiB/s | Range MiB/s | MAD MiB/s | Sender CPU s/GiB, median | Receiver CPU s/GiB, median |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 GiB | rustytransfer | 27.64 | 25.16–28.35 | 0.38 | 13.09 | 14.26 |
| 1 GiB | croc | 29.40 | 29.14–29.49 | 0.09 | 3.08 | 11.97 |
| 2 GiB | rustytransfer | 28.44 | 27.64–29.27 | 0.63 | 13.12 | 14.30 |
| 2 GiB | croc | 30.06 | 30.04–30.08 | 0.01 | 2.97 | 11.95 |
| 4 GiB | rustytransfer | 30.12 | 29.97–30.29 | 0.15 | 13.78 | 14.26 |
| 4 GiB | croc | 23.30 | 22.23–23.66 | 0.35 | 4.13 | 11.94 |

## Coarse Croc timings from normal output

The official binary’s ordinary progress bars show separate source-file hashing
and data-transfer elapsed values without debug logging. The table averages
each measured run’s last reported elapsed hint. Values are rounded/stale display
hints, rather than exact additive phase boundaries or instrumentation timers.

| Size | Source hashing elapsed hint, mean seconds | Data transfer elapsed hint, mean seconds |
| --- | ---: | ---: |
| 1 GiB | 0.0 | 31.2 |
| 2 GiB | 1.0 | 64.0 |
| 4 GiB | 43.4 | 130.4 |

The 1 GiB zero-second hashing display does not prove that hashing was free;
its last visible update was rounded and stale.

The first 4 GiB Croc sample reported roughly 44 s in source hashing and 131 s
in data transfer, versus a 178.44 s outer wall and 177.94 s sender process
lifetime. The extra source-file pass explains most of that sample’s full-time
difference. The data window remains fast. This does not establish a general
Croc regression or isolate the reason hashing was costly on this host.
[Retained normal-output hints and log hashes](cohorts/no-debug-final-20261004/normal-croc-progress-observations.json).

## No-debug and route verification

The external launcher strips every `RUSTYTRANSFER_BENCH_*` and
`CROC_BENCH_*` variable, Rustytransfer metrics JSONL, custom Rust logging and
backtrace controls, and Go/Croc debug controls. It rejects debug/verbose/trace
CLI flags. Live process snapshots verify the actual executable, process group,
working directory, redacted argv and absence of those environment names.
No application profiling or metrics output was enabled for the final rows.

Normal Rustytransfer can initially select an Iroh relay and later upgrade to
direct UDP. Removing the old `BENCH_WAIT_DIRECT` control exposed this behavior;
the final measurement preserves it. Both endpoints have two kernel-filtered
bulk-sized UDP header samples at nominal 3/10 seconds after receiver launch.
The samples verify the expected peer and live process-owned UDP port. They
prove direct bulk traffic at those observation times, rather than continuous
decrypted QUIC STREAM accounting or a pure-direct-from-start transfer. Initial
CLI path selections are retained separately in the raw evidence.

Croc’s live socket evidence verifies all four established data connections
and the control connection on both actual endpoint processes, matching the
expected public peers and Oracle listener ports. Full tuples are retained;
NAT source ports are allowed to differ between the two observations.

All **36 transfers / 72 endpoint rows** passed full received-size/SHA-256
checks, live no-debug checks and the stated route gates. Every temporary Croc
client `/32` lease restored the exact original INPUT chain. Final independent
cleanup verifies no endpoints, listeners or leases remain.

[Audited statistics](cohorts/no-debug-final-20261004/audit-report.json),
[raw endpoint rows](cohorts/no-debug-final-20261004/raw.jsonl),
[configuration and hashes](cohorts/no-debug-final-20261004/manifest.json),
[schedule](cohorts/no-debug-final-20261004/schedule.json),
[final cleanup](control/no-debug-final-cleanup-20261004.json).

## Superseded diagnostics and smoke tests

The earlier debug-enabled 1/2 GiB phase cohorts and 4 GiB warmups were stopped
at a completed-transfer boundary when the user requested this clean final
comparison. Their valid slow samples are retained; they supply no final clean
score. The 4 GiB diagnostic data consists only of one unscored warmup per tool.
Exact stock Croc startup milestones are retained as semantic events, not an
additive payload/completion phase decomposition.

The clean 512 MiB smoke cohort has one warmup and one measured run per tool.
It validates the harness and remains outside the large-file scored cohort.
Earlier smoke attempts exposed an unsupported Rust CLI `--version` check, an
overly strict initial-direct route gate, and an external packet observer issue.
The observer now validates received headers again and skips packets outside
the peer/port/size criteria, including packets potentially queued before its
kernel filter was attached. Original helpers, failed logs and corrected
observer provenance are retained. Application binaries were unchanged.

[Superseded phase audit](../../archives/README.md "Archive member: benchmarks/results/oracle-20261004-no-debug/superseded/large-phase-20261004/audit-report.json")
retains the diagnostic mean sender payload rates of 30.624 MiB/s at 1 GiB
and 30.137 MiB/s at 2 GiB, individually against their contemporaneous Croc
full-transfer means of 27.905 and 27.506 MiB/s. Those phase windows come from
the earlier instrumented cohort and cannot be assigned to the clean runs.

These sequential WAN cohorts do not isolate the cost of toggling Croc debug
logging. Different time windows, readiness observation and normal Rust route
startup behavior also differ. No permanent regression, debug-cost attribution
or production speedup is inferred from comparing them.

The user accepts a 6–10% gap for now. Further tuning is deferred; the measured
defaults remain unchanged. A separate source-built Croc phase diagnostic
workflow was prepared, but was not executed or substituted into this final
official-binary comparison.

## Provenance and reproduction

[Rustytransfer release manifest](provenance/rustytransfer-release-manifest.json)
pins the actual binaries and source archive. The already retained
[source archive and build logs](../oracle-20261004-completion/builds/completion-v1-20261004/)
show optimized Cargo release builds. [Official Croc assets](provenance/croc-release-manifest.json)
are byte-pinned. Frozen harness imports and both observer versions are retained
under `reproduction/frozen-tools/`, along with launcher/auditor/test sources.
The artifact inventory verifies exact bytes; diagnostic logs are retained
locally under `logs/` and excluded from Git by repository policy.

For reproduction, the saved [launcher command](control/no-debug-final-20261004.launcher.json)
specifies every endpoint, binary hash and input path. Its frozen five-file
harness is in `reproduction/frozen-tools/harness-final/`. Restore those files
to a native Linux directory, the observer to the recorded local/shared/remote
paths, and the pinned binaries and fixtures to the paths in the manifest.
Choose a new output directory before running the recorded command. WSL root
packet capture, Oracle SSH access, and the bounded firewall lease helper are
required. No new diagnostic cohort is needed to inspect the retained results.
