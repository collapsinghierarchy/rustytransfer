Param(
  [string]$BinName = "rustytransfer",
  [string]$InstallDir = "$env:USERPROFILE\.local\bin"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path "Cargo.toml")) {
  Write-Host "ERROR: Cargo.toml not found. Run this script from the repo root."
  exit 1
}

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
  Write-Host "ERROR: cargo not found. Install Rust from https://rustup.rs"
  exit 1
}

Write-Host "Building (release)…"
cargo build --release

$BinSrc = Join-Path (Get-Location) ("target\release\{0}.exe" -f $BinName)
if (-not (Test-Path $BinSrc)) {
  Write-Host "ERROR: expected binary at $BinSrc"
  Write-Host "If your binary name differs: .\install-local.ps1 -BinName <name>"
  exit 1
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$Dest = Join-Path $InstallDir ("{0}.exe" -f $BinName)

Copy-Item -Force $BinSrc $Dest

Write-Host ""
Write-Host "Installed: $Dest"
Write-Host "Try: $BinName --help"
Write-Host ""
Write-Host "NOTE: Ensure $InstallDir is on your PATH (new terminal may be needed)."

# Suggest how to add it (only if it's not already on PATH)
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$EffectivePath = [Environment]::GetEnvironmentVariable("Path", "Process")

function Test-PathEntry($pathList, $entry) {
  if ([string]::IsNullOrWhiteSpace($pathList)) { return $false }
  $parts = $pathList -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
  $target = $entry.Trim().TrimEnd('\')
  foreach ($p in $parts) {
    if ($p.TrimEnd('\') -ieq $target) { return $true }
  }
  return $false
}

if (-not (Test-PathEntry $EffectivePath $InstallDir) -and (Test-Path $InstallDir)) {
  Write-Host ""
  Write-Host "To add it for *this* PowerShell session:"
  Write-Host "  `$env:Path = `"$InstallDir;`$env:Path`""
}