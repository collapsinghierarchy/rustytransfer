# Performance measurements

Each CLI endpoint can append one JSON object to the file named by
`RUSTYTRANSFER_METRICS_JSONL`. The record uses
[`performance-v1.schema.json`](performance-v1.schema.json), schema version 1.
It includes the selected Iroh path or WebRTC candidate types and separates
handshake, payload, and shutdown time. The path is sampled at the start and end
of the transfer; `path` is `mixed` if Iroh changes between direct and relay.
The structured JSONL measurements and summaries are versioned. Per-run logs
under `results/*/logs/` remain local and are ignored by Git.

Example for two independently launched endpoints:

```sh
RUSTYTRANSFER_METRICS_JSONL=sender.jsonl ./target/release/rustytransfer --transport iroh send --file input.bin --password ABCDE
RUSTYTRANSFER_METRICS_JSONL=receiver.jsonl ./target/release/rustytransfer --transport iroh recv --code 1234-ABCDE --out received.bin
```

The CLI leaves CPU, peak RSS, and SHA-256 fields `null`; a benchmark runner must
fill those from separate process measurements and hashes computed outside the
timed interval. The commit and dirty-worktree fields identify the source state
without treating an uncommitted tree as the named commit.

The ignored `local_full_file_performance_baseline` test is a local full-transfer
runner. In WSL, run it with:

```sh
RUSTYTRANSFER_BENCH_SIZE_MIB=512 \
RUSTYTRANSFER_LOCAL_BENCH_JSONL=benchmarks/results/phase0-local-512.jsonl \
cargo test -p rustytransfer --release --test local_transport_regressions \
  local_full_file_performance_baseline -- --ignored --nocapture
```

It generates deterministic incompressible input outside the timed intervals,
performs one warm-up per transport, then records five trials per transport in
alternating order. The local Iroh fixture uses its local relay test server; the
WebRTC fixture binds to loopback and verifies a direct host-to-host pair. Each
trial checks output size and SHA-256. Both endpoints share one test process, so
CPU and RSS fields are deliberately `null`; these measurements are useful for
payload throughput and correctness only, not the separate-process resource
gates. The WebRTC fixture uses the production 1 MiB send-buffer limit.

Set `RUSTYTRANSFER_LOCAL_BENCH_TRANSPORT=iroh` or `webrtc` to select one transport;
the default is both. A persistent `RUSTYTRANSFER_BENCH_SOURCE` fixture is preserved.
Enable `RUSTYTRANSFER_BENCH_PATH_EVIDENCE=1` for strict direct Iroh acceptance and
`RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE=1` for optional stage diagnostics. Iroh direct
rows are retained before rejection when both-endpoint STREAM evidence is missing.
Only test-owned received outputs are removed between trials. The
[two-series overhead check](results/local-20261002-profile-overhead/README.md) and
[Oracle stage diagnostics](results/oracle-20261002-payload-profile/README.md)
record the current profiling evidence.

For Croc 11.5.3, `run_croc_baseline.py` starts a temporary local relay and
separate sender and receiver processes. It records the selected auto path from
Croc's debug output, uses `--transport relay` for the forced-relay mode, and
measures each endpoint's CPU and peak RSS with `/usr/bin/time`. It captures the
phase boundaries from Croc's debug stream. Give Croc and Rustytransfer the same
uncompressed input file and keep both output directories on the same
filesystem when comparing them.

`run_oracle_transfer.py` requires `--build-id` and `--storage-class` so its
summaries keep builds and storage placement separate. `--rusty-auth invite`
selects direct-invite authentication; `--direction oracle-to-wsl` runs the
reverse endpoint roles and is available with `--rusty-only`. For a strict
direct-only sweep, build both endpoints with the benchmark path observer and
use `--rusty-path direct`. The runner records per-endpoint payload STREAM-frame
deltas from Iroh path events; it retains diagnostic rows but stops the sweep
unless both endpoints verify direct traffic. Lagged, missing, relay, or mixed
evidence is not accepted as direct. Historical rows with only start/end path
samples remain readable, but are excluded from strict direct summaries.

