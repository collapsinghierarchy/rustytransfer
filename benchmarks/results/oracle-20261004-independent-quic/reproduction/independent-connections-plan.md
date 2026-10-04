# Independent QUIC connection evaluation — 2026-10-04

Use the isolated, explicitly versioned shared-key example, with one application
KEM exchange and one AES payload key per file. Compare three geometries from the
same frozen executable pair: one connection/one data stream, one connection/four
data streams, and four independent connections/four data streams. The primary
control connection also carries data; total connection counts are exactly 1/4.
The four-stream single-connection control keeps file-task and buffer geometry
matched when isolating the number of congestion-control budgets.

First screen: Oracle-to-WSL 512 MiB, 256 KiB chunks, one warmup and five measured
trials per variant in rotated alternating order. Valid slow samples remain
included. Freeze source, binaries and harness bytes; native Linux fixtures;
complete size/SHA-256 and strict per-connection payload path proof at both ends.
Record actual distinct connection IDs, endpoint identities, applied geometry,
one key/KEM counts, wall/startup/payload/shutdown, sender/receiver CPU and RSS.
No tests, builds, generation or external hashing overlap scored transfers.

Use the previous acceptance thresholds: at least 5% median throughput gain or
10% CPU/GiB reduction, confirmed in a second reversed-order series, with no
unexplained >3% guard-case rate regression or material RSS/startup/correctness
regression. A failed representative screen ends production acceptance work.
For an independent-connection benefit, apply the threshold against both controls;
the matched four-stream control prevents attributing a task-count effect to
separate connections. Report each comparison even when a gate fails.
If the screen suggests a qualifying gain, confirm before assessing guard cases;
report throughput/CPU/RSS tradeoffs separately. Wide variation may require more
samples or rejection of a performance claim. No production architecture or
resume changes are part of this evaluation; the example is fresh-transfer only.

Before timing, validate AEAD lane binding/tampering/replay rejection, globally
unique payload/control nonce spaces including empty ranges, bounded buffers,
truncation/trailing data, authenticated completion, ownership/no-overwrite, and
cancellation during bootstrap and payload. Abort/join sibling tasks and close
all collected or partially established connections on errors. Stable-direct
readiness checks run concurrently to avoid artificial serial delay; their time
and all actual handshakes remain included in startup.

Retain every failure/exclusion. Verify per-connection route evidence rather
than only primary or aggregate labels. Cleanup independently checks endpoints,
listeners, leases and the exact original Oracle INPUT-chain hash. Croc ingress
rules are unnecessary for these QUIC-only cohorts.
