# Direct transfer efficiency report - updated 2026-10-04

The ARM efficiency milestone is complete. A small Cargo configuration enables the
pinned AES/POLYVAL crates' runtime ARM acceleration on Linux/macOS AArch64, with
software fallback. Encryption, nonce rules, payload framing, and chunk sizes are
unchanged. Release CI now includes Cargo configuration changes in its path filter.
Desktop and device work is stopped; unvalidated drafts remain in the separate
`feature/desktop-draft` worktree. The submitted Firefox extension and pending
0.1.0 artifacts were not replaced.

The [matched ARM experiment](../benchmarks/results/oracle-20261003-arm-crypto/README.md)
contains 80 measured direct transfers across two reversed-order series, both
directions, and 64/512 MiB fixtures. Every accepted transfer passed full size/hash
checks and direct payload-route verification at both endpoints. Pooled 512 MiB
Oracle CPU cost fell **54.3% when sending** (27.5 to 12.6 CPU seconds/GiB) and
**23.7% when receiving** (63.9 to 48.8). WAN throughput was broadly similar;
there is no general speed-improvement claim. Peak Oracle RSS remained around
30 MiB with overlapping variation.

The first forward 64 MiB series was 3.217% slower, crossing the plan's guardrail.
The additional reversed-order series was 0.376% faster; pooled ten-trial medians
were 0.540% faster. This is an explicit, measured acceptance exception in
[acceptance.json](../benchmarks/results/oracle-20261003-arm-crypto/acceptance.json),
with the original crossing retained and no asserted cause for the variation.

Ten small-file trials per variant showed no meaningful startup regression:
forward 4 KiB medians were 3.0606/3.0604 seconds, and reverse 64 KiB medians
3.9311/3.9346 seconds (baseline/candidate). A reverse 4 KiB warmup lacked receiver
STREAM-frame evidence and was excluded without weakening the route check.
A real interrupted 512 MiB transfer resumed its verified 149.25 MiB prefix,
sent only the remaining 362.75 MiB, and passed complete SHA-256/direct-route
checks on current-source builds. Resume is correctness evidence, not a scored
full-file throughput sample.

The ARM milestone comparison uses its frozen Rustytransfer builds and official Croc 11.5.4
release binaries, with identical incompressible fixtures, one warmup and five
alternating measured transfers per tool/size. These are **Oracle-to-WSL direct
payload transfers only**; rates include process startup and completion.

| File | Rustytransfer median MiB/s | Croc direct median MiB/s | Croc rate advantage |
| --- | ---: | ---: | ---: |
| 64 MiB | 12.48 | 19.28 | 54.5% |
| 512 MiB | 23.02 | 28.87 | 25.4% |

The subsequent larger-file followups and a fresh 512 MiB control used the exact
same frozen binaries. Each cohort has one warmup per tool and five alternating
measured pairs; all twelve transfers passed direct-route and full-file checks.
Sizes use binary units: 1 GiB is 1,073,741,824 bytes and 2 GiB is 2,147,483,648.

| File/cohort | Rustytransfer median MiB/s | Croc direct median MiB/s | Croc rate advantage | Median wall seconds, Rustytransfer/Croc |
| --- | ---: | ---: | ---: | ---: |
| [512 MiB fresh control](../benchmarks/results/oracle-20261003-croc-direct-512-control/README.md) | 25.18 | 28.74 | 14.2% | 20.34 / 17.81 |
| [1 GiB](../benchmarks/results/oracle-20261003-croc-direct-1g/README.md) | 27.59 | 29.30 | 6.2% | 37.11 / 34.95 |
| [2 GiB](../benchmarks/results/oracle-20261003-croc-direct-2g/README.md) | 28.42 | 30.17 | 6.1% | 72.05 / 67.88 |

