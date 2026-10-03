# Direct 512 MiB control: Rustytransfer and Croc — 2026-10-03

This fresh Oracle-to-WSL control used the exact Rustytransfer and Croc 11.5.4
binary manifests used by the [1 GiB comparison](../oracle-20261003-croc-direct-1g/README.md).
One warm-up and five alternating measured pairs produced twelve transfers and
24 endpoint rows. All measured transfers passed full size/SHA-256 checks and
strict direct-route validation. Warm-ups are excluded below.

| Tool | Median MiB/s | Min–max MiB/s | MAD MiB/s | Median wall seconds | Oracle sender CPU seconds | WSL receiver CPU seconds | Sender max RSS KiB | Receiver max RSS KiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Rustytransfer | 25.178 | 22.362–25.262 | 0.084 | 20.335 | 6.55 | 6.94 | 27864 | 24192 |
| Croc direct | 28.743 | 27.610–28.834 | 0.091 | 17.813 | 1.80 | 5.75 | 24464 | 25088 |

Croc's ratio of median rates was 14.2% higher for this 512 MiB control. All
five paired Croc rates were higher than their Rustytransfer counterparts. The
1 GiB batch measured a 6.2% median-rate advantage for Croc, which is consistent
with a narrower gap at the larger size. These two batches do not establish file
size as the cause; the 1 GiB paired rates also varied, with two of five Croc
runs slower than their Rustytransfer pair. Every valid measured sample remains
in the comparison.

The fixture is the established deterministic 512 MiB AES-256-CTR input
(`30671134dac585f880ff30d0a898cba69535339855bd938ef68585a8d142c1de`). Both
endpoints used caller-owned, pre-staged native ext4 files; the runner verified
the Oracle size and SHA-256 before each trial and did not remove the source.
Output verification ran after timing. Caches were not forced cold. The binary
and fixture manifest hashes are recorded in `audit-report.json` and the raw
comparison manifest.

The Rustytransfer manifest SHA-256 is
`a636dbd2506c84d747b2037400fbabe235f35dfb29281c5d662f5e479b44b05d`; the Croc
11.5.4 manifest SHA-256 is
`0204e0efbf5e81f9598beddacbd3f5ba2dbb9d0d353f402ace2590683d035b78`. The
control manifest references the 1 GiB comparison manifest and confirms these
same hashes.

Rates use full process-pair wall time, including setup and completion. CPU and
peak RSS come from the separate endpoint processes. Rustytransfer's direct
payload route was verified at both endpoints. Croc used `--local`,
`send --transport auto`, and an explicit receiver `--ip`; the audit reparsed
all six sender/receiver log pairs and confirmed the Oracle listener/control
and four data channels, sender-local counterparts, and leased WSL `/32` peer.
No Tailcat/DERP route was accepted. Croc did not expose the runner's phase
markers, so its setup-time summary remains unavailable.

The benchmark's normal firewall-lease teardown failed only after all transfer
rows had completed: its stop check expected a three-argument process command,
while the configured lease used four arguments. The scoped recovery helper
verified the single lease PID,
its exact argv/cwd/tag and source `/32`, and confirmed no benchmark endpoints or
Croc listeners remained before signaling it. The original and restored Oracle
INPUT-chain SHA-256 values match exactly
(`8eabe87e4569e251c3147f193c0880b3c52ae3eaf5d007db8dda219ad79a53a5`). Only
the lease's recorded files were removed. This cleanup event did not affect the
completed transfer measurements.

[`audit-report.json`](audit-report.json) contains the per-trial summary,
provenance, route excerpts, and full redacted-log SHA-256 values for each Croc
trial. [Raw records](raw) preserve the canonical endpoint rows and manifests.
Full redacted logs are physically retained under `raw/oracle-512/logs/` locally;
the repository ignore rule excludes those logs from version control. This is
one Oracle ARM64 to WSL x86_64 WAN pair, not a general network-performance
result.
