#!/usr/bin/env bash
set -euo pipefail

BIN_NAME="${BIN_NAME:-rustytransfer}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"

if [ ! -f "Cargo.toml" ]; then
  echo "ERROR: Cargo.toml not found. Run this script from the repo root."
  exit 1
fi

if ! command -v cargo >/dev/null 2>&1; then
  echo "ERROR: cargo not found. Install Rust from https://rustup.rs"
  exit 1
fi

echo "Building (release)…"
cargo build --release

BIN_SRC="target/release/${BIN_NAME}"
if [ ! -f "$BIN_SRC" ]; then
  echo "ERROR: expected binary at $BIN_SRC"
  echo "If your binary name differs, run: BIN_NAME=<name> $0"
  exit 1
fi

mkdir -p "$INSTALL_DIR"
echo "Installing to $INSTALL_DIR/${BIN_NAME}"
install -m 0755 "$BIN_SRC" "$INSTALL_DIR/${BIN_NAME}"

echo
echo "Installed: $INSTALL_DIR/${BIN_NAME}"
echo "Try: ${BIN_NAME} --help"
echo
echo "NOTE: Ensure $INSTALL_DIR is on your PATH."

# Suggest how to add it (only if it's not already on PATH)
if [ -d "$INSTALL_DIR" ] && ! echo ":$PATH:" | grep -q ":$INSTALL_DIR:"; then
  echo
  echo "To add it for *this* terminal session:"
  echo "  export PATH=\"$INSTALL_DIR:\$PATH\""
  echo
fi