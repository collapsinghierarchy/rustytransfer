# rustytransfer

Peer-to-peer file transfer over WebRTC with password-based authentication (MAC-via-PAKE) and end-to-end encryption (ML-KEM+AES-GCM). Similar to [croc](https://github.com/schollz/croc) and [wormhole](https://github.com/magic-wormhole/magic-wormhole), but with quantum-safe features and in rust (and less features). Somewhat similar to [noisytransfercli](github.com/collapsinghierarchy/noisytransfercli), which is based on short authentication strings instead of PAKEs and is hella slow and hella large and is written in JS (and instead of porting it to TS (which is necessary), the author decided instead to redo it in rust -> hence rustytransfer (yikes...)). Both share, however, the same signaling [wrtc back-end](https://github.com/collapsinghierarchy/nt-backend-wrtc).

`rustytransfer` is a CLI tool that establishes a WebRTC data channel between two peers and streams the encrypted file directly from sender to receiver. A lightweight rendezvous service is used only for pairing and WebRTC signaling. The back-end 

> Status: **alpha**. The protocol and implementation are under active development and have not been security-audited, but reviewed by a cryptographer (whatever that means to you). Also everything may change without notice and yada yada.

---

## How pairing works

The sender requests a **4-digit rendezvous code** from the backend and generates (or accepts) a **5-letter uppercase password**. Together they form the share code:

**`NNNN-ABCDE`**

- `NNNN` is redeemed by the receiver to obtain the WebRTC room/app ID
- `ABCDE` is used as the PAKE password

You share **only** `NNNN-ABCDE` with the receiver. Regarding the explanation of the security guarantees there is a blog article in preparation that will soon appear on my [Blog](https://whitenoise.systems/).

---

## Installation

### Option A: Download a prebuilt binary (recommended)

Go to GitHub Releases and download the appropriate asset for your OS/architecture.

Example (Linux/macOS) pattern:

```bash
TAG="alpha-0.0.1"
ASSET="rustytransfer-<platform>-<arch>"  # replace with the actual asset filename
curl -L -o rustytransfer "https://github.com/collapsinghierarchy/rustytransfer/releases/download/${TAG}/${ASSET}"
chmod +x ./rustytransfer
./rustytransfer --help
```

### Option B: Build from source (Rust toolchain required)

```bash
git clone https://github.com/collapsinghierarchy/rustytransfer.git
cd rustytransfer
cargo build --release
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
```

### Receive

```bash
rustytransfer recv --code 1234-ABCDE --out ./received.bin
```

- Redeems `1234` to obtain the room/app ID
- Uses `ABCDE` as the PAKE password
- Streams the decrypted file directly to `--out`

---

## Network requirements

- Outbound HTTPS/WSS access to the rendezvous/signaling service:
  - `https://nt.whitenoise.systems`
  - `wss://nt.whitenoise.systems`
- WebRTC connectivity depends on local network/NAT behavior.
---

## Use of AI
- Early throw-away unit and integration tests.
- CLI wiring (bin crate) is almost 90% chatgpt 5.2.
- The cli_ui crate is also 80-90% chatgpt 5.2.
- The transport crate may contain up to 50% chatgpt 5.2 generated code.
- Install scripts are 100% chatgpt 5.2 generated.

## Acknowledgements

- WebRTC transport via Rust crates in the ecosystem
- Rendezvous service hosted at `nt.whitenoise.systems`
