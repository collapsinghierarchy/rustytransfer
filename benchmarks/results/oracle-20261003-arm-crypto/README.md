# ARM crypto dispatch comparison

The accepted candidate enables the pinned crates' runtime-dispatched AES/POLYVAL implementation on Linux/macOS AArch64. The baseline uses the default build. This changes build configuration only: encryption, authentication, nonce rules, payload framing, chunk size, and retry behavior are unchanged. Both endpoints verified direct payload-route evidence and complete output hashes for every accepted A/B transfer.

Frozen A/B source commit: `8f7d8e8`; source archive SHA-256: `04f2756a5b2e554ee0f3ca0ae615014ec41c20ee207d1a82747c2f408f1ef01a`. The baseline and candidate Oracle binary hashes/paths and the shared x86 binary hash are recorded in `manifest.json`; the same x86 binary is used for both variants. The full 16 JSONL result files are preserved under `series*/`.

Coverage: 80 measured transfers (160 endpoint rows) and 18 warmup transfers (36 endpoint rows), 98 transfers / 196 endpoint rows total. The recorded comparison runner came from checkout `d654b0b`, while the frozen A/B source archive is `8f7d8e8`; final current-source builds are a separate validation and are not represented by these A/B results.

| Series | Direction | Size | Baseline MiB/s | Candidate MiB/s | Rate Δ | Baseline Oracle CPU s/GiB | Candidate Oracle CPU s/GiB | CPU Δ | Baseline Oracle max RSS KiB | Candidate Oracle max RSS KiB |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| series1 | WSL→Oracle | 64 MiB | 4.69 | 4.54 | -3.2% | 66.9 | 51.7 | -22.7% | 30192 | 31244 |
| series1 | WSL→Oracle | 512 MiB | 5.44 | 5.72 | +5.3% | 64.3 | 49.1 | -23.6% | 31064 | 29860 |
| series1 | Oracle→WSL | 64 MiB | 11.59 | 11.62 | +0.2% | 28.0 | 13.4 | -52.0% | 30048 | 30012 |
| series1 | Oracle→WSL | 512 MiB | 25.31 | 25.15 | -0.6% | 27.1 | 12.3 | -54.6% | 28344 | 29972 |
| series2 | WSL→Oracle | 64 MiB | 4.82 | 4.84 | +0.4% | 65.6 | 50.6 | -22.9% | 31360 | 30872 |
| series2 | WSL→Oracle | 512 MiB | 5.64 | 5.68 | +0.7% | 63.2 | 48.6 | -23.1% | 29976 | 30068 |
| series2 | Oracle→WSL | 64 MiB | 12.57 | 12.24 | -2.6% | 28.5 | 14.1 | -50.6% | 27816 | 29316 |
| series2 | Oracle→WSL | 512 MiB | 23.73 | 23.63 | -0.4% | 27.9 | 13.0 | -53.2% | 29632 | 28140 |
| Pooled | Oracle→WSL | 512 MiB | 24.80 | 24.72 | -0.4% | 27.5 | 12.6 | -54.3% | 28792 | 28942 |
| Pooled | Oracle→WSL | 64 MiB | 12.18 | 12.04 | -1.1% | 28.2 | 13.8 | -51.3% | 27998 | 29664 |
| Pooled | WSL→Oracle | 512 MiB | 5.55 | 5.70 | +2.7% | 63.9 | 48.8 | -23.7% | 30960 | 29964 |
| Pooled | WSL→Oracle | 64 MiB | 4.76 | 4.78 | +0.5% | 66.3 | 51.1 | -22.9% | 30586 | 31112 |

**CPU gate:** Pass: at least 10% lower Oracle ARM CPU/GiB in each 512 MiB direction in both series.
**Throughput guardrail exception:** The first WSL-to-Oracle 64 MiB series crossed the -3% guardrail (-3.217%). Five additional reversed-order trials per variant did not reproduce it (+0.376%); pooled ten-trial medians improved +0.540%. The raw audit retains that crossing. Accepting the repeatable CPU benefit is an explicit, measured exception recorded in [acceptance.json](acceptance.json), without claiming a cause for the variation or a general WAN speed improvement.

The table reports medians; `arm-comparison-audit.json` also records extrema, median absolute deviations, process wall/setup values, and pooled summaries kept separate by direction and payload size. RSS is from the Oracle endpoint (receiver for WSL→Oracle, sender for Oracle→WSL).

## Recovery records

- The original run manifest is preserved as `manifest.json`; `audit-arm-comparison.py` can audit this artifact directory directly.
- The recovered run manifest and interruption note are preserved as `recovery-manifest.json` and `interruption-note.md`.
- Startup: ten measured trials per variant/direction. Forward 4 KiB median wall time was 3.0606/3.0604 seconds baseline/candidate; reverse 64 KiB was 3.9311/3.9346 seconds. Observed maxima were 3.2114/3.2106 and 4.2722/4.0210 seconds respectively. No meaningful startup regression was observed. The first reverse 4 KiB warmup lacked receiver payload-interval STREAM-frame evidence and remains excluded; the strict route gate was retained.
- Actual interrupted 512 MiB resume passed on current-source builds: the 156,499,968-byte prefix matched the source; each endpoint transferred only the 380,370,944-byte suffix, and the complete SHA-256 matched with verified direct routes. Treat this as correctness evidence; a full-file-size throughput calculation is not valid for the resumed suffix.

## Acceptance and limits

Accepted for ARM CPU efficiency: pooled 512 MiB Oracle CPU/GiB fell 54.3% when sending and 23.7% when receiving, consistently across both series. Peak Oracle RSS stayed around 30 MiB with overlapping, direction-dependent variation. Upstream CPU feature detection retains the software fallback; no global CPU target feature is enabled.

Numeric A/B results use frozen source `8f7d8e8`. Current `d654b0b` source builds include the already-tested shared invite/retry primitives and are separately checked for interruption/resume and the final direct Croc comparison. Profiling is disabled in the scored runs. This evidence covers Oracle Linux AArch64 and WSL x86_64 over this WAN pair. No physical LAN, native Windows, macOS performance, or physical ARM fallback measurement is claimed. Oracle permissions prevented CPU `perf` sampling; diagnostic stage timings are elapsed spans.

The [final validation record](final-validation/README.md) links startup/resume raw records and the separately scored current-build Croc comparison. See the [efficiency report](../../../docs/direct-transfer-efficiency-report.md) for the completed scope and validation. Desktop and device work is stopped, with unvalidated drafts preserved in the separate worktree.
