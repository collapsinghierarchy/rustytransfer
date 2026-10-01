# Direct-invite runner smoke, 2026-09-29

These four JSONL files contain one warm-up and one measured run per size and
direction (two endpoint rows per run). They validate the two-machine runner and
full output size/SHA-256 checks. They are **not** an optimization baseline:
one measured run cannot establish a median or variance, and the CLI's
start/end `direct` path samples cannot prove that no payload traversed a relay.

- Source commit: `4ba7b01`; Rust transfer code unchanged in the worktree.
- Local: Ubuntu WSL x86_64, WSL-native source/output files; release CLI SHA-256
  `677af53572444003880c4f6413cd0ea6364ff3bf919eb5fa3286cd7d43566e6e`.
- Remote: `ubuntu@141.147.1.21`, Linux aarch64, Oracle home-directory
  source/output files; release CLI SHA-256
  `c182c23fdc5b9f2d157b2e525f6c12b71141e6a9b1151496c3fc4fd6003de022`.
- Authentication: direct `rt1:` invite. Payload path was reported `direct` at
  both endpoints in every run. The receiver output matched the full source
  SHA-256: 64 MiB
  `b657d87cf92612db23f505549e6c37206c46160c77ed3f40dcc153b6625883bf`;
  512 MiB
  `30671134dac585f880ff30d0a898cba69535339855bd938ef68585a8d142c1de`.

| Direction | Size | Measured wall time | Effective rate | Sender CPU | Receiver CPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| WSL to Oracle | 64 MiB | 18.362 s | 3.485 MiB/s | 1.43 s | 4.13 s |
| WSL to Oracle | 512 MiB | 96.837 s | 5.287 MiB/s | 10.25 s | 32.36 s |
| Oracle to WSL | 64 MiB | 5.308 s | 12.058 MiB/s | 1.78 s | 1.18 s |
| Oracle to WSL | 512 MiB | 23.793 s | 21.519 MiB/s | 13.80 s | 7.17 s |

The direction difference is a profiling question, not evidence of a software
regression or improvement. The raw v1 rows predate explicit build/direction
metadata, so keep their files separate when summarizing them.
[`summary.json`](summary.json) does this: it keeps the four input files and two
endpoint roles separate, with eight measured endpoint rows rejected from the
strict-direct acceptance set because they lack payload-interval route evidence.

A separate 2026-10-01 storage sanity check wrote and synchronized 512 MiB of
zeros to Oracle's home-directory ext4 filesystem in about 10 seconds, then
removed that scoped probe file. WSL read the 512 MiB input in about one second.
These simple sequential checks do not model the transfer's writes, but neither
suggests a 97-second file-I/O floor for the WSL-to-Oracle run.
