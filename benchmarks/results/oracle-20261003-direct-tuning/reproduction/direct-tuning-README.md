# Direct-transfer tuning continuation — 2026-10-03

The ARM AES/POLYVAL optimization remains the accepted performance change.
This continuation separates elapsed stage work from CPU cost, tests bounded
window/chunk changes, measures an owned-buffer candidate, and screens parallel
streams sharing one payload key and one QUIC connection. Production tuning
changes require a repeatable gain of
at least 5% throughput or 10% CPU/GiB with no unexplained >3% guard-case rate
regression. Infrastructure and correctness improvements have separate commits.

Every standard WAN cohort uses native ext4 fixtures, exact frozen executables,
one warmup and five alternating measured runs per variant, strict payload-route
evidence at both endpoints, and complete received size/SHA-256 checks outside
all timed intervals. Diagnostic refreshes intentionally have one measured run
per direction rather than optimization acceptance sample counts. No valid slow
sample is excluded. Hosts do not perform builds, fixture generation, other
experiments or external hashing during timed transfers. CPU is per endpoint;
wall rates include complete process startup and completion.

## Diagnostic refresh

The refreshed 512 MiB Oracle sender profile records 0.285 s reading, 0.0254 s
allocating/copying, 1.127 s encrypting, and 15.139 s in send waits, out of a
16.581 s payload interval. The allocation/copy and encryption fields are nested
substages of the legacy aggregate, not additional time to add to it. This is
elapsed-span evidence; it is not a sampled CPU attribution. Sender CPU was
6.46 s and complete wall time 20.578 s, with 24.88 MiB/s effective throughput.
The allocation/copy span is less than 0.4% of sender CPU time even if all its
elapsed time were CPU work. It cannot explain the historical sender CPU gap.

Sender RTT samples ranged 14.741–39.611 ms, with 32.619 ms at the end. The
congestion window ended at 11,012,183 B; 16 lost packets/21,886 lost bytes and
four congestion events were recorded. Of 2,048 payload sends, 2,039 exceeded
1 ms and 394 exceeded 10 ms; the maximum was 67.814 ms. This supported a bounded
receive-credit experiment, without proving a credit bottleneck. The upload
profile instead had RTT reaching 392 ms, no recorded sender loss, and 88.585 s
in send waits. WAN behavior remains a confounder.

Samples occur at message boundaries at least 250 ms apart and at payload start
and end. They can miss conditions during blocked operations. Connection loss
and UDP deltas include all paths; congestion deltas need matching selected
paths. Iroh 1.2.0/noq 1.3.0 expose no stream-credit, bytes-in-flight or blocked
duration here. Noq does not emit TX DATA_BLOCKED/STREAM_DATA_BLOCKED frames:
zero values are not evidence against flow-control blocking. UDP datagrams are
not an exact QUIC lost-packet denominator. The independent route observer
remains authoritative.

There is no demonstrated application starvation that justifies reviving the
earlier pipeline. Small source/destination file spans and dominant send/receive
waits are insufficient to claim that a pipeline would improve WAN throughput.

## Instrumentation overhead

The refreshed instrumentation was separately compared on/off using the same
production CLI binaries, 64 MiB downloads, one warmup and five alternating
trials in each of two reversed-order series. Profile/standard median rates
changed -5.8% and +0.7%; sender CPU changed +1.1% and 0.0%. All 24 transfers
passed full hashes and strict routes. The WAN data do not establish zero
overhead or a repeatable rate penalty. Optimization scores use profiling off.
Sampling is opt-in and bounded to message boundaries, without a periodic task.

## Owned-chunk candidate

The candidate uses bounded `take(...).read_buf` into a `Vec` with tag capacity,
preserving short reads, final EOF/truncation checks, chunk/nonce order and
resume. Focused correctness tests passed; the candidate is retained as an
unshipped patch and frozen source/binaries.

Two reversed-order 512 MiB Oracle→WSL series used the diagnostic baseline and
candidate with profiling disabled, 256 KiB chunks, and identical fixtures.

| Series | Baseline / candidate MiB/s | Rate change | Baseline / candidate sender CPU s/GiB |
| --- | ---: | ---: | ---: |
| 1 | 22.736 / 23.760 | +4.5% | 12.78 / 13.74 |
| 2 | 24.525 / 23.629 | −3.7% | 13.26 / 13.64 |

The change failed confirmation and did not lower sender CPU. All 24 transfers
passed complete hashes and both-endpoint route evidence, including slow runs.
No copy-removal code was accepted; further acceptance guard cohorts were not
run after the representative case failed.

## Bounded receive-window screen

One binary pair compares the unset default against opt-in 2,500,000 and
5,000,000 B stream receive windows, with 256 KiB chunks and profiling disabled.
Both opt-in variants cap aggregate connection receive credit at 5,000,000 B;
other transport defaults and NAT traversal are preserved. Both endpoint logs
prove the actual settings. The unset baseline retains the pinned default
1,250,000 B stream window and default connection receive credit.