Rate advantage means `(Croc median / Rustytransfer median - 1) * 100`; it is
not the percentage reduction in completion time. At 2 GiB the difference
between median completion times is 4.17 seconds. The unrounded rate gap changed
from 6.18% at 1 GiB to 6.14% at 2 GiB: effectively a plateau, not convincing
confirmation of continued narrowing. The 2 GiB individual rates ranged from
28.08-29.52 MiB/s for Rustytransfer and 29.21-30.20 for Croc; respective MADs
were 0.35 and 0.03 MiB/s. All five paired Croc runs were faster at 2 GiB; two
were slower at 1 GiB and remain included.

The earlier 512 MiB gap was 25.4%, whereas the fresh control measured 14.2%.
That change at the same size demonstrates that payload size is not the only
variable. Cohorts ran sequentially in 1 GiB, fresh 512 MiB, then 2 GiB order;
they do not isolate size from WAN variation, cache state, or the time window.
Fixed setup costs become a smaller fraction of larger transfers and are one
plausible explanation for narrowing, but these data do not fit or prove that
model. No valid performance outlier was discarded.

At 2 GiB, median Oracle sender CPU was 26.23 seconds for Rustytransfer and 6.80
for Croc, or 13.12 versus 3.40 CPU seconds/GiB. Receiver CPU was 27.99 versus
22.76 seconds; peak-RSS medians were 30,464/24,692 KiB on the sender and
25,468/25,856 KiB on the receiver. The much larger sender CPU gap remains
despite close throughput. Per-cohort audit reports preserve ranges/MAD,
executable/fixture provenance, route excerpts, canonical rows, and cleanup.

The fresh 512 MiB harness completed all valid transfers but failed during
firewall teardown because its stop check expected three process arguments
instead of the configured four. A separately audited recovery matched the
unique rule, PID, cwd, exact arguments, and client /32 before stopping that
lease. The original INPUT-chain hash was restored exactly. A separate cleanup
check passed before 2 GiB; the 2 GiB job completed with normal teardown. All
cohorts have independent post-run checks showing no benchmark endpoints or
Croc listeners, with OCI settings unchanged.

