# Direct payload stage diagnostics, 2026-10-02

One warm-up and one measured trial per size and direction. All 16 endpoint
records have matching full-file SHA-256, both endpoints' verified direct STREAM
evidence, zero relay STREAM frames, and valid stage counters/timings. The
summary contains eight measured endpoint groups with no rejected rows.
These are diagnostic samples, not an optimization comparison.

| Direction / size | Sender payload seconds | Receiver payload seconds | Sender allocation/copy/encryption | Receiver decryption |
| --- | ---: | ---: | ---: | ---: |
| WSL to Oracle / 64 MiB | 13.519 | 13.626 | 0.049 | 1.368 |
| WSL to Oracle / 512 MiB | 150.783 | 151.125 | 0.410 | 10.027 |
| Oracle to WSL / 64 MiB | 2.119 | 2.141 | 1.245 | 0.043 |
| Oracle to WSL / 512 MiB | 16.832 | 16.847 | 10.010 | 0.358 |

For the forward 512 MiB sample, source reads took 0.300 seconds and send waits
150.066 seconds. Oracle's receive waits took 140.935 seconds and destination
writes 0.150 seconds. For the reverse sample, Oracle source reads took 0.440
seconds and send waits 6.379 seconds. This supports evaluating ARM crypto
dispatch before changing sender buffers or adding a pipeline. Stage timings are
elapsed spans, including scheduling waits where applicable; they are not CPU
samples. The sender allocation/copy/encryption span does not separate those
three operations.

The forward 512 MiB run was slower than the earlier five-run baseline. WAN
conditions and instrumentation remain confounders; these samples cannot prove a
regression or speed improvement. Local profiling-overhead checks and alternating
baseline/candidate trials are required before accepting an optimization.

## Provenance and reproduction

Rust source is the profiling implementation committed as `8f7d8e8`. The source
archive SHA-256 is
`04f2756a5b2e554ee0f3ca0ae615014ec41c20ee207d1a82747c2f408f1ef01a`.
Default release builds, Rust 1.97.0, no CPU/ARM acceleration overrides:

- WSL x86_64 binary SHA-256:
  `56eeb3f97b0d8e69afc066de58edad15dfdfaca387770cd0644e36460b06d50e`.
- Oracle aarch64 binary SHA-256:
  `f9c9e93debb8e985da9f0549bee5adb9d15858c19d66b303daf311a706c6985e`.

Same machines, deterministic incompressible fixtures, ext4 storage, and 256 KiB
chunks as the [five-run baseline](../oracle-20261002-invite-strict-baseline/README.md).
Oracle has one online Neoverse N1 CPU with AES and PMULL hardware features.
No normal-user perf sampling is available (`perf_event_paranoid=4`).

Used the existing Oracle runner with `--rusty-only --rusty-auth invite
--rusty-path direct --payload-profile --runs 1 --timeout 240`, build ID
`b-profile-20261002`, and the appropriate direction/storage labels. Reverse
trials used caller-owned `fixtures-20261002/input-{64,512}.bin` files via
`--remote-input-64` and `--remote-input-512`; size and SHA-256 were rechecked
before each timer. Forward sources were local ext4 files. Summaries separate
profile mode and source-staging policy. Profiling was enabled on both endpoints;
route evidence was enabled on both endpoints. Raw JSONL and summary are adjacent.

The two scoped remote run roots were removed after successful sweeps; the frozen
binaries and caller-owned fixtures remain. Transfer invite tokens were redacted
from retained diagnostic logs.
