# Independent QUIC connection evaluation - 2026-10-04

Four independent QUIC connections did not improve throughput in this 512 MiB
Oracle-to-WSL screen. They increased CPU and memory versus one connection/one
stream. The experiment retains one application ML-KEM exchange and the same
AES-256-GCM payload key across all streams and connections in each file transfer.
Production behavior remains unchanged.

| Layout | Median MiB/s (range; MAD) | Sender / receiver CPU s/GiB | Sender / receiver peak RSS KiB | Median wall s |
| --- | ---: | ---: | ---: | ---: |
| 1 connection, 1 stream | 27.374 (27.074-27.731; 0.240) | 11.88 / 10.48 | 24,624 / 23,116 | 18.704 |
| 1 connection, 4 streams | 27.223 (26.464-27.439; 0.216) | 14.42 / 15.38 | 36,836 / 29,588 | 18.807 |
| 4 connections, 4 streams | 27.252 (27.134-27.468; 0.114) | 15.36 / 14.50 | 37,904 / 30,928 | 18.787 |

Four connections changed median throughput **-0.45%** against the one-stream
baseline and **+0.11%** against the matched four-stream control. Versus the
baseline, sender/receiver CPU increased **29.3%/38.4%**, and peak RSS increased
**53.9%/33.8%**. Versus the four-stream control, CPU changed **+6.5%/-5.7%** and
RSS **+2.9%/+4.5%**. The candidate won only one of five paired rate comparisons
against the baseline and three of five against the four-stream control.
Paired median rate changes were -0.70% and +0.10%, respectively.

Neither comparison meets the preregistered >=5% throughput or >=10% CPU/GiB
threshold. The failed representative screen ends acceptance work: there is no
confirmation series, scored guard series, production parallel architecture,
or resume implementation. Keeping the four-stream control matches file tasks
and buffers when testing whether separate connections help; it is not a new
proposal to adopt four streams. The earlier `/1` screen remains historical.

Sender handshake/payload/shutdown medians were 1.493/16.624/0.047 seconds for
one connection/one stream, 1.451/16.687/0.163 for one connection/four streams,
and 1.516/16.703/0.049 for four connections/four streams. These phase medians
need not sum to the median full-process wall interval. Concurrent direct
readiness avoids imposing four serial 500 ms waits; actual setup remains timed.

## Measurement and provenance

All three layouts used the same frozen `shared-key-parallel/2` example binary
pair, source commit `d703bcdbd137a6f4848fe6f274ed33e4db670ab2`, and 256 KiB chunks.
Each layout had one unscored warmup and five measured transfers in rotated,
alternating order. Rates use the complete timed wall interval. CPU and RSS come
from separate endpoint processes; full-file hashes, fixture checks and binary
hashes are outside timers. Fixtures are the existing native ext4 incompressible
512 MiB files. No tests, builds, fixture generation or external hashing overlap
the scored transfers. All valid samples, including slow samples, are retained.

| Artifact | SHA-256 |
| --- | --- |
| Source archive | `73e82145ffa28d5bcf8c01d78f50ed3ec48ee4922792a5039b052c016cae4920` |
| x86 executable | `940fa0b98c191745c368aa46ad9a09b32d4e6fdab902e7f162d830e1b77f7e62` |
| ARM executable | `81de710316bb3d49360dbb4b5dc197bc36cecdc398443532f9a538c9ae467e2d` |

[Build provenance and source archive](builds/independent-connections-v2-20261004/manifest.json)
identify Rust 1.97.0 on both architectures and the exact input source bytes.
The raw dirty fields are preserved: Windows and WSL Git differ in their view
of line endings, and unrelated user edits remain present. The
[source audit](provenance/source-audit.json) verifies every frozen source file
against the final workspace and unchanged production source bytes against the
previous frozen build; source hashes are authoritative.

