# Native desktop roadmap

The current [desktop MVP](../desktop/README.md) is an Iced/wgpu event-replay UI.
Its lens animation, route selection, dragging, contextual actions and explicit
LAN layout work with fixtures. It does not discover peers or transfer files.
The earlier desktop draft remains a separate worktree.

## Next integration

1. Adapt native/transfer events to the existing desktop model. Display only facts
   present in those events; do not invent addresses, RTT, trust or throughput.
2. Add file selection, direct send/receive, progress and cancellation using the
   existing transfer engine. Keep cryptography, payload framing, verified-prefix
   resume, error categories and durable output finalization unchanged.
3. Add a persistent tray process and Windows file-manager integration after
   single-transfer behavior and process lifecycle are validated. Selected files
   may form a sequential queue; parallel payload transfers need separate review.
4. Design authenticated device pairing and incoming-transfer consent before a
   background listener. A saved peer ID is not authorization to receive a file.

Shared services belong in the native/transfer crates and must remain independent
of the desktop renderer. Keep the CLI and Firefox native-messaging interface
working, with no dependency on the desktop process. Keep portable shared code;
Windows is the first desktop integration target.

## Validation and boundaries

- Retain model, interaction, hit-testing and shader checks. Test real transfers,
  cancellation, reconnect/resume and output safety when the live adapter lands.
- Add accessibility descriptions for the custom canvas; retain keyboard controls
  and reduced-motion behavior. Frame callbacks do not measure GPU execution time.
- Package and sign the desktop client independently of the existing CLI release
  and submitted Firefox-extension artifacts.
- A direct invite bypasses the Rustytransfer backend; it does not guarantee a
  direct payload path or disable Iroh relay fallback. Only explicit network
  evidence may place a peer in the local-network group.
- Mobile clients, folders, cloud storage, offline delivery, automatic contact
  trust and a system daemon are outside the current scope.

Performance work uses the [maintained Rustytransfer-only baseline](../benchmarks/README.md).
It remains report-only pending hosted-runner calibration. Historical WAN/Croc
measurements are documented in the [efficiency report](direct-transfer-efficiency-report.md).
The earlier detailed implementation plan and the retired parallel-transfer
experiment are recoverable through the [archive index](../benchmarks/archives/README.md).
