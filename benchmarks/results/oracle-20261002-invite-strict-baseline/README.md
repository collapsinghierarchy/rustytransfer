# Verified direct-invite baseline, 2026-10-02

One warm-up and five measured runs were completed for each size and direction.
All 48 endpoint rows passed full output size/SHA-256 checks and verified direct
payload paths on both endpoints: zero relay STREAM-frame deltas, no relay
selection, and no missing or lagged path evidence. The [summary](summary.json)
contains eight measured endpoint groups, five samples each, and no rejected rows.
This establishes the current baseline; no payload optimization is included.

## Build and environment

- Source: commit `a49fa18`, `feature/direct-transfer-desktop`. The Rust source
  archive SHA-256 is
  `46d89bd4bcd8be90ddf8a4683c5b36049bbc6d0a7503b62641d7de53e719f26f`.
- Rust 1.97.0, release builds, portable default CPU settings, 256 KiB chunks.
- WSL x86_64 binary SHA-256:
  `0497c6740bc7b8cf4ce206b5cd91b2dfcdbf5e3085b54c5dc2edc9a97fee1b41`.
- Oracle aarch64 binary SHA-256:
  `a37d080db5dd2d587df3f8b43b3424d11a45c51f0bda353f4208cece8fb99b85`.
- WSL: Ubuntu, AMD Ryzen 7 5800X, 16 logical CPUs,
  Linux `6.18.33.2-microsoft-standard-WSL2`.
- Oracle: `ubuntu@141.147.1.21`, Ubuntu aarch64, Neoverse N1, one online CPU.
  Its advertised CPU features include AES and PMULL. Existing services were
  left running; no compilation or profiling overlapped these measurements.
- Source and destination storage: native ext4 on both hosts. Caches were not
  dropped. Reverse sources were copied into a unique trial directory and fully
  hashed before the timer started. Each destination was new. Output hashing and
  process cleanup were outside the timed interval. This is neither a cold-cache
  disk benchmark nor a physical-LAN or native-Windows result.
- Direct `rt1:` authentication, benchmark payload path observer enabled on both
  endpoints; payload stage profiling disabled. Invites were redacted from logs.
  The binaries were frozen before later worktree edits. Raw `working_tree_dirty`
  flags describe the checkout at measurement time; binary hashes identify the
  exact executables used.
- Input 64 MiB SHA-256:
  `b657d87cf92612db23f505549e6c37206c46160c77ed3f40dcc153b6625883bf`.
- Input 512 MiB SHA-256:
  `30671134dac585f880ff30d0a898cba69535339855bd938ef68585a8d142c1de`.
  Both are deterministic incompressible AES-CTR fixtures, not transfer secrets.

## Measured results

Rates use the external process-pair wall timer, including endpoint startup,
authentication, payload, and shutdown. Sender and receiver rows refer to the
same five trials; they are not ten independent samples.

| Direction | Size | Median MiB/s | Min | Max | MAD | Sender CPU s/GiB | Receiver CPU s/GiB | Sender peak RSS KiB | Receiver peak RSS KiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| WSL to Oracle | 64 MiB | 4.691 | 4.624 | 4.816 | 0.067 | 18.56 | 63.04 | 25148 | 31120 |
| WSL to Oracle | 512 MiB | 5.633 | 4.748 | 5.661 | 0.028 | 17.44 | 63.22 | 25276 | 30540 |
| Oracle to WSL | 64 MiB | 12.653 | 11.372 | 12.736 | 0.083 | 27.84 | 14.88 | 29748 | 24948 |
| Oracle to WSL | 512 MiB | 21.443 | 17.349 | 25.138 | 0.959 | 27.44 | 13.42 | 30220 | 24856 |

CPU and peak RSS columns are medians of separate endpoint process measurements.
Phase medians, below, come from the transfer metrics rather than the external
timer; their sum need not equal the external wall median.

| Direction | Size | Endpoint | Handshake s | Payload s | Shutdown s |
| --- | ---: | --- | ---: | ---: | ---: |
| WSL to Oracle | 64 MiB | Sender | 1.307 | 10.698 | 0.381 |
| WSL to Oracle | 64 MiB | Receiver | 0.493 | 10.875 | 0.192 |
| WSL to Oracle | 512 MiB | Sender | 1.169 | 86.880 | 1.595 |
| WSL to Oracle | 512 MiB | Receiver | 0.433 | 87.124 | 1.415 |
| Oracle to WSL | 64 MiB | Sender | 0.942 | 2.114 | 0.619 |
| Oracle to WSL | 64 MiB | Receiver | 0.528 | 2.139 | 0.596 |
| Oracle to WSL | 512 MiB | Sender | 0.998 | 17.305 | 4.460 |
| Oracle to WSL | 512 MiB | Receiver | 0.521 | 17.325 | 4.473 |

The directional difference is repeatable across the earlier smoke and this
series, but these totals do not isolate network, storage, and crypto costs.
Stage profiles and matched alternating baseline/candidate trials are required
before accepting an optimization. Retain the planned gate: at least 5% median
throughput improvement or 10% lower CPU/GiB, confirmed in a second series, with
no unexplained regression above 3% in representative direct cases.

An earlier sweep dated 2026-10-01 ended during its first measured 512 MiB trial.
Its five 64 MiB measurements and 512 MiB warm-up remain under
`/home/wasilij/rustytransfer-bench/results/baseline-forward-20261001`; they are
excluded here. Its incomplete trial is not treated as a successful measurement.
The fresh sweeps completed with both scoped remote run directories removed.

## Reproduce the summary

The command below uses the frozen historical summarizer. Restore it from the
[retired tools archive](../../archives/README.md) into a separate checkout
before reproducing this historical summary.

```sh
python3 benchmarks/summarize.py \
  benchmarks/results/oracle-20261002-invite-strict-baseline/wsl-to-oracle-64.jsonl \
  benchmarks/results/oracle-20261002-invite-strict-baseline/wsl-to-oracle-512.jsonl \
  benchmarks/results/oracle-20261002-invite-strict-baseline/oracle-to-wsl-64.jsonl \
  benchmarks/results/oracle-20261002-invite-strict-baseline/oracle-to-wsl-512.jsonl \
  --output benchmarks/results/oracle-20261002-invite-strict-baseline/summary.json
```
