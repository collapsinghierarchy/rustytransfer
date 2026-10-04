# Native desktop MVP validation record

This record describes the local validation run for source commit `38f468a415f5e75ba977a7d09d87ea05cb742128`. The toolchain was Rust/Cargo 1.97.0. The Linux build used WSL; Windows GNU release artifacts were cross-compiled there for `x86_64-pc-windows-gnu` with MinGW GCC, then executed on Windows.

## Recorded checks

- 15 unit tests passed on Linux and in the native Windows test executable.
- `cargo fmt --check` and Clippy with `-D warnings` passed on Linux.
- Linux and Windows native release builds succeeded.
- The PowerShell WSL launcher passed its process-scoped `--help` invocation.
- Direct, relay, and resume smoke-test runs produced the PNG and JSON artifacts linked below.

The Windows smoke commands were:

```powershell
target\rustytransfer-native-mvp.exe --scenario direct --smoke-test target\native-mvp-validation-direct
target\rustytransfer-native-mvp.exe --scenario relay --smoke-test target\native-mvp-validation-relay
target\rustytransfer-native-mvp.exe --scenario resume --smoke-test target\native-mvp-validation-resume
```

The `desktop.yml` workflow builds, tests, and runs Clippy on Windows and Linux and stores a preview artifact. It has not yet been run remotely because this source has not been pushed.

## Replay captures and callback samples

Each PNG is the lens capture from its named scenario. All three captures show the initial transferring state. The resume stream contains later failure and relay-recovery events; advance it with Play or Step to see those states. The model tests validate the complete streams.

| Scenario | Lens capture | Callback data | Frames | Mean (ms) | p95 (ms) | Max (ms) | Dots | Geometry rebuilds | Resize rebuilds |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Direct | [direct-lens.png](direct-lens.png) | [direct-frame-cadence.json](direct-frame-cadence.json) | 1,121 | 7.1424 | 8.7244 | 50.7517 | 16,192 | 0 | 0 |
| Relay | [relay-lens.png](relay-lens.png) | [relay-frame-cadence.json](relay-frame-cadence.json) | 1,142 | 7.0096 | 7.4681 | 35.9607 | 16,192 | 0 | 0 |
| Resume | [resume-lens.png](resume-lens.png) | [resume-frame-cadence.json](resume-frame-cadence.json) | 1,143 | 6.9954 | 7.4708 | 32.6148 | 16,192 | 0 | 0 |

The captures use a 1280×840 logical window and a 1232×620.9 logical field. Callback intervals cover scripted lens movement and the final screenshot capture. All slow samples remain in the reported percentiles. These are window frame-callback intervals, not GPU timestamps, input latency, or transfer throughput. Two earlier Windows motion runs averaged about 16.7 ms; the recorded final runs were about 7 ms. This variation reflects compositor, refresh, and host-load sensitivity; it does not establish a speedup, a performance benchmark, or a frame-rate guarantee.

The separate Windows hardware inventory showed an AMD Radeon RX 7700 XT with driver `32.0.22029.9039`. The smoke reports do not record which adapter or graphics backend WGPU selected. The Linux WSLg graphical smoke attempt stopped early, so there is no completed Linux GUI validation. The Windows frontend was validated. Canvas content does not yet provide screen-reader semantics, and the live transfer backend is not wired.

## Artifact provenance

The native Windows executable is a local ignored build artifact at `target/rustytransfer-native-mvp.exe`; it is not committed. Its SHA-256 is `eabe73fdab0b99898fe9938d084eeec04bf63f593c7e1b26493a80f5afb006ab`.

The working-copy bytes of `desktop/Cargo.lock` have SHA-256 `4c80802ea196833bf97b6e4692b7d71b5b3927014e8f72bdc9fb631b94285dc3`. The raw working-copy hash can differ from the Git blob hash because of line-ending conversion.

| Recorded artifact | SHA-256 |
| --- | --- |
| [direct-lens.png](direct-lens.png) | `358443e5b728176b491d7ee66f5ab38c389b6a4a018fa47e6dc9cab28be16c81` |
| [direct-frame-cadence.json](direct-frame-cadence.json) | `38460bb6d3e1e8bff493e0d0e54d7dc927b2670216d811e2c0aa2ed1501cc656` |
| [relay-lens.png](relay-lens.png) | `d3ef1c9e639062006c0edac4e0d064e2c455777f318c54e7b006a4a0dc3a1689` |
| [relay-frame-cadence.json](relay-frame-cadence.json) | `b3773c9ee0ff1d27793f471646eccfc8512e5ea64cabecf868d2be6dc3cf1d7b` |
| [resume-lens.png](resume-lens.png) | `084de14f29b40f816cca1ac2ca097f07bdd765bdebe4e9849dbd7cad8f447d4a` |
| [resume-frame-cadence.json](resume-frame-cadence.json) | `807fcef145bbc9b9de347b7ddbf769e3200a56b1612ebe4be0b16b675bdbe61a` |