Use `--payload-profile` to collect opt-in sender read/allocation-copy-encrypt/send
wait and receiver receive/decrypt/write stage timings. These are elapsed spans,
including async wait and backpressure, rather than CPU time; sender read includes
the EOF probe and receiver write includes payload flush. The profile is absent
by default, uses additive JSON fields, and forms a separate `profile_mode` summary
group. For repeated Oracle-to-WSL runs, optional `--remote-input-64` and
`--remote-input-512` paths select already staged Oracle fixtures. Supply both
with `--direction oracle-to-wsl`; the runner verifies each fixture's full size
and SHA-256 before every trial and leaves those inputs untouched. `source_staging`
keeps pre-staged and per-trial inputs in separate summary groups.

Summarize medians, extrema, and MAD while keeping each JSONL input intact:

```sh
python3 benchmarks/summarize.py benchmarks/results/phase0-local-64-direct.jsonl \
  --output benchmarks/results/phase0-local-summary.json
```

## Phase 0 status (2026-09-20)

The release baseline completed for 64 and 512 MiB using the same SHA-256 input
for Croc and Rustytransfer at each size. Rustytransfer recorded five successful
direct runs per transport and size. Croc recorded five runs per mode at 64 MiB;
at 512 MiB it recorded five forced-relay runs and six auto runs whose selected
path was direct, plus two auto fallbacks that used relay. Every included row
has matching source and received SHA-256 values.

At 512 MiB, the matched direct-path medians were:

| Transport | Successful direct runs | Median wall time | Median effective rate | Median handshake / payload / shutdown |
|---|---:|---:|---:|---:|
| Croc auto | 6 | 4.50 s | 113.86 MiB/s | 0.86 / 3.53 / 0.09 s |
| Rustytransfer Iroh | 5 | 13.49 s | 37.94 MiB/s | 0.37 / 13.13 / 0.03 s |
| Rustytransfer WebRTC | 5 | 14.64 s | 34.96 MiB/s | 0.61 / 13.87 / 0.00 s |

Croc's auto mode fell back to relay twice; those rows are summarized separately.
Its forced-relay measurements are also kept separate because Rustytransfer's
512 MiB run used a direct path. The 64 MiB Rustytransfer path was direct, while
Croc's auto mode selected relay at that size, so those rows are not a same-path
comparison.

The local Rustytransfer fixture runs both endpoints in one process, so its
per-endpoint CPU and RSS fields remain `null`. Croc's direct-path median peak
RSS was about 340 MiB for the sender and 580 MiB for the receiver; its local
relay-path medians were about 26 and 24 MiB. These Croc measurements are
process-level results, while Rustytransfer's resource gate remains unmeasured.

The Phase 0 path and repeatability gate is complete. No product optimization was
made. The initial Rustytransfer runs made before adding the production 1 MiB
WebRTC send-buffer limit to the test fixture and the first Croc run with
unclassified auto paths are preserved in separate raw files but excluded from
[`phase0-summary.json`](results/phase0-summary.json). The implementation work can
proceed to Phase 1; the final CPU/RSS goals still need endpoint-level Rustytransfer
measurements.

## Phase 1 status (2026-09-20)

The sender and receiver file-transfer loops now live in the native library
module [`crates/transfer/src/lib.rs`](../crates/transfer/src/lib.rs). The CLI and full-file local
benchmark both call the same core. Progress uses a `(total, transferred)`
callback, and the transport interface preserves the existing PAKE, KEM, SMT,
FIN/FIN_ACK, and stream shutdown order. A bounded in-memory transport exercises
empty files, chunk boundaries, invalid passwords and ciphertext, early EOF,
slow receiving, delayed FIN/FIN_ACK, and peer aborts.

`cargo test --all-targets` passed, including the in-memory cases and existing
Iroh/WebRTC shutdown regressions. The release local integration runner completed
five direct-path measurements per transport at both 64 and 512 MiB. All runs
matched the input size and SHA-256. See the preserved JSONL and summaries:

