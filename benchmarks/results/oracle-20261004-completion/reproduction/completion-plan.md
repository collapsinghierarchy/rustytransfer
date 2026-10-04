# Preregistered completion experiment — 2026-10-04

Test production single-connection framing and crypto unchanged. One KEM and one
application payload key per file remain unchanged. Baseline and explicit-close
candidate use the same frozen binary, same 256 KiB chunk, default windows,
payload profiling off and coarse completion profiling on. Candidate initiates
explicit connection close only after the existing confirmation/EOF conditions;
receiver must also commit and complete its required stream shutdowns. Await
the existing bounded endpoint close; do not reduce timeouts or bypass delivery.

First run one warmup and one diagnostic 64 MiB download per mode. Then screen
512 MiB Oracle→WSL with one warmup and five alternating measured samples per
mode, retain slow valid samples and validate full SHA/size and strict direct
STREAM-frame evidence at both endpoints. Score complete outer wall time as
before; separately retain endpoint process lifetimes (GNU time, 10ms resolution),
outer launch/ready/receiver launch/pair exit offsets on the runner monotonic
clock, and coarse application/confirmation/commit/drain/close spans. These
lifetimes overlap and their medians must not be added. Exact Croc payload/commit
markers are unavailable from official detached sender logs (seconds resolution,
some completion markers missing); do not infer a payload rate from process time.

Accept >=5% repeatable rate gain or >=10% CPU/GiB savings with no unexplained
>3% guard rate regression or material CPU/RSS/startup/correctness cost. Only after
a passing representative screen, run reversed confirmation and 64/512 MiB
opposite-direction, startup and interruption/resume guards. Do not ship parallel
architecture, extra keys, startup/address changes, or a protocol rewrite as part
of this completion experiment. If explicit close fails, retain opt-in diagnostics
and the measured negative result; default close behavior remains unchanged.

All builds/tests/fixture verification/external hashing occur outside timed
transfers. Frozen source archive and executable hashes identify exact inputs;
pre/post cleanup audits verify no endpoints/listeners/firewall leases remain and
the exact Oracle INPUT chain. Croc followup, if run, uses existing pinned 11.5.4
binaries and existing direct-only /32 temporary lease; no OCI changes.

## Amendment before any transfer: local endpoint working directory

Untimed inspection found that CLI metric emission calls `git rev-parse` and
`git status` after application lifetime capture. Local endpoints inherited the
Windows-mounted repository cwd, while Oracle endpoints already ran outside Git.
Three untimed Git status diagnostics took 3.22/5.32/3.30 seconds in the workspace
and roughly 1ms in native non-Git storage. These current measurements cannot
retroactively assign that cost to historical runs.

The diagnostic cohort now includes a third baseline-workspace variant, same
binary and default close, plus baseline and explicit-close variants in the
native benchmark base. One warmup plus one measured 64 MiB transfer per variant
will expose post-metric process duration and ensure the native cwd flag works.
The 512 MiB screen compares baseline and candidate under identical native cwd.
This working-directory correction is benchmark infrastructure, not a production
speed improvement. Fresh Croc comparisons must use the same native local cwd
for both programs. Outer wall timing and exact frozen build provenance stay
unchanged; the runner records local cwd in every row. No historical row is edited.

## Reporting amendment after measurements

The user requested that future Croc reporting compare each Rustytransfer phase
individually against Croc's arithmetic mean full-transfer throughput, never
Rustytransfer's overall throughput against Croc. Derived reports now implement
that policy; raw timings, rates, valid slow rows and the Rustytransfer-only close
screen are preserved. Phase rate means average file MiB / phase seconds over
the five measured runs. Overhead-phase rates are normalized comparison indices,
not actual wire rates. The final Croc cohort has optional profiling disabled;
the earlier profiled close screen supplies finer commit/close diagnostics in a
different measurement window. The interrupted comparison is retained unscored;
its complete retry is the final phase-reference cohort.
