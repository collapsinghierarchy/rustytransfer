# Native field interaction validation

Source: `d8cf0d4336601ef175dc2e7cd72f3a7644b24227`, on `feature/native-desktop-mvp`. Recorded on Windows, 2026-10-04, with Rust/Cargo 1.97.0. Linux release builds and tests used WSL; Windows GNU builds used MinGW GCC and `x86_64-pc-windows-gnu`, then ran on Windows. This record supplements the [initial MVP evidence](../README.md).

The source implements release-to-select gestures with a 6 px drag threshold, animated node capture into the draggable lens, full-path connection hover, contextual native controls inside the lens, visibility filters, and the large source at the top left. Peers default to the right; explicit local network evidence places them below the source on the left. Pointer and lens warping still execute in WGSL; capture and lens movement reuse the cached geometry.

## Validation

- All 30 tests passed on Linux and in the native Windows test executable. Tests cover click versus drag, dragging out and back, focus cancellation, captured node attachment, nearest-path selection between dots, route hover, filters, per-item actions, LAN classification, and WGSL validation.
- Formatting and Clippy with `-D warnings` passed for Linux and the Windows GNU target. Both release builds succeeded. Windows graphical smoke runs completed for the direct fixture and the explicit local-network variant below.
- Existing production transfer sources and CI retain their behavior. The separate desktop CI still checks builds/tests; these local callback reports are not a performance gate. No remote desktop workflow run is recorded.

The ignored Windows executable `target/rustytransfer-native-mvp.exe` has SHA-256 `9e70d4832281b0aca884ad41a709f7137d0c2adc29d4716304a010d17ac32491`. The raw working-copy `desktop/Cargo.lock` SHA-256 remains `4c80802ea196833bf97b6e4692b7d71b5b3927014e8f72bdc9fb631b94285dc3`.

```powershell
target\rustytransfer-native-mvp.exe --smoke-test target\native-mvp-interactions-recorded
target\rustytransfer-native-mvp.exe --replay desktop\review\interactions\local-demo.ndjson --smoke-test target\native-mvp-lan-validation
```

The [local replay](local-demo.ndjson) is derived from `desktop/fixtures/direct.ndjson` by adding `network_scope: "local"` to the phone's peer event. It is synthetic demonstration data, not a discovered LAN peer. Missing locality information in the unchanged direct fixture keeps that peer on the right. The local fixture is retained so the layout can be reproduced.

## Captures and callback samples

- [Default layout](overview.png): large source at top left, peer on the right, visibility filters only in the top right.
- [Connection hover](hovered-route.png): the whole payload path glows without opening the lens.
- [Captured device](captured-node.png) and [moved device lens](dragged-node-lens.png): the original marker leaves the field and follows the lens; contextual controls move with it.
- [LAN layout](lan-overview.png): the explicitly local peer is below the source on the left.

| Replay | Raw callback samples | Frames | Mean ms | p95 ms | Max ms | Geometry rebuilds during lens drag | Resize rebuilds |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Direct | [frame-cadence.json](frame-cadence.json) | 1146 | 6.9804 | 7.4866 | 37.3448 | 0 | 0 |
| Explicit local peer | [lan-frame-cadence.json](lan-frame-cadence.json) | 1136 | 7.0418 | 7.6587 | 34.2317 | 0 | 0 |

These are window frame callback intervals during scripted lens movement and its final capture, not GPU execution time, input latency, or transfer throughput. Slow samples are retained. Both reports record the geometry revision as 1 at the start and end of lens motion. Captures use a 1280 by 840 logical window. Host load, refresh rate, compositor, and graphics adapter affect cadence; these runs establish no speedup or frame-rate guarantee. The smoke runner also generates the connection lens captures in its ignored output directory.

The GUI remains an event-replay MVP. Real file transfers, LAN discovery, and screen-reader descriptions for the custom canvas are not implemented. Linux compilation and tests passed; these graphical captures validate Windows only.

## Artifact hashes

| Artifact | SHA-256 |
| --- | --- |
| [overview.png](overview.png) | `f86ad6f69b40c3cb9e8e4c1b1d332a10a986108ce0a91b4687d8597a66d8d14d` |
| [hovered-route.png](hovered-route.png) | `4c035445a95c2d69d5ee1540b205f4dad838494d1b3b81c97214255fecc9ee73` |
| [captured-node.png](captured-node.png) | `6effef0b817a051ffd7261de0f67a9edc8379d36c64e8d7d1fc86bd3928a8561` |
| [dragged-node-lens.png](dragged-node-lens.png) | `5e4040d5ad18295687dfc7171954fe893aeed8c4b28ee4598a3b477080197a99` |
| [frame-cadence.json](frame-cadence.json) | `7931fe3eb28b74c1b150a7d8088d3c2da70099b680704a136b1139e00d7f708a` |
| [lan-overview.png](lan-overview.png) | `9d8903ab697c3b31a10a2979b76d36505080f0d8f5f4893b954e28b259d636a4` |
| [lan-frame-cadence.json](lan-frame-cadence.json) | `a0ab35c5204fa168fd30f7b0a681ee8a9d8c3fb0fa9820f45f8c69bd710bcde8` |
| [local-demo.ndjson](local-demo.ndjson) | `46692d99c818c08cecc64067b908f1950c3a34055bac5f0276e85d946c6c8719` |
