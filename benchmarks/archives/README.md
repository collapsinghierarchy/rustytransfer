# Historical benchmark archives

These are frozen evidence, not maintained performance commands. Historical Croc
measurements, scored raw samples, readable reports and frozen dependencies remain
available under `benchmarks/results/` and in these archives.

The maintained tree keeps reports, scored rows, summaries and source provenance.
Build/check output and superseded implementation material are archived below.
The current index covers nine bundles and 551 verified members.

[`index.json`](index.json) maps original paths to each archive, byte count and
SHA-256. [`removal-manifest.json`](removal-manifest.json) inventories the helpers
removed only after verification. All 120 local helpers are recoverable.

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

Repeat for any campaign needed. Frozen scripts may depend on old filesystem
layouts, binaries, tools or hosts. Archiving does not claim they run against
current production. Artifact-index files beside each campaign locate archived
helpers. Original campaign inventories inside the archives remain collection-time
snapshots; live inventory records retain original hashes alongside updated hashes
for the normalized provenance described below.

## Archived diagnostics and retired experiments

`diagnostics-20261005.tar.gz` contains 254 build/check logs, unscored diagnostic
artifacts and maintained inventory snapshots at their original repository paths.
It preserves 9.00 MB of original bytes in a 0.75 MB bundle. Reports, all scored
rows, summaries, source archives and the normalized source manifests stay live.
Campaign `artifact-index.json` files refer to the single member/hash index rather
than duplicating large inventory lists. When the same inventory path occurs in
two bundles, the campaign bundle holds the collection-time original and the
diagnostic bundle holds the later maintained snapshot.

`retired-experiments-20261005.tar.gz` preserves the parallel-transfer example,
the completed benchmark cleanup plan and the superseded desktop implementation
plan. These are historical material; the maintained runner and current desktop
roadmap replace them. The production transfer engine never used that example.

To recover a linked diagnostic artifact, find its original path in `index.json`
and extract the indicated bundle into a separate directory:

```sh
tar -xzf benchmarks/archives/diagnostics-20261005.tar.gz -C /tmp/rustytransfer-history
tar -xzf benchmarks/archives/retired-experiments-20261005.tar.gz -C /tmp/rustytransfer-history
```

## Approved warning-source cleanup, 2026-10-05

The retired helper `local-agent-helpers.tar.gz!target/croc_measure.py` used a fixed
self-hosted relay password. It now generates a fresh random password on every
reproduction run. The literal was removed from the affected branch histories.
The other 119 members of that archive retain their exact bytes. The seven
original archives and 294 members verified at that milestone. No frozen
dependencies were changed by either cleanup.

Historical `source_files_sha256` provenance now uses records with separate `path`
and `sha256` fields rather than filenames as assignment keys beside digests. Every
original source digest is retained. Two Rust patch hunks have unrelated boundary
context trimmed; every added and removed source line is retained, and the patch
hash and live inventory hashes were updated. Production code, measured samples
and transfer behavior are unchanged.

[`security-normalization.json`](security-normalization.json) records original and
normalized evidence hashes and the rewritten commit mapping.
[`security-review.json`](security-review.json) records the original redacted finding
review and its resolution. There are no scanner exceptions, disabled rules or
ignore files. The maintained publication guard uses the unchanged default detector.

Some frozen historical readers expect the old provenance mapping. Restore its
exact original JSON bytes into a new directory before using those readers:

```sh
python3 benchmarks/archives/restore_provenance.py \
  --source . --destination /tmp/rustytransfer-original-provenance \
  --index benchmarks/archives/security-normalization.json
```

Overlay those recovered JSON files into a separate historical reconstruction
containing the result artifacts and extracted helpers. The restoration command
checks original byte counts and hashes and never overwrites the maintained tree.
Historical reports retain the original commit IDs at measurement time; the
rewrite mapping explains their replacement IDs.

The fixed performance baseline's new object ID is recorded in `../baseline.json`.
Its production source and harness are identical to the original reviewed baseline;
only evidence formatting and ancestry changed. The runner still refuses to advance
the baseline automatically. CI remains report-only for performance.

Rewriting branch histories does not erase other clones, backups, reflogs, or hosted
cached/PR references. Any relay still using the old password must also be rotated.
No complete physical erasure or external revocation is claimed.
