#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'python3 is required to write the Firefox native-host manifest.' >&2
  exit 1
fi
cargo build --manifest-path "$repo_root/Cargo.toml" -p rustytransfer-firefox-host --release

source="$repo_root/target/release/rustytransfer-firefox-host"
if [ ! -f "$source" ]; then
  echo "Expected native host executable was not found: $source" >&2
  exit 1
fi

install_dir="${XDG_DATA_HOME:-$HOME/.local/share}/rustytransfer/firefox-host"
install -d "$install_dir"
install_dir="$(cd "$install_dir" && pwd -P)"
binary="$install_dir/rustytransfer-firefox-host"
install -m 0755 "$source" "$binary"

case "$(uname -s)" in
  Linux) manifest_dir="$HOME/.mozilla/native-messaging-hosts" ;;
  Darwin) manifest_dir="$HOME/Library/Application Support/Mozilla/NativeMessagingHosts" ;;
  *) echo 'Firefox native host installation supports Linux and macOS here.' >&2; exit 1 ;;
esac

install -d "$manifest_dir"
manifest="$manifest_dir/org.rustytransfer.host.json"
python3 - "$binary" "$manifest" <<'PY'
import json
import sys

binary, manifest = sys.argv[1:]
with open(manifest, "w", encoding="utf-8") as output:
    json.dump(
        {
            "name": "org.rustytransfer.host",
            "description": "Rustytransfer Firefox native host",
            "path": binary,
            "type": "stdio",
            "allowed_extensions": ["rustytransfer@collapsinghierarchy"],
        },
        output,
        indent=2,
    )
    output.write("\n")
PY

echo "Installed Firefox native host: $binary"
echo "Registered manifest: $manifest"
