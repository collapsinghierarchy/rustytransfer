# Direct 2 GiB Rustytransfer/Croc comparison - 2026-10-03

Oracle-to-WSL, using the exact frozen binaries from the
[1 GiB comparison](../oracle-20261003-croc-direct-1g/README.md).
One warmup per tool and five alternating measured pairs produced twelve
transfers and 24 canonical endpoint rows. All passed complete received-size,
SHA-256, and direct-route verification. Warmups are excluded below.

| Tool | Median MiB/s | Min-max MiB/s | MAD MiB/s | Median wall seconds | Oracle sender CPU seconds | WSL receiver CPU seconds | Sender max RSS KiB | Receiver max RSS KiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Rustytransfer | 28.425 | 28.078-29.516 | 0.346 | 72.05 | 26.23 | 27.99 | 30464 | 25468 |
| Croc direct | 30.170 | 29.210-30.198 | 0.028 | 67.88 | 6.80 | 22.76 | 24692 | 25856 |

Croc's ratio of median rates was 6.1% higher. It was slower in 0
of the five paired measured transfers. All valid measurements are retained.
The [combined report](../../../docs/direct-transfer-efficiency-report.md)
compares these results with 1 GiB and the fresh 512 MiB control; sequential
size cohorts do not isolate payload size from WAN conditions.

2 GiB means 2,147,483,648 bytes. The deterministic AES-256-CTR fixture was
generated independently on both hosts and matched full SHA-256
`fd23e40748d31513a8d01ee79911e637d22bd39d02da98d47471c24f804fad28`. Its first 512 MiB matches the baseline
fixture; a separate preflight also verified the first 1 GiB against the 1 GiB
fixture. Inputs and outputs used native ext4 storage. Caller-owned sources
were pre-staged, verified before each timed trial, and retained. Received
size/hash checks were outside the timers. Cache state was not forced cold.
No builds, fixture generation, or other transfer jobs ran during timings.

[audit-report.json](audit-report.json) validates all endpoint rows, executable
manifest hashes, fixture size/hash, Iroh byte counts and strict direct-route
statistics, and all six Croc sender/receiver log pairs. Croc used global
`--local`, `send --transport auto`, and an explicit receiver `--ip` to connect
four TCP data channels directly to its Oracle sender's embedded listeners.
No third-party payload relay or Tailcat/DERP path was accepted. Iroh uses QUIC;
this comparison does not isolate the reason for the rate difference.

Rates use full process wall time, including startup and completion. CPU and
peak RSS are measured separately at each endpoint; Croc phase timings remain
null. The [raw records](raw) retain the canonical JSONL, run manifest, schedule,
firewall audit, preflight, and cleanup smoke record. The schedule is planned;
the complete paired canonical rows prove that all scheduled trials finished.
Route excerpts and full redacted log hashes are versioned in the audit report;
full per-run logs remain local under the repository ignore policy.

The temporary instance ingress lease allowed only the WSL client's /32 for
TCP 9009-9013, with a 30-minute maximum lifetime. Normal cleanup restored
the original Oracle INPUT chain exactly. The independent
[post-run audit](raw/post-run-audit.json) found no benchmark endpoints or Croc
listeners. OCI ingress rules were not changed. Results cover one Oracle ARM64
and WSL x86_64 WAN pair in this direction.
