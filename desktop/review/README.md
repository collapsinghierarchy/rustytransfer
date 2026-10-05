# Native desktop MVP validation

This standalone Iced app is an event-replay MVP: it does not perform live
transfers or discover LAN peers; screenshots show replayed data only. Both
validation scopes were recorded on
Windows on 2026-10-04 with Rust/Cargo 1.97.0. Linux builds and tests used WSL;
Windows GNU builds used MinGW and `x86_64-pc-windows-gnu`, then ran on Windows.

## Validation scopes

Initial MVP, source `38f468a415f5e75ba977a7d09d87ea05cb742128`: 15 tests passed
on Linux and native Windows; Linux formatting/Clippy with `-D warnings`, both
release builds, and the process-scoped WSL launcher `--help` passed. The direct,
relay, and resume replays produced the initial captures below.

Interaction update, source `d8cf0d4336601ef175dc2e7cd72f3a7644b24227`: 30
tests passed on Linux and Windows; formatting, Clippy with `-D warnings`, and
both release builds passed. Tests cover gestures, lens attachment, route hover,
filters, contextual actions, locality, and WGSL. Windows graphical smokes used
the direct fixture and a synthetic local peer; the replay marks the phone event
`network_scope: "local"`, it is not discovered LAN telemetry.

The WSLg graphical smoke stopped early, so graphical validation is Windows-only;
no selected adapter/backend was recorded. The `desktop.yml` workflow has not run
remotely. Callback intervals cover scripted motion and final capture, not GPU
time, input latency, transfer throughput, a benchmark, or an FPS guarantee.
Slow samples are retained; host/compositor/refresh conditions affect cadence.
No speedup is established. The custom canvas has no screen-reader semantics.

## Captures and callback samples

Initial captures show the transferring state; use Play or Step to advance the
resume replay to failure and relay recovery. Current build and smoke commands
are documented in the [desktop guide](../README.md).
The explicit-local replay is a synthetic direct-fixture variant used only for
the LAN-layout capture.

| Validation | Capture | Callback data | Frames | Mean (ms) | p95 (ms) | Max (ms) | Dots | Geometry rebuilds | Resize rebuilds |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Initial direct | [direct-lens.png](direct-lens.png) | [direct-frame-cadence.json](direct-frame-cadence.json) | 1,121 | 7.1424 | 8.7244 | 50.7517 | 16,192 | 0 | 0 |
| Initial relay | [relay-lens.png](relay-lens.png) | [relay-frame-cadence.json](relay-frame-cadence.json) | 1,142 | 7.0096 | 7.4681 | 35.9607 | 16,192 | 0 | 0 |
| Initial resume | [resume-lens.png](resume-lens.png) | [resume-frame-cadence.json](resume-frame-cadence.json) | 1,143 | 6.9954 | 7.4708 | 32.6148 | 16,192 | 0 | 0 |
| Interaction direct | [overview.png](interactions/overview.png) | [frame-cadence.json](interactions/frame-cadence.json) | 1,146 | 6.9804 | 7.4866 | 37.3448 | — | 0 | 0 |
| Explicit local peer | [lan-overview.png](interactions/lan-overview.png) | [lan-frame-cadence.json](interactions/lan-frame-cadence.json) | 1,136 | 7.0418 | 7.6587 | 34.2317 | — | 0 | 0 |

The initial logical window/field was 1280 × 840 / 1232 × 620.9; interaction
captures used 1280 × 840. All motion runs had zero geometry and resize rebuilds;
interaction reports record geometry revision 1 at motion start and end.
Interaction captures: [connection hover](interactions/hovered-route.png),
[captured device](interactions/captured-node.png), [moved device lens](interactions/dragged-node-lens.png),
and [explicit LAN layout](interactions/lan-overview.png). `nearby` reachability
or a direct route alone does not establish LAN locality.

## Artifact provenance

The two ignored, uncommitted Windows executables had SHA-256
`eabe73fdab0b99898fe9938d084eeec04bf63f593c7e1b26493a80f5afb006ab` (initial)
and `9e70d4832281b0aca884ad41a709f7137d0c2adc29d4716304a010d17ac32491`
(interaction). Working-copy `desktop/Cargo.lock` SHA-256 was
`4c80802ea196833bf97b6e4692b7d71b5b3927014e8f72bdc9fb631b94285dc3` for both;
line-ending conversion can change it from the Git blob hash. Hardware inventory
showed an AMD Radeon RX 7700 XT / driver `32.0.22029.9039`; selected adapter and
backend were not recorded.

| Artifact | SHA-256 |
| --- | --- |
| [Initial direct capture](direct-lens.png) | `358443e5b728176b491d7ee66f5ab38c389b6a4a018fa47e6dc9cab28be16c81` |
| [Initial direct callbacks](direct-frame-cadence.json) | `38460bb6d3e1e8bff493e0d0e54d7dc927b2670216d811e2c0aa2ed1501cc656` |
| [Initial relay capture](relay-lens.png) | `d3ef1c9e639062006c0edac4e0d064e2c455777f318c54e7b006a4a0dc3a1689` |
| [Initial relay callbacks](relay-frame-cadence.json) | `b3773c9ee0ff1d27793f471646eccfc8512e5ea64cabecf868d2be6dc3cf1d7b` |
| [Initial resume capture](resume-lens.png) | `084de14f29b40f816cca1ac2ca097f07bdd765bdebe4e9849dbd7cad8f447d4a` |
| [Initial resume callbacks](resume-frame-cadence.json) | `807fcef145bbc9b9de347b7ddbf769e3200a56b1612ebe4be0b16b675bdbe61a` |
| [Interaction overview](interactions/overview.png) | `f86ad6f69b40c3cb9e8e4c1b1d332a10a986108ce0a91b4687d8597a66d8d14d` |
| [Connection hover](interactions/hovered-route.png) | `4c035445a95c2d69d5ee1540b205f4dad838494d1b3b81c97214255fecc9ee73` |
| [Captured device](interactions/captured-node.png) | `6effef0b817a051ffd7261de0f67a9edc8379d36c64e8d7d1fc86bd3928a8561` |
| [Moved device lens](interactions/dragged-node-lens.png) | `5e4040d5ad18295687dfc7171954fe893aeed8c4b28ee4598a3b477080197a99` |
| [Interaction callbacks](interactions/frame-cadence.json) | `7931fe3eb28b74c1b150a7d8088d3c2da70099b680704a136b1139e00d7f708a` |
| [Explicit LAN layout](interactions/lan-overview.png) | `9d8903ab697c3b31a10a2979b76d36505080f0d8f5f4893b954e28b259d636a4` |
| [Explicit LAN callbacks](interactions/lan-frame-cadence.json) | `a0ab35c5204fa168fd30f7b0a681ee8a9d8c3fb0fa9820f45f8c69bd710bcde8` |
| [Synthetic local replay](interactions/local-demo.ndjson) | `46692d99c818c08cecc64067b908f1950c3a34055bac5f0276e85d946c6c8719` |
