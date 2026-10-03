# Final direct Croc comparison and functional validation

Completed 2026-10-03 on Oracle Linux AArch64 (one online Neoverse N1 CPU) and WSL
Linux x86_64 (Ryzen 7 5800X), using native ext4 paths and release Rust 1.97.0 builds.
The Oracle inputs were pre-staged and size/SHA-256 verified before each timer;
received files were checked after each timer. Cache state was not forced cold.
No builds or other benchmark transfers ran concurrently.

One warmup and five alternating measured transfers per size/tool produced
24 transfers / 48 endpoint records. All passed direct-route and full-file hash
checks. Median throughput is complete file MiB divided by full process wall time,
including connection/setup and completion. Warmups are excluded below.

| Size | Tool | MiB/s median | MiB/s min-max | MiB/s MAD | Wall seconds median |
| --- | --- | ---: | ---: | ---: | ---: |
| 64 MiB | Rustytransfer | 12.475 | 11.387-12.730 | 0.216 | 5.130 |
| 64 MiB | Croc direct | 19.277 | 15.097-19.743 | 0.466 | 3.320 |
| 512 MiB | Rustytransfer | 23.023 | 22.769-25.528 | 0.254 | 22.238 |
| 512 MiB | Croc direct | 28.869 | 27.676-28.905 | 0.023 | 17.735 |

| Size | Tool | Oracle sender CPU seconds | WSL receiver CPU seconds | Sender max RSS KiB | Receiver max RSS KiB |
| --- | --- | ---: | ---: | ---: | ---: |
| 64 MiB | Rustytransfer | 0.90 | 0.98 | 28784 | 24900 |
| 64 MiB | Croc direct | 0.33 | 0.74 | 24332 | 25676 |
| 512 MiB | Rustytransfer | 6.45 | 6.89 | 30356 | 24972 |
| 512 MiB | Croc direct | 1.76 | 6.01 | 24568 | 26020 |

The resource table reports medians from separate processes, not a shared
in-process harness. Full extrema/MAD and paired deltas are in
[audit-report.json](audit-report.json). Croc was faster and used less sender CPU
in these trials. This is one WAN direction; four Croc TCP data channels and
Iroh/QUIC differ beyond encryption, so this comparison does not identify the
cause of the gap or imply that either transport is universally optimal.

Croc 11.5.4 used global `--local --no-compress --debug --disable-clipboard
--ignore-stdin`, followed by `send --transport auto --port 9009 --transfers 4`.
The receiver used `--ip 141.147.1.21:9009 --no-compress`. Croc's embedded TCP
listener (called a local relay upstream) ran on the sender itself; file payload
crossed directly between the two endpoints, with no third-party relay hop.
The [pinned negotiation source](https://github.com/schollz/croc/blob/v11.5.4/src/croc/tailcat_negotiation.go)
disables Tailcat for `OnlyLocal`; the
[receiver source](https://github.com/schollz/croc/blob/v11.5.4/src/croc/croc.go)
clears default relay addresses when a sender IP is supplied.

Every Croc trial required all five Oracle listener ports, receiver connections
exclusively to Oracle 9009-9013, sender-local channels exclusively to loopback,
and accepted remote peers matching the leased client /32. Tailcat/DERP evidence
causes rejection. All twelve Croc log pairs were independently rechecked;
[croc-route-evidence.json](croc-route-evidence.json) retains the exact TCP route
lines and SHA-256 of each redacted full log. Full per-run logs stay local under
the repository's ignore policy. Rustytransfer required both endpoint observers
to verify direct payload paths with no relay STREAM-frame deltas. Croc's
handshake/payload/shutdown fields are null because detached sender markers cannot
provide reliable cross-host phases; its wall/CPU/RSS measurements are complete.

[Rustytransfer provenance](provenance-rusty-final.json) records current source
`d654b0b` plus the Cargo configuration, source archive/config hashes, and endpoint
binary hashes. The recorded config hash is for the Windows working-copy bytes;
Git line-ending normalization can change it without changing its Rust flags.
[Croc provenance](provenance-croc-11.5.4.json) records official release asset
URLs, verified archive digests, binary digests, and runtime versions. The release
API's `target_commitish=main` is metadata, not a pinned source commit. Full
canonical endpoint records and order are under [croc-direct-raw](croc-direct-raw).
The earlier flag-placement failure is [recorded separately](excluded-croc-pre-listener.json)
and excluded: Croc exited before opening a listener; its temporary firewall rule
was restored.

OCI subnet rules already permitted TCP 9009-9013. A bounded, unique, client-/32
instance rule was removed after the run. Both the lease audit and independent
[post-run audit](post-run-audit.json) verify the original INPUT chain SHA-256,
no remaining benchmark endpoints, and no TCP listeners on those ports.

Startup raw records are under [startup-raw](startup-raw): ten measured trials
per variant/direction, with forward 4 KiB and reverse 64 KiB kept separate.
Two endpoint rows from the initial reverse 4 KiB warmup remain excluded for
unverified route evidence. Resume raw records and the scoped interruption audit
are under [resume-raw](resume-raw); prefix/suffix sizes and hashes are derived
from that audit and cross-checked against both final endpoint rows. The resume
rate field uses full file size and is intentionally not summarized or scored.
No LAN, native Windows, macOS performance, or ARM fallback hardware result is
claimed. The frozen ARM A/B source differs from these current frontend builds;
those experiments are not merged into the Croc medians.
