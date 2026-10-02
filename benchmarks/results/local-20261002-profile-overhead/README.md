# Local payload profiler overhead check, 2026-10-02

Two series, each with one unscored warm-up and five measured transfers for each
64/512 MiB size and mode. Series 1 ran standard before profiling; series 2
reversed that order. All 40 measured transfers (80 endpoint rows) passed full
SHA-256 and strict both-endpoint direct STREAM evidence, with zero relay frames.
Profiles passed byte/chunk/stage validation. Warm-ups were checked but are not
written by the existing local harness.

| Series | Size | Sender median payload seconds, standard | Profiled | Change |
| --- | --- | ---: | ---: | ---: |
| 1 | 64 MiB | 0.204532 | 0.203705 | -0.41% |
| 1 | 512 MiB | 1.618084 | 1.642078 | +1.48% |
| 2 | 64 MiB | 0.208165 | 0.203977 | -2.01% |
| 2 | 512 MiB | 1.710488 | 1.672203 | -2.24% |

Receiver differences ranged from -2.56% to +1.45%. The observed variation is
under 3%, with inconsistent sign between series for 512 MiB; this check does not
show a repeatable overhead regression on this x86_64 host. It does not establish
zero overhead on ARM or explain WAN variation. Optimization comparisons use
profiling disabled on both endpoints.

The existing `local_full_file_performance_baseline` ignored release test ran with
`RUSTYTRANSFER_LOCAL_BENCH_TRANSPORT=iroh`, `RUSTYTRANSFER_BENCH_EXPECTED_PATH=direct`,
`RUSTYTRANSFER_BENCH_PATH_EVIDENCE=1`, the same verified ext4 source fixture per
size, and a unique `RUSTYTRANSFER_LOCAL_BENCH_JSONL` path per block. Only profiled
blocks set `RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE=1`. Both endpoints share one
process; CPU/RSS fields are deliberately null. Source files were preserved and
test-owned received outputs were removed between trials.

Rust 1.97.0 default release build on WSL x86_64 Ryzen 7 5800X, four test runtime
worker threads, WSL ext4 destinations. No concurrent local builds or benchmarks.
Binary hash and order are in `manifest.json`. Source is `8f7d8e8` plus the local
harness changes committed with these records; ARM build cfgs do not apply to
this target. Raw JSONL is adjacent. The source fixtures and host limitations
are documented in the [Oracle baseline](../oracle-20261002-invite-strict-baseline/README.md).
