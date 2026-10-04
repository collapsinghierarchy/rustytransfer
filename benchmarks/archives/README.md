# Historical benchmark archives

These are frozen evidence, not maintained performance commands. Croc and Oracle
collectors, their tests and dependencies retain their original bytes and paths.
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
