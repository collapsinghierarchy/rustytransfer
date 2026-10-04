# Rustytransfer performance baseline

The maintained command compares two committed Rustytransfer revisions:

```sh
python3 benchmarks/performance.py --profile ci --baseline-ref 47c338c19e9751ec0a47414f8d323e77fc974b08
```

Run on Linux/WSL with Python 3.12+, Git, `sha256sum`, and Rust 1.97.0 installed
through rustup. Configuration is reviewed in [`baseline.json`](baseline.json).
The fixed baseline is the first compatible harness commit, with production code
identical to the approved cleanup-plan revision. Updates require an explicit
configuration review; the runner never advances or substitutes the baseline.
The default candidate is committed HEAD; uncommitted transfer/build/harness
changes must be committed first. `--candidate-ref FULL_SHA` selects another
committed candidate. Identical refs are rejected outside explicit calibration.

The runner builds each revision independently with `--release --locked`, the
same pinned compiler and host architecture, and an identical committed harness.
Harness changes require a reviewed migration. All builds finish before trials.
It records source trees, binary and harness hashes, Cargo lock hashes, compiler,
configuration, runner/image identities, fixture identities and exact trial order.

For 64 and 512 MiB it generates deterministic SHAKE256 counter-stream fixtures,
performs one unscored warmup per build/size, then five pairs with alternating
baseline/candidate order. Fixtures are shared between arms; caches are warmed,
without cache flushing. Every transfer has a fresh destination and full size and
SHA-256 verification, including an independent Python hash after the harness
hash. Both endpoints must provide direct STREAM-frame evidence. Minimal route
counters are enabled identically; experimental settings and verbose profiling
are cleared. Chunk size is the production 256 KiB, with one connection/stream.

This is **local Iroh transfer-core performance** using production encrypted
transfer and durable finalization code. Both endpoints share one test process.
It covers local setup, payload and completion, not normal CLI launch, WAN/NAT
behavior, invite/resume/reconnect performance or per-endpoint CPU/RSS. Correctness
tests continue to cover those behaviors. The explicit elapsed timer encloses
local connection setup through both endpoints completing; build, fixture
creation, external hashing, process launch and report emission are outside it.
Endpoint phase durations overlap and are retained individually, never added
across endpoints. Endpoint `wall_seconds` are legacy phase sums, not pair elapsed.
The Python trial envelope supplies authoritative pairing/warmup metadata.

Statistics use the five paired **sender payload-rate ratios** (candidate divided
by baseline), with median, range, MAD and an exact enumeration of bootstrap
median resamples. Pair elapsed-rate ratios and absolute sender rates are reported
separately. No valid slow sample is removed. A provisional 10% loss threshold and
20% relative paired spread limit classify observations; unstable or
threshold-overlapping intervals are inconclusive. Small-sample bootstrap bounds
are descriptive and need hosted-runner calibration before gating. There is no
automatic retry or historical WAN throughput threshold.

[Performance CI](../.github/workflows/performance.yml) runs manually or weekly on
`ubuntu-24.04` x86 Linux. It calls this same command and uploads evidence even on
failure. It is **report-only**: performance observations never fail a run;
correctness, missing samples, incompatible harnesses, process failures and invalid
timing/hash/route evidence do. No Croc installation or Oracle credentials are
needed. PR/push performance triggers, ARM coverage and required regression gates
remain deferred until runner repeatability and runtime are calibrated.

Explicit calibration (also selectable in the manual workflow):

```sh
python3 benchmarks/performance.py --profile ci --baseline-ref FULL_SHA --candidate-ref FULL_SHA --calibration aa
python3 benchmarks/performance.py --profile ci --baseline-ref FULL_SHA --candidate-ref FULL_SHA --calibration slow
```

`slow` uses unchanged production source with a test-only 2 ms sleep per sender
progress callback in the candidate arm, inside payload timing. Its results are
synthetic detection evidence, never an accepted production baseline.

Output defaults to a fresh `target/performance/TIMESTAMP/`; `--output PATH` chooses
a fresh directory, useful on native WSL storage. Reports, raw trial envelopes,
endpoint rows, logs and build caches remain there. Only this invocation's source
snapshots, generated fixtures and known received/partial outputs are removed,
including on failure. Existing fixtures, release artifacts and caches are kept.

Run focused tests with `python3 -m unittest discover -s benchmarks -p test_performance.py`.
The [local validation record](results/local-20261004-performance/README.md) retains
A/A, synthetic slowdown and fixed-baseline candidate cohorts: 72 verified
transfers, including all warmups and valid slow samples. Hosted-runner calibration
remains required before enabling performance gating.
Historical reports and reproduction instructions are indexed in
[`archives/README.md`](archives/README.md); the old guide and runners are frozen
there. Historical Croc measurements remain evidence only. See the
[final no-debug report](results/oracle-20261004-no-debug/README.md),
[efficiency report](../docs/direct-transfer-efficiency-report.md), and
[approved cleanup plan](CLEANUP_PLAN.md).
