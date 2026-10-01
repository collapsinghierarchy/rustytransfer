# Verified-direct invite smoke, 2026-10-01

One warm-up and one measured run were made for each size and direction, with
two endpoint rows per run. All 16 rows passed full output size/SHA-256 checks,
and both endpoints verified the payload route as direct: no relay STREAM frames,
no relay selection, and no lagged or missing path evidence. The
[`summary.json`](summary.json) has eight accepted measured endpoint groups and
no rejected rows. One measured run per case is **validation**, not a stable
throughput baseline or evidence that a candidate is faster.

- Rust source: modified `feature/direct-transfer-desktop` worktree based on
  `4ba7b01`, before any payload optimization. The packaged Rust source archive
  SHA-256 was
  `f8e8fcb8dda551ee5cb826288f2867bcd536f8301462842258d015487e148027`.
- Toolchain: Rust 1.97.0 on WSL x86_64 and Oracle Linux aarch64.
- WSL release binary SHA-256:
  `9b11fc9c6eb6847ca2796972c5224b7d98cb31d22823b6801f57ae3f2bc5f0ed`.
- Oracle release binary SHA-256:
  `11066ce784f9cdee050f90a25e19928fed89b5bb7b7e9bdb59020a39efcefff0`.
- Host pair: Ubuntu WSL and `ubuntu@141.147.1.21`. Files were on each host's
  native ext4 filesystem. Reverse trials staged and hashed the Oracle source
  before the transfer wall timer started.
- Authentication: direct `rt1:` invite. The incompressible 64 MiB source SHA-256
  was `b657d87cf92612db23f505549e6c37206c46160c77ed3f40dcc153b6625883bf`;
  the 512 MiB source SHA-256 was
  `30671134dac585f880ff30d0a898cba69535339855bd938ef68585a8d142c1de`.

| Direction | Size | Measured wall time | Effective rate | Sender CPU | Receiver CPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| WSL to Oracle | 64 MiB | 14.144 s | 4.525 MiB/s | 1.46 s | 4.25 s |
| WSL to Oracle | 512 MiB | 93.676 s | 5.466 MiB/s | 9.49 s | 33.39 s |
| Oracle to WSL | 64 MiB | 5.454 s | 11.735 MiB/s | 1.77 s | 1.08 s |
| Oracle to WSL | 512 MiB | 23.928 s | 21.398 MiB/s | 13.83 s | 7.39 s |

The large direction difference is consistent with the previous uninstrumented
smoke but does not isolate network, filesystem, or CPU costs. Later A/B runs
must use the same observer in both binaries, five measured runs per size and
direction, and alternate build order before a performance claim is made.