- [64 MiB raw results](results/phase1-local-64-same-fs.jsonl) and
  [summary](results/phase1-local-64-same-fs-summary.json)
- [512 MiB raw results](results/phase1-local-512-same-fs.jsonl) and
  [summary](results/phase1-local-512-same-fs-summary.json)

At 512 MiB, the direct-path median effective rates (sender / receiver) were:

| Transport | Phase 0 | Phase 1 | Change |
|---|---:|---:|---:|
| Iroh | 37.94 / 38.20 MiB/s | 38.55 / 38.83 MiB/s | +1.6% / +1.7% |
| WebRTC | 34.96 / 35.21 MiB/s | 33.96 / 34.18 MiB/s | -2.9% / -2.9% |

Phase 1 wall-time medians were 13.28 s for the Iroh sender and 15.08 s for the
WebRTC sender. The full min/max/MAD and phase timing values are in the summaries.
WebRTC's five-run spread is wider than Iroh's; the medians remain within the
plan's 3% no-regression limit. At 64 MiB, all ten transfers also passed exact
size and hash checks. The detailed results show no throughput loss against the
Phase 0 medians.

Rust endpoint CPU and per-endpoint RSS remain unmeasured because both peers share
the integration-test process. The initial 64 MiB Phase 1 run read its input from
`/mnt/c` while writing its output under `/tmp`; that raw file and summary are
preserved as diagnostics but excluded from the Phase 0 comparison. The corrected
64 MiB series puts the input and output on the same WSL filesystem.

**Decision:** keep the extraction. Correctness and shutdown regressions pass;
the 512 MiB throughput gate is met. The next experiment is the Phase 2 block-size
sweep at 8, 32, 64, 256, and 1024 KiB on the same direct local paths.

## Phase 2 status (2026-09-20)

The 512 MiB release sweep measured five direct transfers per transport and block
size, with one warm-up per combination. The five block sizes were tested in
alternating order. Every successful row has the expected direct path and matching
input/output SHA-256. The detailed min/max/MAD data is in
[`phase2-local-512-sweep-summary.json`](results/phase2-local-512-sweep-summary.json);
the raw 100 endpoint rows remain in
[`phase2-local-512-sweep.jsonl`](results/phase2-local-512-sweep.jsonl).

Median effective rates in MiB/s (sender side) were:

| Chunk size | Iroh, 512 MiB direct | WebRTC, 512 MiB direct |
|---:|---:|---:|
| 8 KiB | 38.54 | 35.61 |
| 32 KiB | 88.92 | 31.80 |
| 64 KiB | 119.19 | 31.67 |
| 256 KiB | 164.76 | 30.38 |
| 1024 KiB | 163.09 | 28.92 |

For Iroh, 256 KiB is the smallest block within 3% of the best median; it also
improves payload time from 12.92 s at 8 KiB to 2.71 s. For WebRTC, 8 KiB is the
fastest tested size; larger blocks reduce its measured effective rate. The CLI
now defaults to 256 KiB for Iroh and retains 8 KiB for WebRTC. `--chunk-size`
continues to override either default.

Iroh also completed the same five-size sweep on a direct loopback path with
isolated Linux `netem` profiles. These 64 MiB runs used 15, 35, and 75 ms egress
delay, corresponding to approximately 30, 70, and 150 ms RTT. At each profile,
64 KiB was the smallest size within 3% of the best median. The raw and summarized
results are available for [30 ms](results/phase2-netem-rtt30-iroh-direct64.jsonl),
[70 ms](results/phase2-netem-rtt70-iroh-direct64.jsonl), and
[150 ms](results/phase2-netem-rtt150-iroh-direct64.jsonl). The corresponding
summary files report the path, phase times, extrema, and MAD.

