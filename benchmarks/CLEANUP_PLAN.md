# Benchmark cleanup and CI baseline plan

Status: implemented local runner, fixed compatible baseline, report-only CI and
verified helper archives. Hosted-runner calibration and enabling regression
gating remain deferred. See [supported command](README.md) and
[archive recovery index](archives/README.md). The original approved plan follows,
including historical inventory counts and proposed future stages.

Plan context: following the completed comparison in
`69fc4cce9a5d7ab8e160f6723ce792d2e35fabcf`. The CI baseline compares
Rustytransfer with an approved Rustytransfer revision. No Croc comparison is
included.

## Inventory and cleanup scope

Inventory taken on 2026-10-04:

| Location | Current contents | Treatment |
| --- | --- | --- |
| `target/*.py` | 120 ignored experiment, build, recovery and audit helpers | Preserve necessary unique evidence, then remove exact inventoried helper files after consolidation |
| `benchmarks/results/**` | 162 tracked frozen Python script copies in five campaigns | Package historical reproduction helpers into verified archives; retain readable results and provenance |
| `benchmarks/` | Three active tools and three Python test files | Consolidate the supported workflow behind one entry point and its tests |
| `.github/scripts/` | Seven security/Clippy scripts | Keep their existing purpose and CI behavior |

The active tools are `run_oracle_transfer.py`, `run_croc_baseline.py`, and
`summarize.py`; their tests include completion/environment checks. The frozen
copies are evidence rather than additional maintained entry points. Most are in
the no-debug, direct-tuning and completion campaigns: 64, 42 and 36 copies,
respectively. Independent QUIC has 18 and ARM crypto has two.

Preserve the user's README and ignore changes, assets, desktop draft worktree,
and release/submitted-extension artifacts. Inspect actual diffs before cleanup;
Windows/WSL status entries also include stat/line-ending noise. Build caches,
source fixtures and unrelated files are outside the helper-removal allowlist.

## One maintained command

Proposed entry point:

```sh
python3 benchmarks/performance.py --profile ci --baseline-ref FULL_COMMIT_SHA
```

The script owns builds, fixture preparation, trial scheduling, validation,
comparison, reporting and cleanup. Keep small internal functions and meaningful
runner tests. CI calls this same command; it contains no separate timing or
threshold implementation.

Configuration lives in one reviewed `benchmarks/baseline.json`: approved full
commit SHA, harness version, Rust toolchain, fixture sizes, sample policy and
regression thresholds. Generated files go to ignored `target/performance/` and
CI artifacts. `benchmarks/README.md` becomes the concise supported-command guide;
historical narratives stay in the evidence/report index.

The first implementation supports the local Iroh transfer-core baseline. A
future `--profile wan` may reuse the entry point for trusted two-host
Rustytransfer-only validation, if needed. It is deferred from initial CI work.

## What the CI check measures

Reuse `local_full_file_performance_baseline` and its real transfer functions in
`tests/local_transport_regressions.rs`. They already use a local Iroh relay
fixture, production encryption/transfer code, native files, full hashes and
direct STREAM evidence at both endpoints. Ordinary `cargo test --workspace`
currently skips this ignored performance test.

Make the test harness controllable by the runner: one warmup/trial at a time,
unique output paths and an explicit pair timer around setup through completion.
That permits five alternating baseline/candidate pairs for 64 and 512 MiB,
with one unscored warmup per build and size. Retain endpoint phase durations
and distinguish them from the single pair's elapsed time. Do not sum overlapping
endpoint lifetimes or count sender/receiver rows as independent trials.

Both endpoints currently share one process. This check covers local encrypted
transfer setup, payload and finalization. It does not reproduce CLI launch,
invite/reconnect/resume behavior, NAT traversal over WAN, or per-endpoint CPU/RSS.
Existing correctness tests continue to cover those features. A separate-process
CLI benchmark can be added later if startup or resource gates become necessary.

Build both revisions with the same pinned Rust toolchain, `--release --locked`,
architecture and build options, in isolated target directories. Use the same
harness version for both. Establish the first approved baseline after the
harness lands; record its production source identity. Harness incompatibility
requires an explicit reviewed migration. Build/download/generation and external
full-file hashing happen outside transfer timers; builds and other benchmark
jobs finish before trials start.

Pre-stage one deterministic incompressible fixture per size, reuse it for both
builds, record its hash and storage location, and document the warmed-cache
policy. Give every trial a fresh destination. Keep the production 256 KiB chunk
default, one connection and one stream. Optional payload/completion profiling,
verbose logging and experimental tuning stay disabled. The local harness may
enable its existing minimal route-evidence counters identically for both
builds; record that evidence setting explicitly. These rows have a different
measurement scope from the historical no-controls CLI cohort.

