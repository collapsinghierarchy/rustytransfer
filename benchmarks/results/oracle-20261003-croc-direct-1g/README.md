# Direct 1 GiB Rustytransfer/Croc comparison - 2026-10-03

Oracle-to-WSL, with the exact Rustytransfer and Croc 11.5.4 binaries from the
[earlier 64/512 MiB comparison](../oracle-20261003-arm-crypto/final-validation/README.md).
One warmup and five alternating measured pairs produced twelve transfers and
24 canonical endpoint rows. Every accepted transfer passed complete size/SHA-256
and direct-route checks. Warmups are excluded from the table.

| Tool | Median MiB/s | Min-max MiB/s | MAD MiB/s | Median wall seconds | Oracle sender CPU seconds | WSL receiver CPU seconds | Sender max RSS KiB | Receiver max RSS KiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Rustytransfer | 27.592 | 27.262-28.550 | 0.306 | 37.113 | 13.19 | 14.17 | 31220 | 25664 |
| Croc direct | 29.297 | 23.949-29.799 | 0.502 | 34.952 | 3.50 | 12.82 | 24620 | 26180 |

Croc's ratio of median rates was 6.2% higher; the difference between median
completion times was 2.16 seconds. Two of the five paired Croc runs were slower
(one by 6.05 seconds, one by 0.07 seconds); all valid runs remain included.
The previous 512 MiB median-rate gap was 25.4%, measured in a different batch.
These snapshots alone do not prove that file size caused the change. The
[efficiency report](../../../docs/direct-transfer-efficiency-report.md) keeps
the fresh 512 MiB control and larger-file followups separate.

1 GiB means 1,073,741,824 bytes. The fixture is AES-256-CTR over zero bytes using
the same public zero benchmark key/IV as the smaller fixtures, independently
generated on both hosts. Its full SHA-256 is
`d37dfb4cb391e50e142f164f25a5d9b87b01b1c811d714f985c73aae53ac80c5`;
the first 512 MiB matches the established `30671134...42c1de` fixture.
Sources were pre-staged in native ext4 paths, verified before every timer,
and retained as caller-owned files. Received size/hash checks ran after timers.
Cache state was not forced cold. No builds or other transfer jobs ran concurrently.

[audit-report.json](audit-report.json) validates the fixture, exact provenance
manifest hashes, endpoint pairing, all Iroh byte counts/direct evidence, and all
six Croc sender/receiver log pairs. It includes exact TCP route lines and hashes
of the redacted full logs. Croc used four TCP data channels directly to the Oracle
sender's embedded listener, with `--local`, `send --transport auto`, and explicit
receiver `--ip`; no third-party payload relay or Tailcat/DERP path was accepted.
Croc phase timings remain null; rates use full process wall time, including setup
and completion. CPU/RSS comes from separate endpoint processes.

[Raw records](raw) preserve the canonical JSONL, manifest, order, and firewall
restoration. Full per-run logs remain local under the repository ignore policy.
The [post-run audit](raw/post-run-audit.json) independently checked the original
Oracle INPUT-chain hash and no remaining benchmark endpoints or Croc listeners.
Platform/direction coverage remains this Oracle ARM64 and WSL x86_64 WAN pair.