Croc had higher median rates and used less sender CPU on this endpoint pair. It used four TCP data
channels directly into its embedded listener on the Oracle sender; Rustytransfer
used Iroh/QUIC. The comparison does not isolate the cause of the difference or
establish universal transport optimality. Croc's global `--local`, `send
--transport auto`, and receiver `--ip 141.147.1.21:9009` prevent a third-party
payload relay; actual endpoint connection logs were independently rechecked.
See the [final comparison record](../benchmarks/results/oracle-20261003-arm-crypto/final-validation/README.md)
for provenance, CPU/RSS, ranges/MAD, route excerpts, startup/resume records, and
limitations. OCI already allowed TCP 9009-9013. A temporary instance rule allowed
only this client's IPv4 /32; the original INPUT chain was restored exactly, and
no benchmark endpoints or Croc listeners remained.

Validation passed: workspace tests, formatting, wasm check, the existing
Clippy/SARIF identity/report gates with three existing findings, cargo-deny,
44 Python benchmark tests, and 14 Oracle crypto tests with ARM dispatch enabled.
Matched A/B results use frozen source `8f7d8e8`; current `d654b0b` plus the Cargo
configuration is separately validated by the final comparison and resume check.
No physical LAN, native Windows, macOS performance, or physical ARM software
fallback measurement is claimed. Oracle permissions prevented CPU `perf`
sampling; stage diagnostics measure elapsed time.

The accepted optimization enables hardware AES and authentication-field
arithmetic in the pinned RustCrypto dependencies through five Cargo config
lines. It is runtime dispatched on supported Linux/macOS ARM64 processors,
retains software fallback, and does not weaken encryption or change the wire
protocol. The matched experiment establishes a reduction in CPU cost, not a
corresponding increase in network speed. Removing CPU work helps efficiency
even when another resource determines completion time.

The Croc comparison identifies architectural differences, but it does not
identify one proven cause of the remaining gap. Croc's verified four TCP data
connections can distribute file work across independent network flows;
Rustytransfer's [Iroh adapter](../crates/native/src/transport/iroh.rs) carries the
ordered payload on one bidirectional QUIC stream. Different flow/congestion
behavior is a plausible contributor. Four streams on one QUIC connection would
still share its path's congestion budget; they do not reproduce four independent
TCP connections. The [QUIC recovery specification](https://www.rfc-editor.org/rfc/rfc9002.html#section-7)
describes congestion control at the packet/path level, and
[Iroh's stream documentation](https://docs.rs/iroh/1.2.0/iroh/endpoint/struct.Connection.html#method.open_uni)
describes multiplexing streams within a connection.

Rustytransfer encrypts each application chunk with AES-256-GCM and then sends
it through QUIC's encrypted transport. The compared Croc path uses encrypted
application messages over plain TCP sockets, as shown by its
[cryptography](https://github.com/schollz/croc/blob/v11.5.4/src/crypt/crypt.go) and
[TCP implementation](https://github.com/schollz/croc/blob/v11.5.4/src/comm/comm.go).
This gives Rustytransfer additional transport processing. The sender also
copies its reusable read buffer into a newly allocated owned chunk, then seals
in place and awaits a send; the receiver decrypts and writes each chunk before
receiving the next. These are concrete places to investigate, but their share
of the current gap has not been measured. The Iroh flush call is a no-op, so
removing it is not a credible large optimization; awaiting a stream write does
not mean waiting one network round trip per chunk.

The [earlier stage profile](../benchmarks/results/oracle-20261002-payload-profile/README.md)
measured 10.010 seconds of allocation/copy/encryption and 6.379 seconds of send
wait in a 512 MiB Oracle-to-WSL payload. It predates the ARM acceleration and
combines three operations; it cannot quantify today's copy or crypto bottleneck.
In the opposite direction, send waits dominated instead. Post-optimization
profiles and transport statistics are needed before attributing the current
gap to CPU, disk, flow control, or packet loss. Large-file sender CPU/wall ratios
also do not show the Oracle process continuously using its entire CPU.

At that milestone, the following experiments were proposed. The continuation
below records their measurements and decisions; this table describes the
original hypotheses rather than accepted performance changes.

| Priority | Experiment | Likely value and architectural scope |
| --- | --- | --- |
| 1 | Refresh opt-in stage profiles and record QUIC RTT, loss, congestion state, and send stalls; then test bounded transport-window changes only if the evidence shows a limit. | Best chance of a substantial throughput gain with a small transport-local change if flow control is the bottleneck. No measured gain is promised. Preserve NAT traversal defaults and cap memory. |
| 2 | Sweep the existing CLI `--chunk-size` from 256 KiB to 512 KiB and 1 MiB. | Cheapest experiment, with no product code change. Larger chunks reduce per-message allocation, FSM, framing, and progress overhead. They do not enlarge QUIC packets or automatically increase the congestion window. |
| 3 | Read directly into the owned plaintext chunk passed to the sender FSM. | Removes the visible buffer-to-chunk copy while preserving the existing in-place encryption and protocol. Likely a CPU/allocation improvement; bandwidth upside is uncertain. Preserve short reads, tag capacity, truncation checks, and resume behavior. |
| 4 | If profiles show application gaps, test a small bounded prefetch/write pipeline. | Overlaps file work with transport waits. Keep encryption nonce order, bounded queues, cancellation, and output finalization intact. The existing small disk-stage times make a large storage-only gain unlikely on these hosts. |
| 5 | Experiment with multiple payload streams or connections only after the smaller tests. | Larger architectural change with potential throughput upside if flow behavior or scheduling is limiting. Requires authenticated offsets/order, unique nonce handling, bounded reassembly, resume and route-proof changes; extra streams alone do not multiply bandwidth. |

Iroh exposes [connection statistics and congestion state](https://docs.rs/iroh/1.2.0/iroh/endpoint/struct.Connection.html#method.stats)
and [transport configuration](https://docs.rs/iroh/1.2.0/iroh/endpoint/struct.QuicTransportConfig.html).
The latter supports tuning windows to bandwidth, RTT, and memory; its documented
100 Mbps/100 ms default tuning is not a 100 Mbps speed cap. Change windows only
after distinguishing flow-control blocking from congestion or application
starvation. Each accepted candidate needs alternating baseline/candidate trials,
direct payload proof, hashes, CPU/GiB and RSS, plus small-file and resume checks.
Reversing the size-cohort order would also help test whether the shrinking gap
persists independently of the time of day.

Matching Croc's large-file rates with Iroh is a plausible engineering target.
These measurements do not establish a fundamental QUIC/Iroh ceiling or require
replacing Iroh. They also cannot guarantee parity on this WAN, other machines,
or small files. The measured remaining throughput gap is modest; the sender CPU
gap is considerably larger and deserves its own efficiency target. Keep both
targets explicit and retain the current cryptographic and resume guarantees.

## Direct-transfer tuning continuation

The fresh profile separates allocation/copy from encryption and records sampled
Iroh connection statistics and send stalls only when payload profiling is
enabled. On the 512 MiB Oracle-to-WSL diagnostic transfer, allocation/copy took
0.0254 seconds, encryption 1.127 seconds, and send waits 15.139 seconds within
a 16.581-second payload interval. Sender CPU was 6.46 seconds. These elapsed
spans do not provide sampled CPU attribution, but the copy span is too small
to explain the historical sender CPU gap. File-read/write spans were also small;
the refreshed profile provides no application-starvation evidence that would
justify adding a prefetch/write pipeline.

The sender's RTT samples ranged from 14.741 to 39.611 ms, with a final congestion
window of 11,012,183 bytes, 16 lost packets, and four congestion events. Of
2,048 sends, 2,039 took more than 1 ms and 394 more than 10 ms. These observations
supported a bounded receive-window screen without proving flow-control blocking.
The pinned transport does not count transmitted DATA_BLOCKED/STREAM_DATA_BLOCKED
frames; zero counters cannot establish that blocking never occurred. Message-boundary
sampling also cannot quantify blocked duration or bytes in flight.

| Screen | Measured outcome | Decision |
| --- | --- | --- |
| Owned-chunk read, two reversed-order 512 MiB download series | Throughput changed +4.5% then -3.7%; sender CPU/GiB increased in both series. | Reject the runtime change; retain its patch and correctness evidence. |
| Stream receive window 2.5/5.0 MB, 512 MiB downloads | Throughput changed +2.6%/+1.5%; sender CPU and peak RSS increased. | Keep bounded opt-in research controls; preserve production defaults. |
| 512 KiB / 1 MiB chunks, both directions and 64/512 MiB | Most rate changes were small; 512 KiB regressed 14.6% on 512 MiB uploads. At 64 MiB upload, 1 MiB improved medians 6.5%/12.0% in two series, with only five of ten paired wins, wide variation, and higher RSS. | Retain the 256 KiB default; preserve the positive case-specific result and existing opt-in sizes. |

Acceptance requires a repeatable gain of at least 5% throughput or 10% CPU/GiB,
with no unexplained greater-than-3% rate regression in guard cases. Failing a
representative screen ends that candidate's acceptance work. Valid slow samples
remain included. These negative screens do not prove global optimality or a
fundamental Iroh throughput ceiling.

The 64 KiB small-file cohorts use ten measured trials per chunk size/direction.
Two attempted 4 KiB-upload groups stopped because the receiver observed zero
payload STREAM frames, despite complete hash checks and a verified direct sender.
They remain excluded diagnostic records; neither establishes a chunk-specific
failure or a proven relay route. The route check was not weakened. Production
short-read, truncation and interrupted-prefix resume tests cover all three chunk
sizes; the earlier physical 512 MiB resume remains baseline correctness evidence.

The isolated `shared-key-parallel/1` example compares one and four data streams
on one QUIC connection. One KEM exchange supplies the same AES-256-GCM payload
key to every stream. Disjoint global chunk counters prevent nonce reuse, and
authenticated metadata binds stream identity, ranges and offsets. The example
keeps bounded chunk buffers, writes disjoint file ranges, and aborts and joins
sibling tasks on failure or cancellation. It uses an explicit experimental
ALPN/wire format and does not implement resume; production framing and resume
remain unchanged.

Across five alternating measured 512 MiB downloads per mode, one/four-stream
medians were 27.409/27.220 MiB/s (-0.69%). Sender CPU was 12.08/14.94 s/GiB
(+23.7%); receiver CPU was 10.14/14.70 s/GiB (+45.0%). Sender/receiver peak RSS
rose from 25,028/22,316 to 35,608/29,748 KiB. All twelve transfers, including
warmups and a valid slow one-stream sample, passed strict direct-route and full
hash checks. The representative screen failed both acceptance gates, so no
production parallel architecture is accepted. These modes share a congestion
budget; multiple independent QUIC connections remain unmeasured. The prototype
also differs from production in buffering and finalization, so its rates do not
establish a production throughput improvement.

Two reversed-order 64 MiB profiling on/off series changed median throughput
-5.8% then +0.7%, with sender CPU/GiB changing +1.1% then 0.0%. This shows no
repeatable rate penalty in these cohorts, rather than proving zero overhead.
All optimization scores use profiling disabled.

The continuation's final Croc comparison used frozen production CLI source
`3356360`, with the 256 KiB default and profile/window controls unset. The
parallel example was separately frozen at `37a2bad`. Official Croc 11.5.4 kept
compression off, global `--local`, `send --transport auto`, receiver explicit
Oracle `--ip`, and four TCP data channels. Each file/tool had one warmup and
five alternating measured transfers on identical incompressible fixtures.

| File | Rustytransfer / Croc median MiB/s | Croc rate advantage | Sender CPU s/GiB, Rustytransfer / Croc | Receiver CPU s/GiB, Rustytransfer / Croc |
| --- | ---: | ---: | ---: | ---: |
| 64 MiB | 11.378 / 19.675 | 72.9% | 14.08 / 5.28 | 17.12 / 12.16 |
| 512 MiB | 23.958 / 27.521 | 14.9% | 15.00 / 3.82 | 15.38 / 12.50 |

All 24 transfers passed full received-size/SHA-256 checks and strict actual
direct-route proof. The 512 MiB rate ranges were 21.411–24.011 MiB/s for
Rustytransfer and 26.623–28.916 for Croc; MADs were 0.053 and 0.898. Valid slow
samples remain included. These later rates differ from the earlier cohorts;
sequential WAN measurements do not isolate transport, CPU work or time-window
effects as the cause of the difference.

The [continuation evidence](../benchmarks/results/oracle-20261003-direct-tuning/README.md)
retains 260 audited valid transfers including warmups, with raw rows, complete
cohort schedules, executable/source provenance, source archives, ranges/MADs,
reproduction helpers, exclusions and [acceptance decisions](../benchmarks/results/oracle-20261003-direct-tuning/acceptance.json).
The two incomplete 4 KiB groups remain unscored; their diagnostics are retained
outside the audited complete-group totals. No additional production performance
default changed. Infrastructure/correctness commits are `f9412e8` (diagnostics),
`c78d5d6` (chunk/resume tests), `3356360` (bounded opt-in window control), and
`37a2bad` (isolated shared-key example and runner metadata).

All required workspace, formatting, wasm, 53 Python benchmark tests, security
identity/report self-tests, Clippy/SARIF gates with exactly three existing
findings, and cargo-deny checks pass. The example's 13 focused tests and final
one/four-stream hash/direct-route, truncation and SIGTERM-cleanup smokes pass;
it introduces no example Clippy findings. An earlier window-metadata test
expectation was corrected before the passing checks; its failed run remains
visible. Exact frozen-source hashes match the final source after validation.
The final independent cleanup found no benchmark endpoints, TCP listeners or
leases and verified the original INPUT-chain hash. The temporary rule was
bounded and restricted to the dynamically derived SSH client IPv4 /32; OCI
settings were unchanged. Desktop/device work remains stopped and pending
extension/release artifacts remain intact.
