# Benchmark evidence

The maintained command and fixed Rustytransfer revision are documented in
[the performance guide](../README.md). Its local transfer-core timing differs
from the historical two-host WAN measurements below. Historical Croc comparisons
are evidence only; the maintained baseline and CI have no Croc dependency.

| Evidence | Scope |
| --- | --- |
| [Local runner validation](local-20261004-performance/README.md) | A/A, deliberate slowdown and fixed-baseline comparisons; 72 size/hash/route-verified transfers |
| [Final no-debug comparison](oracle-20261004-no-debug/README.md) | Frozen Oracle-to-WSL builds, 1/2/4 GiB full runtimes, all measured samples and warmups |
| [ARM crypto acceptance](oracle-20261003-arm-crypto/README.md) | Matched CPU-efficiency comparison, reversed-order followup, startup and resume correctness |
| [Direct-transfer experiments](oracle-20261003-direct-tuning/README.md) | Receive-window, chunk, diagnostics and shared-key stream screens; rejected alternatives retained |
| [Completion diagnostics](oracle-20261004-completion/README.md) | Endpoint completion phases and post-measurement source audit |
| [Independent QUIC experiment](oracle-20261004-independent-quic/README.md) | Four-connection screen; no production architecture accepted |

Smaller dated invite, probe, relay and large-file controls remain in their
campaign directories. They document distinct connectivity/correctness checks
and comparison windows rather than interchangeable baseline samples. No valid
slow sample has been removed.

Readable reports, scored/raw rows, summaries, acceptance records, frozen source
archives and source manifests remain at their result paths. Build logs,
diagnostic/check outputs, unscored partial diagnostics and inventory snapshots
are in verified archives. Each affected campaign's `artifact-index.json` points
to the authoritative [member/hash index](../archives/index.json).

For extraction, legacy-reader compatibility and original-to-rewritten commit
identities, see [archive recovery](../archives/README.md). Reported commit IDs
describe the measurement-time sources; use `baseline.json` for the maintained
baseline rather than selecting a historical result by throughput.