Report source/binary/harness hashes, runner architecture/image, toolchain,
configuration, exact trial order, warmups, raw rows, route proof, received hashes,
phase durations, elapsed times, rates, spread and the decision. Shared-process
resource values retain their scope; per-endpoint CPU/RSS remain unavailable.

## CI integration and regression policy

Add `.github/workflows/performance.yml` as an independent job/workflow. Start
with manual and scheduled runs, then enable reporting on relevant pull requests
and main-branch changes after measuring runtime and repeatability. Use the same
driver on x86 Linux and ARM Linux when available, with architecture-specific
reports. Pin the OS label and record the actual image; a label alone does not
freeze the machine's performance.

Each job builds and compares both revisions on its own runner. The saved WAN
rates of 27-30 MiB/s are historical observations, not CI pass/fail thresholds.
GitHub-hosted jobs use fresh machines and `-latest` images can change; this
motivates contemporaneous relative comparisons. See
[GitHub runner documentation](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Loopback and shared host load still require calibration.

The baseline reference is a reviewed full SHA, kept fixed until deliberately
updated. Build and record the actual candidate revision; on PR runs distinguish
the merge-test revision from the PR head. Never silently fall back to comparing
the candidate with itself. Use one validated harness version for both builds.
Changes to the harness or thresholds are visible in review alongside results.

Start in report-only mode. Run unchanged-code A/A comparisons and controlled
slower candidates to calibrate noise and confirm detection. A proposed initial
threshold is a 10% loss in the median paired payload-rate ratio; that value is a
CI proposal, separate from the user's accepted WAN gap. Add startup/completion
gates only after their repeatability is demonstrated.

Correctness, missing samples, invalid timing/hash/route evidence and process
failures always fail the check. Preserve all valid slow trials. For a noisy
threshold breach, allow at most one complete confirmation set with both builds;
retain both sets. Report unstable measurements as inconclusive and require
investigation before enabling a required performance gate. Do not rerun until
a passing sample appears. Publish reports/artifacts on success and failure.

PR jobs use hosted runners without Oracle credentials. Any future dedicated
host/WAN profile runs trusted code in a separate scheduled/manual workflow,
serializes access to benchmark hosts, and uses scoped credentials. This follows
[GitHub's guidance on untrusted code and self-hosted runners](https://docs.github.com/en/actions/reference/security/secure-use#hardening-for-self-hosted-runners).

## Implementation sequence

1. **Preserve and index.** Record exact helper paths/hashes and references.
   Identify unique recovery/preparation evidence, including prepared but unrun
   experiments. Verify existing artifact inventories before changing layouts.
2. **Land the runner.** Add the single command, config, controlled local harness
   and tests. First reproduce valid local measurements with the accepted
   production behavior. Cover bad hashes, invalid/missing rows, incompatible
   builds, timeouts, cleanup, paired statistics and regression decisions.
3. **Integrate CI and calibrate.** Validate the workflow locally where possible,
   then run A/A and deliberately slower comparisons in CI. Keep it report-only
   until the selected threshold and runtime are supported by results.
4. **Retire active experiments.** Extract needed Rustytransfer validation and
   reporting into the runner before removing old tools. Preserve frozen
   historical dependencies. Move the old Croc runner into historical evidence;
   the maintained baseline has no Croc dependency. Update all documentation,
   imports and tests that reference retired active paths.
5. **Compact historical helpers.** Use one checked archive per campaign with
   original paths, sizes, hashes and an extraction map. Keep result READMEs,
   scored raw rows, summaries and core provenance readable at existing paths.
   Archive helper trees only after extracting and verifying every member.
   Update artifact indexes to distinguish live files from archived members;
   retain the original inventories inside archives. Avoid broken report links
   and rewriting measured provenance. Git history remains intact.
6. **Remove redundant local helpers.** Delete only inventoried, verified agent
   helper copies after their replacements/evidence are retained. Use exact
   resolved paths within the approved workspace; keep fixtures and Cargo caches.
   Verify repository references, archive roundtrips, focused runner tests and
   the relevant Rust test target. Commit cleanup separately from CI behavior.

The target state is one documented maintained performance command, one reviewed
baseline configuration and one CI workflow, with tests and a clear historical
evidence index. Successful completion requires a working baseline comparison
and recoverable historical evidence, in addition to a smaller file inventory.