The same namespace setup cannot establish WebRTC's loopback UDP sockets: the
existing `local_webrtc_fin_ack_closes_after_peer_close_signal` regression fails
there with `ENODEV`, even after assigning `127.0.0.1` to the isolated loopback
device. WebRTC RTT measurements are therefore unavailable in this environment.
An initial Iroh RTT attempt started on relay and changed to direct during the
transfer; its partial raw file is preserved but excluded because the selected
path was mixed. The corrected Iroh runs waited for the direct path before timing.
The sweep measured the full transfer on network transports; a separate
transport-free FSM microbenchmark was not run.

`/usr/bin/time -v` resource probes used a 512 MiB warm-up and one measured run
per transport. The release test process contains both endpoints, so peak RSS is
reported for the pair and bounds either peer in this harness. Iroh used 27,028
KiB peak RSS and 23.84 CPU seconds for both runs combined (11.92 s per pair on
average); WebRTC used 24,136 KiB and 64.86 CPU seconds (32.43 s per pair on
average). These totals include one warm-up and test/setup overhead. The exact
probe record is [`phase2-resource-probe.json`](results/phase2-resource-probe.json);
standalone CLI endpoint processes were not measured. WebRTC's CPU estimate is
above the 28-second goal and remains a Phase 3 optimization target.

**Decision:** adopt transport-specific defaults. The local 512 MiB payload gain
for Iroh exceeds 10%, and the paired-process RSS probe stays below 30 MiB for
both selected configurations. The WebRTC network-emulation and standalone FSM
measurements remain limitations. The next experiment is release-mode allocation
and CPU profiling of the WebRTC 8 KiB path before changing its buffer handling.

## Phase 3 status (2026-09-20)

**Hypothesis:** encrypting and decrypting in the existing chunk `Vec` avoids a
second full-block allocation and copy while preserving the message format and
nonce order. `DemStreamSealer` and `DemStreamOpener` now use AES-GCM's in-place
API; the transfer core reserves room for the authentication tag and passes the
same buffer through the sender FSM. The receiver decrypts its received buffer
in place. This changes no wire bytes.

All-target regression results: `cargo test --all-targets` passed (24 library
tests, the CLI default test, four local transport regressions, and the protocol
integration tests; three manual benchmarks were ignored). The focused transfer
tests also passed after fixing the sender's byte counter to count plaintext,
since the in-place buffer includes the tag after encryption. The crypto unit
test covers roundtrip and tamper rejection.

The release A/B run used the same 512 MiB source (`8e25e35d…416a35bf`), direct
loopback paths, one warm-up and five measured trials per transport. It checks
the received size and full SHA-256 on every run. Median effective throughput
and its change from the Phase 2 selected defaults:

| Transport | Block | Phase 2 median | Phase 3 median | Change | Phase 3 payload median |
|---|---:|---:|---:|---:|---:|
| Iroh receiver | 256 KiB | 169.38 MiB/s | 197.93 MiB/s | +16.9% | 2.221 s |
| Iroh sender | 256 KiB | 164.76 MiB/s | 191.25 MiB/s | +16.1% | 2.313 s |
| WebRTC receiver | 8 KiB | 35.87 MiB/s | 49.24 MiB/s | +37.3% | 9.787 s |
| WebRTC sender | 8 KiB | 35.61 MiB/s | 48.84 MiB/s | +37.1% | 9.872 s |

Rates are computed from the runner's measured wall interval; payload durations
exclude handshake and shutdown. Per-trial values are in
[`phase3-inplace-local-512.jsonl`](results/phase3-inplace-local-512.jsonl), with
median, min, max, and MAD in
[`phase3-inplace-local-512-summary.json`](results/phase3-inplace-local-512-summary.json).
All ten measured records use `path=direct`, match the source hash, and report
success.

