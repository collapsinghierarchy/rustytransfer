# Completion diagnostics and Croc phase reference — 2026-10-04

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

### 64 MiB — Croc full-transfer mean: 18.656 MiB/s

| Rustytransfer sender phase | Mean seconds (range) | Mean per-run phase rate, MiB/s | Rate / Croc full mean |
| --- | ---: | ---: | ---: |
| Setup + handshake | 0.897 (0.850–0.933) | 71.463 | 3.831× |
| Payload | 2.092 (2.088–2.102) | 30.589 | 1.640× |
| Completion | 0.384 (0.325–0.408) | 167.638 | 8.986× |
| Remaining outer time | 0.445 (0.405–0.478) | 144.427 | 7.742× |

### 512 MiB — Croc full-transfer mean: 28.826 MiB/s

| Rustytransfer sender phase | Mean seconds (range) | Mean per-run phase rate, MiB/s | Rate / Croc full mean |
| --- | ---: | ---: | ---: |
| Setup + handshake | 0.925 (0.858–0.986) | 554.892 | 19.249× |
| Payload | 17.812 (16.556–22.793) | 29.216 | 1.014× |
| Completion | 2.930 (0.594–4.097) | 290.650 | 10.083× |
| Remaining outer time | 0.445 (0.439–0.450) | 1151.287 | 39.939× |

| Rustytransfer payload phase | Mean MiB/s | Croc full mean MiB/s | Phase / reference |
| --- | ---: | ---: | ---: |
| 64 MiB sender payload | 30.589 | 18.656 | 1.640× |
| 64 MiB receiver payload | 30.180 | 18.656 | 1.618× |
| 512 MiB sender payload | 29.216 | 28.826 | 1.014× |
| 512 MiB receiver payload | 29.172 | 28.826 | 1.012× |

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
| Baseline | 25.391 (21.723–26.801; 1.410) | 12.90 / 14.14 |
| Explicit close | 23.827 (21.685–26.914; 2.143) | 13.04 / 14.36 |

Median rates changed -6.16%; paired median rate
change was 0.42%, with
3/5 paired wins. CPU did not improve. Neither >=5% repeatable
rate nor >=10% CPU/GiB saving passed. This is not a universal slowdown claim;
the failed screen ends acceptance, with no reversed confirmation or scored
direction/startup/resume guard campaign. Default close remains unchanged.
All 12 transfers / 24 rows passed hashes, size, strict direct evidence and
requested/applied-mode plus span-bound checks.

Baseline receiver commit median was **1.788 s**
(range 1.016–3.726); receiver endpoint close median
was **0.032 s** (range 0.023–1.388).
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
timestamp was **3.125 s** in the checkout versus
**0.003 s** natively. This tail includes metric emission and
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

Measured frozen source is based on `889ef41232399aa5151f8c14bae98f475df55600` with uncommitted
instrumentation archived exactly; implementation commit is `4e807a8`.

| Artifact | SHA-256 |
| --- | --- |
| Source archive | `8999e8ab97f53d54f1e538db3f9564b5681b74f77cc6b92fe6a6f21f2d7a3b39` |
| x86 executable | `48ef6ccd810721285af34d5787d61a00a9bef3a63f82c885c0799c775a41598c` |
| ARM executable | `c325bf4a2a58b078ea20928bbea2e446fe9c22d907f8d459e9c728c25ef1fba9` |

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
[partial audit](../../archives/README.md "Archive member: benchmarks/results/oracle-20261004-completion/interrupted/completion-croc-20261004/partial-audit.json"), redacted
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