| Window | Median MiB/s | Change vs default | Sender CPU s/GiB | Sender / receiver peak RSS KiB |
| --- | ---: | ---: | ---: | ---: |
| Default | 23.411 | — | 14.52 | 29,412 / 25,536 |
| 2,500,000 B | 24.027 | +2.6% | 15.24 | 31,740 / 25,940 |
| 5,000,000 B | 23.773 | +1.5% | 15.16 | 34,028 / 26,432 |

Neither variant met an acceptance threshold; sender CPU and RSS rose. No
production window default changed, and no confirmation series or acceptance
guard cohorts were needed. The bounded opt-in control remains research
infrastructure. This screen does not prove a particular transport bottleneck.

## Chunk sweep

The original accepted ARM binaries compare 256 KiB, 512 KiB and 1 MiB chunks,
with profiling disabled. No transport packet or congestion-window setting is
changed by these application chunk sizes. Production short-read, truncation and
interrupted-prefix resume tests cover all three sizes.

| Direction / size | 256 KiB / 512 KiB / 1 MiB median MiB/s |
| --- | ---: |
| Oracle to WSL, 512 MiB | 24.265 / 24.616 / 24.497 |
| Oracle to WSL, 64 MiB | 12.499 / 12.481 / 12.668 |
| WSL to Oracle, 512 MiB | 5.160 / 4.408 / 5.129 |
| WSL to Oracle, 64 MiB, first series | 3.892 / 4.371 / 4.144 |

The valid 5.005 MiB/s 512 KiB download at 64 MiB remains included. The 512 KiB
upload variant regressed 14.6% at 512 MiB, despite its smaller-file improvement.
The 1 MiB upload median at 64 MiB improved 6.5% in the first series and 12.0%
in a reversed-order second series (3.903 versus 4.372 MiB/s). This meets the
median target gate, but only five of ten paired trials favored 1 MiB, with
large WAN variation. Second-series rate ranges were 3.324–4.676 versus
3.113–4.692 MiB/s; MADs were 0.517 and 0.296. Sender/receiver RSS increased
from 25,496/30,820 to 27,524/32,036 KiB. This is a case-specific positive median
result, not sufficient evidence of a robust general default improvement.
The existing opt-in chunk sizes remain available and the 256 KiB default stays.
The other cases showed no substantial throughput or CPU/GiB benefit.

The remaining sweep stopped on its first 4 KiB-upload 1 MiB-chunk warmup because
both endpoints did not prove direct payload STREAM frames. Its received hash
and diagnostic rows remain preserved and excluded; route checks were not
weakened. A separate 4 KiB retry failed the same receiver zero-frame observation
check, this time with 256 KiB chunks. Neither failed group contributes a scored
summary. These records show missing tiny-file route evidence; they do not prove
chunk-specific corruption or a relay path. A 64 KiB upload guard replaces
the scored 4 KiB upload cohort; the 4 KiB limitations remain explicit.
An independent cleanup audit found no endpoints/listeners/leases and
the exact original INPUT chain. The completed preceding groups are audited
separately; the incomplete group contributes no performance summary.

Raw small reverse rows from the old runner label their source as pre-staged.
The independent audit records the actual per-trial staging, derived from the
trial size and frozen runner behavior. Raw rows are unchanged. The staging
copy and full verification stayed outside timers and matched across variants.
The runner metadata bug is fixed for new trials.

## Provenance and reproduction

`builds/*/manifest.json` records archive/file/executable hashes, source commit,
dirty state, toolchains and immutable local/Oracle executable paths. The
baseline is the previous accepted ARM build. The first attempted diagnostic
build reused stale outputs because deterministic archive mtimes were zero.
Identical baseline/candidate executable hashes exposed it before any scored
run. Its `excluded-build.json` is retained; no transfer used that build.
Subsequent builders refresh extracted source mtimes before Cargo recompilation
and verify the copied archive and executable hashes. Full source archives are
retained. Dirty flags remain as recorded; exact file hashes are authoritative
despite Windows/WSL line-ending/stat noise and unrelated user changes.

`cohorts/*` contains untouched rows, configs, schedules and independent audits.
Large fixtures/executables stay on native Linux storage; full diagnostic logs
are retained locally under ignored `logs/` directories. Inventory hashes cover
the retained artifacts. Reproduction helpers expect to be placed back in this
repository's `target/` directory; they perform real Oracle work when invoked.
Existing fixtures, SSH authorization and the pinned Croc manifest are required.

Pending: remaining chunk cases, profiling overhead, parallel screen, fresh final
Croc comparison, cleanup audit and final acceptance record. Remove this pending
line only after all requested work has completed.