The paired-process resource probe ran the same source and selected block sizes
with one warm-up and one measured transfer per transport. Iroh used 15.11 CPU
seconds total (7.56 per transfer on average) and 24,088 KiB process peak RSS;
WebRTC used 42.34 CPU seconds total (21.17 per transfer) and 21,452 KiB. Against
the Phase 2 probe averages (11.92 and 32.43 CPU seconds per transfer), these
are reductions of 36.6% and 34.7%; RSS decreased by 2,940 KiB and 2,684 KiB.
The process contains both endpoints and the harness, so CPU is an estimate for
the pair and RSS bounds either peer in this setup. The exact `/usr/bin/time -v`
outputs are `phase3-resource-iroh-time.txt` and
`phase3-resource-webrtc-time.txt`; the normalized record is
[`phase3-resource-probe.json`](results/phase3-resource-probe.json).

Two extra probe attempts are retained but excluded: one used a newly generated
source after its `/tmp` copy disappeared between WSL invocations (its hash was
`41eb02e4…a6095c`), and one used the correct source from `/mnt/c`, which changed
the input filesystem and storage cost. The accepted Iroh and WebRTC resource
probes ran in one WSL shell against the same `/tmp` copy and both reported the
expected source SHA-256. Resource measurements remain single measured samples;
unlike the throughput sweep, they are not five-run statistics.

**Decision:** keep the in-place crypto change. Both transports exceed the
10% CPU-reduction gate, and neither peak RSS increased. Throughput also
improved, with every local transfer verified. The next experiment is a bounded
read-ahead pipeline with depths 1, 2, 4, and 8 under the 70 ms Iroh WAN profile.

## Phase 4 status (2026-09-21)

**Hypothesis:** bounded read-ahead could overlap file reads, encryption, and
Iroh sends. A temporary implementation tested depths 1, 2, 4, and 8, with a
semaphore limiting in-flight chunks. In-memory checks covered output integrity,
the in-flight bound, slow reads and sends, and errors at several chunk positions.
The temporary implementation passed `cargo test --all-targets` and its focused
pipeline checks. No artificial encryption delay or per-process memory profile
was measured.

The release loopback sweep used the 64 MiB input
(`7819076e00e7e50a7295966e7b06803eb639616d0be6794172cb46cdc3523010`), one
warm-up, and five measured transfers per depth. Each transfer selected the
direct path and passed the full size and SHA-256 checks. Sender payload medians
were:

| Depth | Median payload | MAD |
|---:|---:|---:|
| 1 | 0.2121 s | 0.0013 s |
| 2 | 0.1981 s | 0.0041 s |
| 4 | 0.1985 s | 0.0008 s |
| 8 | 0.1955 s | 0.0023 s |

The WAN profile ran in an isolated network namespace with 35 ms delay in each
direction, 50 Mbit/s rate, and 0.2% loss. The transfer still selected the
direct path. The first sweep measured five runs per depth after one warm-up;
then depths 1 and 2 received five additional measured runs each after two
warm-ups. All 30 measured transfers passed size and hash checks. The isolated
namespace was removed after each run; the host loopback qdisc remained
`noqueue`.

Combining the ten sender measurements at depths 1 and 2 gives:

| Depth | Median payload | MAD | Min–max | Median effective rate |
|---:|---:|---:|---:|---:|
| 1 | 19.8706 s | 6.7805 s | 12.6105–57.8468 s | 2.792 MiB/s |
| 2 | 24.0643 s | 11.5328 s | 11.9723–57.5090 s | 2.414 MiB/s |

Depth 2 was 21.1% slower by median, with wide run-to-run variation. In the first
WAN sweep, depth 4 had a 30.9797 s median and depth 8 had a 25.4818 s median;
neither improved on depth 1. The loopback improvement did not carry over to the
constrained path, so the 5% WAN improvement gate was not met. The available
runner reports endpoint CPU and RSS as unavailable, so this experiment also
does not establish a process-memory change.