All **18 transfers / 36 endpoint rows** passed full received size/SHA-256 and
strict direct payload-route proof. Each endpoint recorded exactly the requested
number of distinct QUIC stable IDs, sorted lane indices, common endpoint IDs,
and per-connection native STREAM-frame evidence. Sender/receiver identities
were cross-checked for every lane. Primary evidence alone cannot accept a run:
relay, missing, lagged, duplicate or mismatched secondary evidence is rejected.
The control connection carries lane zero, so the connection counts are exactly
one or four, rather than one plus four.

The [complete cohort audit](cohorts/independent-connections-screen-20261004/audit-report.json),
[comparisons](cohorts/independent-connections-screen-20261004/comparisons.json),
raw variant `rows.jsonl` files, and complete schedule preserve all measurements.
Payload profiling and window experiments are disabled. Both endpoints observe
every connection before the final READY/observer-ready/GO barrier; evidence
covers that barrier, data headers and each endpoint's stream completion, then
ends before FIN/ACK. Setup and lane-binding messages precede this window.

## Correctness and checks

One application KEM encapsulation/decapsulation supplies a shared payload key.
Each extra QUIC connection performs its normal transport handshake, verifies
the same peer identity, and proves its assigned lane with an AEAD challenge and
response bound to version, session, manifest digest and disjoint chunk range.
Binding request/response counters use a separate nonce prefix from payload
global chunk counters. Duplicate lanes are rejected before response encryption.
All file tasks use bounded chunk buffers and disjoint file offsets. A shared
absolute deadline bounds bootstrap, control and data operations; JoinSet errors,
signals and expiry abort and join siblings before owned partial cleanup.
Connection guards close accepted/dialed connections on bootstrap failure.

The required nine repository commands, Clippy SARIF identity/report checks,
workspace tests, WASM check, dependency policy, **56 Python benchmark tests**,
and **16 example tests** pass. Workspace Clippy retains exactly its three
existing findings; the example introduces none. Local checks passed all three
8 MiB layouts with full hashes and per-connection proof, a 64 KiB four-connection
case with empty ranges, source truncation, SIGTERM during initial accept, and
SIGTERM during payload at either endpoint. Interrupted transfers left no output
or owned partial. No-overwrite, AAD/nonce/duplicate checks and sibling deadline
drain are also covered by focused tests. Partial bootstrap closure is a source
audit; the physical interruption checks cover initial accept and active payload.

The initial Windows-mounted-target release build failed with a temporary archive
permission error. Its [failed log](checks/shared-key-connections-20261004/release-build.log)
is retained; the native Linux build and final required checks passed. It is not
a failed scored transfer. All local correctness checks passed on their first
attempt. Per-run endpoint logs remain local under ignored `logs/` directories;
the inventory retains their hashes and marks them as local-only. Structured rows,
source archives, build/check logs and reproduction helpers are versioned. No
fixtures, executables, identity private keys or full invites are exported.

## Decision and limits

This is a negative screen for this prototype, size, direction and endpoint pair.
It establishes no repeatable benefit from separate connections here, and does
not support attributing Croc's earlier advantage simply to its connection count.
It does not prove a general QUIC limit, global tuning optimality, or behavior in
the opposite direction or different network conditions. The example's separate
chunk buffers, framing and finalization differ from production; these rates
cannot be compared directly with historical production/Croc cohorts as a speed
improvement. Production framing, 256 KiB default and resume remain unchanged.

[Acceptance](acceptance.json) records thresholds and unrun followups.
[Final cleanup](cleanup/final-independent-connections-cleanup-20261004.json)
independently verifies no benchmark endpoints, Croc listeners or leases and the
exact original Oracle INPUT-chain hash. No Croc ingress lease was needed; OCI
settings were untouched. Desktop/device work remains stopped, pending release
artifacts remain intact, and unrelated user edits are preserved.

The [preregistered plan](reproduction/independent-connections-plan.md) and exact
reproduction scripts are retained. Run preparation, builds and correctness
checks outside timed intervals; use fresh names/directories and the authorized
Oracle/WSL locations. Source archives rebuild the example without relying on the
current checkout. `artifact-inventory.json` hashes all retained bytes, with
`.gitattributes` preventing newline conversion of raw evidence.
