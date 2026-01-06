Param(
  [string]$RepoUrl = "https://github.com/collapsinghierarchy/rustytransfer.git",
  [string]$Ref = "main",
  [string]$InstallDir = "$env:USERPROFILE\.local\bin"
)

$ErrorActionPreference = "Stop"

function Have-Cmd($name) {
  $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

Write-Host "== rustytransfer install from source =="

if (-not (Have-Cmd git)) {
  Write-Host "ERROR: git is required. Install Git for Windows first."
  exit 1
}

if (-not (Have-Cmd cargo) -or -not (Have-Cmd rustc)) {
  Write-Host @"
ERROR: Rust toolchain not found.

Install Rust (rustup) from: https://rustup.rs
Then reopen your terminal and re-run this script.
"@
  exit 1
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$WorkDir = "$env:LOCALAPPDATA\rustytransfer-src"

if (Test-Path "$WorkDir\.git") {
  Write-Host "Updating existing checkout in $WorkDir"
  git -C $WorkDir fetch --tags --prune
  git -C $WorkDir checkout $Ref
  try { git -C $WorkDir pull --ff-only } catch {}
} else {
  Write-Host "Cloning $RepoUrl into $WorkDir"
  if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
  git clone $RepoUrl $WorkDir
  git -C $WorkDir checkout $Ref
}

Write-Host "Building (release)…"
cargo -C $WorkDir build --release

$BinSrc = Join-Path $WorkDir "target\release\rustytransfer.exe"
if (-not (Test-Path $BinSrc)) {
  Write-Host "ERROR: expected binary at $BinSrc"
  Write-Host "If your binary has a different name, edit `$BinSrc in this script."
  exit 1
}

$Dest = Join-Path $InstallDir "rustytransfer.exe"
Copy-Item -Force $BinSrc $Dest

Write-Host ""
Write-Host "Installed: $Dest"
Write-Host "Try: rustytransfer --help"
Write-Host ""
Write-Host "NOTE: Ensure $InstallDir is on your PATH."
Write-Host "Quick check: `$env:Path -split ';' | Select-String -SimpleMatch '$InstallDir'"
