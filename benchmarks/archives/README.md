# Historical benchmark archives

These are frozen evidence, not maintained performance commands. Croc and Oracle
collectors, their tests and dependencies retain their paths and original bytes,
except for the documented credential sanitization below.
Readable reports, scored raw rows, summaries, patches and core provenance remain
under `benchmarks/results/`. The old benchmark guide is in `retired-active-tools`.

[`index.json`](index.json) maps every archived original repository path to a
bundle, byte count and SHA-256. It also records each bundle's hash and successful
extraction verification. [`removal-manifest.json`](removal-manifest.json) lists
only the exact helper paths removed after verification. All 120 inventoried
local agent helpers are preserved, including unique preparation/recovery code.

Verify every bundle and extracted member with Python 3.12 or later:

```sh
python3 benchmarks/archives/verify.py
```

Extract into a separate directory, preserving original paths:

```sh
mkdir -p /tmp/rustytransfer-history
tar -xzf benchmarks/archives/oracle-20261004-no-debug.tar.gz -C /tmp/rustytransfer-history
tar -xzf benchmarks/archives/retired-active-tools.tar.gz -C /tmp/rustytransfer-history
tar -xzf benchmarks/archives/local-agent-helpers.tar.gz -C /tmp/rustytransfer-history
```

Repeat for any campaign needed. For a full historical reconstruction, check out
`372956e` in a separate checkout and extract the bundles there. Frozen scripts
may depend on the old filesystem layout, binaries, tools or hosts; archiving
does not claim they can run against current production. Frozen source patches,
tool manifests and checksums remain readable at their existing result paths.

Original campaign `artifact-inventory.json` files remain historical inventories
and are also inside their bundles unchanged. New adjacent `artifact-index.json`
files explain archived storage. Use this archive index to locate helper bytes;
do not interpret an archived helper's absence from the live tree as lost evidence.

## Credential sanitization, 2026-10-05

`local-agent-helpers.tar.gz!target/croc_measure.py` contained a fixed password
used by its retired, self-hosted benchmark relay. It now generates a fresh
random password for each reproduction run. The old literal is unnecessary for
the maintained Rustytransfer-only benchmark or for recovering measurements.
The other 119 members of this bundle retain their exact bytes. Scored data,
source-file checksums, Rust patches, frozen dependencies, and all other bundles
are unchanged.

[`security-review.json`](security-review.json) records the original and sanitized
archive/member hashes and the reviewed findings, without credential values.
`index.json` records this explicit sanitization rather than claiming that the
modified member still has its original hash. All seven archives and 294
extracted members passed verification afterward.

A new commit does **not** remove the old password from Git history. The review
record leaves that historical finding blocked. History cleanup and any exact
false-positive exceptions need separate approval; neither has been enabled by
this archive change. The 75 other findings are source-file SHA-256 provenance
values and Rust `password: &[u8]` parameter declarations, which contain no
credential and have been retained.
