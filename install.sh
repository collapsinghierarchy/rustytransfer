#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/collapsinghierarchy/rustytransfer.git}"
BRANCH_OR_TAG="${BRANCH_OR_TAG:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

echo "== rustytransfer install from source =="

if ! need_cmd git; then
  echo "ERROR: git is required. Install it with your package manager."
  exit 1
fi

if ! need_cmd cargo || ! need_cmd rustc; then
  cat <<'EOF'
ERROR: Rust toolchain not found.

Install Rust (rustup) from: https://rustup.rs
Then re-run this script.
EOF
  exit 1
fi

mkdir -p "$INSTALL_DIR"

WORKDIR="${WORKDIR:-$HOME/.cache/rustytransfer-src}"
mkdir -p "$(dirname "$WORKDIR")"

if [ -d "$WORKDIR/.git" ]; then
  echo "Updating existing checkout in $WORKDIR"
  git -C "$WORKDIR" fetch --tags --prune
  git -C "$WORKDIR" checkout "$BRANCH_OR_TAG"
  git -C "$WORKDIR" pull --ff-only || true
else
  echo "Cloning $REPO_URL into $WORKDIR"
  rm -rf "$WORKDIR"
  git clone "$REPO_URL" "$WORKDIR"
  git -C "$WORKDIR" checkout "$BRANCH_OR_TAG"
fi

echo "Building (release)…"
cargo -C "$WORKDIR" build --release

BIN_SRC="$WORKDIR/target/release/rustytransfer"
if [ ! -f "$BIN_SRC" ]; then
  echo "ERROR: expected binary at $BIN_SRC"
  echo "If your binary has a different name, edit BIN_SRC in this script."
  exit 1
fi

echo "Installing to $INSTALL_DIR"
install -m 0755 "$BIN_SRC" "$INSTALL_DIR/rustytransfer"

echo
echo "Installed: $INSTALL_DIR/rustytransfer"
echo "Try: rustytransfer --help"
echo
echo "NOTE: Ensure $INSTALL_DIR is in your PATH."
echo "Example (bash/zsh): echo 'export PATH=\$PATH:$INSTALL_DIR' >> ~/.profile"
