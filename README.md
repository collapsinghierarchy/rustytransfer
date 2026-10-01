# rustytransfer

Peer-to-peer file transfer over WebRTC or Iroh with short-code PAKE or direct Iroh invites and end-to-end encryption (ML-KEM+AES-GCM). Similar to [croc](https://github.com/schollz/croc) and [wormhole](https://github.com/magic-wormhole/magic-wormhole), but with quantum-safe features and in rust (and less features).

> Status: **alpha**. The protocol and implementation are under active development and have not been security-audited, but reviewed by a cryptographer (whatever that means to you). Also everything may change without notice and yada yada.

---

## How pairing works

The sender requests a **4-digit rendezvous code** from the backend and generates (or accepts) a **5-letter uppercase password**. Together they form the share code:

**`NNNN-ABCDE`**

- `NNNN` is redeemed by the receiver to obtain the room/app ID
- `ABCDE` is used as the PAKE password

You share **only** `NNNN-ABCDE` with the receiver.

For a direct Iroh transfer, the sender instead shares one invite in the form
`rt1:<sender EndpointId>:<per-transfer token>`. Iroh authenticates the sender's
ID; the random token authorizes this particular file transfer. The direct
mode uses Iroh's public lookup/relays and does not contact the rustytransfer
backend or run PAKE. Keep the complete invite private until the transfer ends.

Save and list peer IDs locally:

```bash
rustytransfer contacts add alice '<EndpointId-or-direct-invite>'
rustytransfer contacts list
rustytransfer contacts remove alice
```

The address book stores the peer ID and name; when given a direct invite, it
discards the per-transfer token. A saved contact ID does not replace the token
needed to authorize a transfer.

---

## Installation

### Option A: Download a prebuilt package (recommended)

Open [GitHub Releases](https://github.com/collapsinghierarchy/rustytransfer/releases)
and download the archive for your OS and architecture. Extract it and run
`rustytransfer` (or `rustytransfer.exe` on Windows). New packages also include
the Firefox native host and its installer; follow the included `INSTALL.txt`.

### Option B: Build from source (Rust toolchain required)

```bash
git clone https://github.com/collapsinghierarchy/rustytransfer.git
cd rustytransfer
cargo build -p rustytransfer --release
./target/release/rustytransfer --help
```

### Option C: Build + install locally

These scripts build whatever is currently checked out and install it.

#### Linux/macOS

```bash
./install-local.sh
```

#### Windows (PowerShell)

```powershell
.\install-local.ps1
```

By default, the scripts install to:

- Linux/macOS: `~/.local/bin`
- Windows: `%USERPROFILE%\.local\bin`

Make sure that directory is on your `PATH`.

### Workspace packages

The root `rustytransfer` package provides the CLI and Rust compatibility
exports. The implementation is split into `rustytransfer-crypto` (PAKE and
encryption), `rustytransfer-protocol` (wire messages and state machines),
`rustytransfer-transfer` (file transfer and metrics), and
`rustytransfer-native` (rendezvous, signaling, WebRTC, and Iroh).
`rustytransfer-wasm` contains the JavaScript PAKE adapter and builds as a
`cdylib` for WebAssembly. `rustytransfer-firefox-host` is the separate desktop
native messaging process used by the Firefox extension.

### Firefox desktop extension (direct Iroh preview)

The Firefox extension uses a local native host for file access and direct Iroh
transfers. Install the host on the **same operating system as Firefox**. From a
GitHub release archive, run the included `install-firefox-host` script; it uses
the bundled binary and does not need Rust. From a source checkout, these scripts
build the host first:

```powershell
# Windows PowerShell, from the repository root
.\scripts\install-firefox-host.ps1
```

```bash
# Linux or macOS, from the repository root
bash scripts/install-firefox-host.sh
```

For Windows Firefox, use the Windows installer rather than installing the host
inside WSL. The Linux/macOS installer also needs Python 3 to write the host
manifest. Install the Firefox add-on from
[Mozilla Add-ons](https://addons.mozilla.org/firefox/addon/rustytransfer/).
The installer registers the host for the extension ID in the add-on manifest;
Firefox requires this separate native-host registration. See
[Mozilla's native messaging guide](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_messaging).

Choose **Send** to pick a file and copy its direct invite. On the other peer,
paste that invite into **Receive** and choose the destination. This preview uses
public Iroh connectivity and does not contact the rustytransfer backend or use
PAKE. Keep the invite private. If a receive is interrupted, choose the same
destination with a fresh invite to verify and resume the saved partial file.

### Probe persistent Iroh IDs and public relays

This standalone probe uses Iroh's public `N0` address lookup and relay without
the rustytransfer backend or PAKE. It exchanges only `PING`/`PONG`; it does not
transfer files. Run it on two machines, or use two distinct key files locally.

```powershell
$key = Join-Path $env:LOCALAPPDATA 'rustytransfer\iroh-probe-listener.key'
cargo run -p rustytransfer-native --bin iroh-probe --no-default-features --features iroh -- --key-file $key --relay-only listen
```

Copy the printed `Local EndpointId` to the other peer:

```powershell
$key = Join-Path $env:LOCALAPPDATA 'rustytransfer\iroh-probe-dialer.key'
cargo run -p rustytransfer-native --bin iroh-probe --no-default-features --features iroh -- --key-file $key --relay-only dial 'PASTE_ENDPOINT_ID_HERE'
```

Each side prints the authenticated peer ID and selected path. `path: relay`
confirms relay traffic. Run the following twice to confirm that the EndpointId
survives a restart:

```powershell
cargo run -p rustytransfer-native --bin iroh-probe --no-default-features --features iroh -- --key-file $key id
```

Omit `--relay-only` to allow direct IP paths. Keep the key file private.

---

## Usage

### Send

### TUI File Picker
```bash
rustytransfer send 
```
After the file is selected
- Prints a share code like: `1234-ABCDE`
- The receiver uses that code to connect
### CLI

```bash
rustytransfer send --file /path/to/file
```

- Prints a share code like: `1234-ABCDE`
- The receiver uses that code to connect

Optional flags:

- `--transport <webrtc|iroh>` selects the data transport. It defaults to `webrtc`; both peers must use the same value.
- `--direct` starts a direct Iroh transfer and prints a copyable invite instead of a share code.
- `--identity-file <path>` selects the sender's persistent Iroh key file for direct transfers. By default it is stored under the user's data directory.
- `--password ABCDE` (exactly 5 uppercase letters, otherwise one is generated.)
- `--pick` an explicit flag to open the terminal file picker
- `--chunk-size <bytes>` to tune streaming chunk size (advanced)

Examples:

```bash
# Let rustytransfer generate a 5-letter password and print the full share code
rustytransfer send --file ./example.zip

# Force a specific 5-letter password
rustytransfer send --file ./example.zip --password ABCDE

# Pick a file with the terminal UI
rustytransfer send --pick

# Use Iroh for both sides instead of the default WebRTC transport
rustytransfer --transport iroh send --file ./example.zip
rustytransfer --transport iroh recv --code 1234-ABCDE --out ./received.bin

# Bypass the rendezvous backend and PAKE
rustytransfer send --direct --file ./example.zip
rustytransfer recv --invite 'rt1:<sender-id>:<one-time-token>' --out ./received.bin
```

If requesting a rendezvous code fails, `send` switches to direct Iroh mode
automatically. It prints only a direct invite after Iroh is publicly reachable;
it does not print a short PAKE code. The receiver then uses `--invite`.

### Receive

```bash
rustytransfer recv --code 1234-ABCDE --out ./received.bin
```

- Redeems `1234` to obtain the room/app ID
- Uses `ABCDE` as the PAKE password
- Streams the decrypted file directly to `--out`

---

## Transfer reliability

- **Output safety:** The receiver saves incoming bytes in
  `.<output-name>.rustytransfer.part` next to the requested output. It publishes
  the file only after the transfer is confirmed and never overwrites an existing
  destination. The partial file contains plaintext; delete it to discard an
  unfinished transfer.
- **Resume:** Run `recv` again with the same `--out` path and a new share code or
  direct Iroh invite. The receiver reports its saved prefix length and SHA3-256
  digest. The sender resumes at that offset only if the prefix matches its
  source; otherwise the authenticated transfer clears the partial file and
  starts at byte zero.
- **Reconnect:** After a transient connection failure, `send` and `recv` retry
  up to three times after 1, 2, and 4 seconds. Each attempt creates a fresh
  encrypted session. Authentication, protocol, and file errors stop immediately.
- **Diagnostics:** Failed transfers report a stable `RTY-*` code and an exit
  code for the failure category. Set `RUSTYTRANSFER_METRICS_JSONL` to log
  successful and failed transfers, including phase, error code, byte count,
  and completion state. `--verbose` adds redacted diagnostic context.

Use the same version on both peers; older builds are incompatible with this
transfer protocol.

## Network requirements

- Share codes require outbound HTTP/WebSocket access to the rendezvous backend.
  Set `RUSTYTRANSFER_BACKEND_URL` to override its base URL, for example
  `https://transfer.example`.
- WebRTC connectivity depends on local network and NAT behavior when using
  `--transport webrtc`.
- Iroh uses its QUIC and relay paths when using `--transport iroh`.
- Direct invites use Iroh's public `N0` lookup and relays, even when the
  rustytransfer backend is offline. Relay traffic remains end-to-end encrypted.

---

## Use of AI

- Early throw-away unit and integration tests.
- CLI wiring (bin crate) is almost 90% chatgpt 5.2.
- The cli_ui crate is also 80-90% chatgpt 5.2.
- The transport crate may contain up to 50% chatgpt 5.2 generated code.
- Install scripts are 100% chatgpt 5.2 generated.

## Acknowledgements

- WebRTC transport via Rust crates in the ecosystem
- Rendezvous service backend