Raw rows and summaries are retained in
[`phase4-iroh-loopback-pipeline.jsonl`](results/phase4-iroh-loopback-pipeline.jsonl),
[`phase4-iroh-loopback-pipeline-summary.json`](results/phase4-iroh-loopback-pipeline-summary.json),
[`phase4-iroh-rtt70-pipeline.jsonl`](results/phase4-iroh-rtt70-pipeline.jsonl),
[`phase4-iroh-rtt70-pipeline-summary.json`](results/phase4-iroh-rtt70-pipeline-summary.json),
[`phase4-iroh-rtt70-pipeline-confirm.jsonl`](results/phase4-iroh-rtt70-pipeline-confirm.jsonl),
[`phase4-iroh-rtt70-pipeline-confirm-summary.json`](results/phase4-iroh-rtt70-pipeline-confirm-summary.json),
and the combined depth 1/2 statistics in
[`phase4-iroh-rtt70-pipeline-combined-summary.json`](results/phase4-iroh-rtt70-pipeline-combined-summary.json).

**Decision:** reject the pipeline candidate. Its implementation, CLI option,
and temporary sweep harness have been removed; the measured data stays for
reference. Phase 5 is not started because Phase 4 did not pass its performance
gate. A useful next measurement would break down per-chunk encryption and Iroh
send latency under the same WAN profile before selecting another optimization.

## Oracle validation (2026-09-21)

The release comparison transferred uncompressed 64 MiB and 512 MiB inputs from
the WSL x86_64 sender to the Oracle ARM64 receiver. Each product and size had
one warm-up and three measured transfers. Croc 11.5.3 used `--transport relay`;
Rustytransfer used the benchmark-only `RUSTYTRANSFER_BENCH_RELAY_ONLY=1` setting
on both endpoints. All measured transfers selected relay at both ends and
passed file-size and SHA-256 checks. This is three measured runs per size; the
five-run Oracle gate in the implementation plan remains open.

| Size | Transport | Median wall time (min-max; MAD) | Median rate (min-max; MAD) | Sender / receiver CPU | Sender / receiver peak RSS |
|---:|---|---:|---:|---:|---:|
| 64 MiB | Rustytransfer Iroh | 66.42 s (66.36-66.42; 0.00) | 0.964 MiB/s (0.964-0.964; 0.000) | 1.04 / 2.74 s | 29,280 / 36,340 KiB |
| 64 MiB | Croc relay | 17.57 s (13.41-22.03; 4.16) | 3.643 MiB/s (2.905-4.772; 0.739) | 0.71 / 1.88 s | 23,888 / 21,664 KiB |
| 512 MiB | Rustytransfer Iroh | 538.25 s (538.24-540.23; 0.01) | 0.951 MiB/s (0.948-0.951; 0.00002) | 7.08 / 22.62 s | 29,904 / 36,492 KiB |
| 512 MiB | Croc relay | 96.68 s (89.51-125.37; 7.17) | 5.296 MiB/s (4.084-5.720; 0.424) | 2.24 / 16.17 s | 24,576 / 22,620 KiB |

