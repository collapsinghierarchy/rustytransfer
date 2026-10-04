# RustyTransfer native desktop MVP

This is a standalone Rust workspace for the native desktop UI. It uses Iced 0.14 with its `wgpu` renderer and does not embed a WebView. The MVP replays runtime event streams and displays modeled state. It does not send files or connect to peers, relays, or signaling services.

The replay fixtures in `fixtures/` are copied from the design system examples. They describe direct transfer, relay use, and resuming after a direct path failure. They are example event data, not live telemetry. The UI must not present invented RTT, IP addresses, security claims, or throughput; facts absent from the event stream remain unknown.

## Run

With a Windows Rust toolchain installed, run from the repository root:

```powershell
cargo run --manifest-path desktop/Cargo.toml --release --locked
```

On Windows with WSLg configured, the helper invokes the Linux Rust toolchain while using a Linux target directory outside the Windows-mounted checkout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\desktop\run-wsl.ps1
```

This enables this launcher only in that PowerShell process, without changing the machine's script policy. Linux builds need the system libraries required by Iced's X11/Wayland backends and a working graphical session. WSLg can provide the graphical session on supported Windows installations. The helper does not install packages or configure WSL.

## Replay and controls

The demo provides direct, relay, and resume scenario buttons; play, pause, restart, and step; a pointer vortex; clickable nodes and routes that open a draggable information lens; and draggable peers. Tab navigates the native controls. Press I to inspect the payload, N to cycle routes and nodes, Escape to close, arrow keys to move the lens (Shift moves it farther), Space to toggle playback, R to restart, M to toggle reduced motion, and F12 to show diagnostics. Reduced motion disables the pointer vortex and moving packet animation while keeping the lens's static separation effect.

The command-line interface accepts:

```text
--help                    Show usage
--replay PATH             Load an NDJSON event stream
--scenario direct|relay|resume
                          Select a bundled demonstration
--smoke-test DIR          Save PNG screenshots and frame-cadence JSON, then exit
```

`--replay` and `--scenario` select event data for visualization only. Smoke-test screenshots are window captures saved as PNGs. The JSON records frame callback intervals during scripted lens motion; these callbacks do not measure GPU execution time and do not establish a 60 fps result.

## Checks

```powershell
cargo test --manifest-path desktop/Cargo.toml --locked
cargo fmt --manifest-path desktop/Cargo.toml -- --check
cargo clippy --manifest-path desktop/Cargo.toml --all-targets --locked -- -D warnings
```

The separate `.github/workflows/desktop.yml` checks formatting, tests, Clippy, and release builds on Windows and Linux. It retains a Windows preview executable. Frame cadence remains a local report, with no performance threshold or regression gate. Existing transfer, security, and performance workflows keep their current scope.

The renderer adapts the equations and semantic colors from the local design-system reference, **v1.4.2 Coherent Field Lens** (`assets/design_system/index.html`, SHA-256 `9247f8521eb522fb2c071bddf749d2075d89953103628fad6f3bf0262af36e20`). The lens dimensions are scaled for the native window. Both route hit testing and rendering use the same pointer/lens deformation; static geometry stays cached during lens motion. No ignored asset files are needed to build this crate.

This is the visual foundation for a desktop client. A live adapter to the production transfer core, file selection/send/receive controls, packaging/signing, and screen-reader descriptions for the custom field are still future work. The current parser accepts the bundled event schema and reports unsupported event types as errors; it is not a live telemetry service.
