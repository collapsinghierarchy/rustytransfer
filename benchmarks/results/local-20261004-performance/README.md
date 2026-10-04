# Local performance runner validation — 2026-10-04

Three sequential cohorts used the maintained command on native WSL storage,
x86_64 Linux, with Rust 1.97.0 release/locked builds. Each cohort includes one
warmup per build/size and five alternating measured pairs at 64 and 512 MiB:
72 transfers total (60 measured, 12 warmups), all verified by full size/SHA-256
and direct STREAM evidence at both endpoints. No build or other benchmark cohort
ran concurrently with measured transfers. This is a local transfer-core check
with both endpoints in one process; it establishes no WAN or per-endpoint
CPU/RSS result.

| Cohort | MiB | Median paired candidate/baseline payload-rate ratio | Observation |
| --- | ---: | ---: | --- |
| aa | 64 | 1.0064 | no-regression-observed |
| aa | 512 | 1.0217 | no-regression-observed |
| slow | 64 | 0.3668 | regression-observed |
| slow | 512 | 0.3692 | regression-observed |
| candidate | 64 | 1.0168 | no-regression-observed |
| candidate | 512 | 1.0117 | no-regression-observed |

`aa` independently built the identical compatible-harness revision
`1311b81694f9f15c002ab952bebfa7158b10ecc2` on both arms. `slow` used that same
source with a test-only 2 ms delay per sender progress callback on the candidate
arm, inside payload timing. This is deliberate synthetic slowdown detection,
not a production code change or accepted baseline.

`candidate` used the fixed baseline and committed `d146c47`, with default candidate
selection. Production source identity matches between arms; benchmark cleanup
changed no transfer behavior. The final command succeeded after the source check
was corrected to inspect content while tolerating Windows/WSL CRLF differences.
An isolated Git test also verifies that actual source edits still fail the check.

Each cohort retains `report.json`, scored/raw trial envelopes including warmups,
`summary.md`, both compiler logs, and a SHA-256-verified `runner.py.frozen` snapshot.
Original report fields are preserved, including the pre-configuration A/A run's
null configured baseline and its explicit baseline/candidate arguments. Runner
provenance additions after that run do not rewrite its measurements. Received
files and generated fixtures were removed by the runner; binaries/build caches
remain at the native paths recorded in the reports. Source commits and harness,
binary, fixture and runner hashes remain in each report.

Validation also passed: 13 maintained Python tests, 64 legacy benchmark tests
before retirement, seven non-ignored local Rust regression tests, Rust formatting,
the existing security/Clippy script checks, actionlint 1.7.7 on the performance
workflow, and extraction/hash checks for all seven archives (294 members).
The four original campaign inventories were checked before removal: 2,613 entries,
zero missing files or byte/hash mismatches.

CI remains manual/scheduled and report-only. A 10% threshold, 20% paired spread
limit and small-sample bootstrap intervals are provisional. Hosted-runner A/A and
slowdown calibration, architecture-specific calibration/ARM coverage, and required
regression gating remain future work. No CI run or remote push is claimed.