Rates use the complete measured wall interval. Croc's median rate was 3.78x
Rustytransfer's at 64 MiB and 5.57x at 512 MiB. Both products used relay paths,
but their relay providers differ (Iroh/N0 and Croc's public relay), so these
numbers compare the end-to-end relay configuration rather than isolating only
the transfer implementation. The endpoints ran as separate processes; CPU and
RSS are per sender and receiver.

A separate 64 MiB Rustytransfer direct-path probe completed successfully with
the expected SHA-256: 43.39 s wall time, 1.475 MiB/s, sender/receiver CPU of
1.52/4.11 s, and sender/receiver peak RSS of 28,556/35,924 KiB. It is one
successful warm-up observation, kept outside the relay medians. Croc's strict
`--transport derp` attempt could not establish Tailcat peer readiness on this
route; Croc auto mode selected relay for the 64 MiB file.

A separate Croc direct TCP probe then used Oracle as sender and WSL as receiver.
The receiver connected to the sender's local Croc endpoint with
`--ip 141.147.1.21:9009`; Croc opened four data connections to Oracle ports
9010-9013. Logs confirm the connections went to the Oracle address, and all
files passed SHA-256 verification. Each size had one warm-up and three measured
runs. The fixed-port direct route uses Croc's sender-local relay; it is distinct
from the Tailcat/DERP UDP route.

A matching Rustytransfer series used the same direction, endpoint machines,
zero-filled inputs, sample count, warm-up policy, full-process wall interval,
and SHA-256 checks. Both endpoints reported the Iroh `direct` path in every
run. Rustytransfer does not require Croc's fixed TCP ingress ports because Iroh
establishes its direct QUIC path through NAT traversal.

| Size | Transport | Median wall time (min-max; MAD) | Median rate (min-max; MAD) | Sender / receiver CPU | Sender / receiver peak RSS |
|---:|---|---:|---:|---:|---:|
| 64 MiB | Croc direct TCP | 4.683 s (4.679-4.737; 0.004) | 13.666 MiB/s (13.509-13.677; 0.011) | 0.38 / 0.83 s | 24,260 / 22,576 KiB |
| 64 MiB | Rustytransfer Iroh direct | 4.076 s (4.009-4.162; 0.067) | 15.701 MiB/s (15.377-15.963; 0.262) | 1.78 / 1.11 s | 38,408 / 27,920 KiB |
| 512 MiB | Croc direct TCP | 19.277 s (19.272-27.303; 0.005) | 26.560 MiB/s (18.752-26.567; 0.007) | 1.80 / 6.37 s | 24,848 / 22,664 KiB |
| 512 MiB | Rustytransfer Iroh direct | 18.841 s (18.787-18.844; 0.003) | 27.175 MiB/s (27.170-27.253; 0.005) | 13.49 / 7.82 s | 38,508 / 28,196 KiB |

One 512 MiB run spent about eight seconds bringing up all transfer channels;
the other two reached transport-ready in about a quarter second. The Oracle
host firewall also rejected these ports initially. A temporary iptables rule
limited to the WSL address allowed the measurements and was removed afterwards.
The OCI ingress rule is separate and remains as configured.

For the matched direct series, Rustytransfer's median effective rate was 14.9%
higher at 64 MiB and 2.3% higher at 512 MiB. Its median wall time was 13.0%
lower and 2.3% lower, respectively. Rustytransfer paid for that throughput with
2.39x and 2.61x as much combined endpoint CPU. Its Oracle sender also exceeded
the plan's 30 MiB peak-RSS target at about 37.6 MiB. The products use different
direct transport implementations, so these figures compare their complete
direct-transfer behavior rather than isolating TCP against QUIC.

Raw relay rows and their summary are in
[`oracle-64.jsonl`](results/oracle-20260921-relay/oracle-64.jsonl),
[`oracle-512.jsonl`](results/oracle-20260921-relay/oracle-512.jsonl), and
[`summary.json`](results/oracle-20260921-relay/summary.json). The successful
direct observation is retained in
[`oracle-64.jsonl`](results/oracle-20260921-direct-probe/oracle-64.jsonl);
its associated logs and the unmatched Croc path diagnostics remain available
locally under `results/oracle-20260921-direct-probe/logs/` and
`results/oracle-20260921-derp-probe/logs/`.
The Croc direct TCP rows and summary are in
[`oracle-20260921-croc-direct-tcp`](results/oracle-20260921-croc-direct-tcp).
The matched Rustytransfer direct rows and summary are in
[`oracle-20260921-rusty-direct-reverse`](results/oracle-20260921-rusty-direct-reverse).
Successful transfer logs and the initial firewall-block diagnostic remain
local in the corresponding `logs/` directories.

**Decision:** the current Rustytransfer build is substantially slower than Croc
on this Oracle relay route and does not meet the planned 5.5 MiB/s target for
512 MiB. On the matched Oracle-to-WSL direct route, Rustytransfer slightly
outperforms Croc at 512 MiB and more clearly at 64 MiB, while using substantially
more CPU and more sender memory. Phase 5 remains unstarted; the next relay-path
experiment is to profile per-chunk encryption and Iroh send latency. Direct-path
work should first target sender CPU and peak RSS without reducing throughput.
