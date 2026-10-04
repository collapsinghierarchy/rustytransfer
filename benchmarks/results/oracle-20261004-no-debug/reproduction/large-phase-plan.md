# Larger-file phase reference and Croc diagnosis — 2026-10-04

User-authorized 1/2/4 GiB (1024/2048/4096 MiB) extension, Oracle ARM sender to
WSL x86 receiver, identical native fixtures and explicit native non-Git cwd.
Use the exact frozen completion-v1 Rustytransfer binaries and official pinned
Croc 11.5.4 assets. Keep one Rust connection/stream, 256 KiB chunks, invite
pairing, and four Croc TCP data channels. All profiling and close/window
experiments disabled in the stock reference cohort.

One warmup plus five alternating measured runs per tool and size, sequential.
Full-file size/SHA-256 and both-endpoint route verification mandatory; retain
all valid slow samples. No build/test/generation/external hash jobs during
timed transfers. Existing per-run source/output verification stays outside
the timer. Every Croc transfer gets a bounded client /32 lease (1200 s), exact
INPUT restoration, and isolated process/output cleanup.

Report arithmetic mean of per-run Rustytransfer phase rates versus arithmetic
mean of Croc full-transfer rates, with phase duration means/ranges and sender
and receiver payload views. Never compare the overall Rustytransfer rate to
Croc. Overhead normalized rates are indices, not wire throughput. Endpoint
durations overlap, and Croc/Rust phase boundaries may differ.

Additional user request: obtain Croc phases using an instrumented source build
of the same tag. Preserve official assets as the reference. Retain exact patch,
source/toolchain/binary provenance and semantic boundaries. Measure enabled
versus disabled on the same source-built pair before interpreting diagnostic
phase rates. The diagnostic build cannot be silently substituted for stock.
No production performance change is accepted by a diagnostic comparison.

Only after all builds/preparation are finished will timing begin. Hardware/WAN
variation remains a limitation; startup and completion need separate profiling
before optimization decisions. Mandatory Rustytransfer final durability stays.

## User-requested overall snapshot during the run

After the phase-only policy, the user explicitly asked for overall runtimes
and throughputs of both products. Provide that requested view separately,
with completed 1 GiB (five runs/tool) and provisional 2 GiB (three runs/tool)
means. Preserve this snapshot and its sample counts; do not treat provisional
values as the final cohort. The primary final investigation still uses phase
decomposition. No automatic overall ranking/ratio is added by the collector.

## Superseding final comparison: no debug flags or application profiling

The user explicitly requested a final measurement without any debug flags for
either product. Stop the previous debug-enabled reference at a completed
transfer boundary and retain its completed 1/2 GiB cohorts and unscored 4 GiB
warmups. They are diagnostic evidence, not the final comparison.

Use the same frozen Rustytransfer release binaries and official Croc 11.5.4
assets, with one warmup and five alternating measured runs per tool at each
of 1024, 2048 and 4096 MiB. No Croc --debug, no Rustytransfer benchmark or
metrics environment variables, no custom Rust log controls. Verify live
endpoint argv/environment through the external observer. Report the requested
full runtimes and arithmetic means of per-run full-transfer rates. Application
phase durations are unavailable in this cohort and must not be inferred.

Validate full received-file hashes outside the timer. Croc route evidence uses
actual process-owned established TCP connections to the four data ports, with
both peer addresses checked and NAT tuples retained. Rustytransfer evidence
uses normal CLI direct-path reports plus two kernel-filtered bulk UDP header
samples per endpoint; this is sampled route coverage, not continuous decrypted
QUIC STREAM accounting. Preserve this limitation in the final report.

The timer runs from the first endpoint launch until both endpoint exits,
including readiness and external observation work. Keep source-built Croc
phase diagnostics separate from this official-binary comparison. Freeze
harness dependencies and observer hashes before timing, run a smaller clean
smoke test first, and retain failures rather than silently replacing samples.

The clean smoke exposed an assumption in the route gate: without the old
BENCH_WAIT_DIRECT control, the normal Rustytransfer CLI initially reports relay
and upgrades to direct bulk UDP traffic during the transfer. Preserve that
normal operation, record the initial selected path, and require both direct
bulk samples on each endpoint. Label this normal invite operation with sampled
direct bulk evidence, rather than a pure-direct-from-start cohort. No hidden
route controls or production changes are permitted in the final comparison.
Two preliminary smoke attempts are retained separately: an unsupported CLI
--version preflight check and the overly strict initial-route gate. All smoke
attempts remain outside final scored results by design.
