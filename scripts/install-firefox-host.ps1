param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'rustytransfer\firefox-host')
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $repoRoot 'Cargo.toml'
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
  throw 'cargo was not found. Install the Rust toolchain for Windows first.'
}

& cargo build --manifest-path $manifestPath -p rustytransfer-firefox-host --release
if ($LASTEXITCODE -ne 0) {
  throw 'Building the Firefox native host failed.'
}

$source = Join-Path $repoRoot 'target\release\rustytransfer-firefox-host.exe'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
  throw "Expected Windows executable was not found: $source"
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$binary = Join-Path $InstallDir 'rustytransfer-firefox-host.exe'
Copy-Item -LiteralPath $source -Destination $binary -Force

$hostManifest = Join-Path $InstallDir 'org.rustytransfer.host.json'
$json = @{
  name = 'org.rustytransfer.host'
  description = 'Rustytransfer Firefox native host'
  path = $binary
  type = 'stdio'
  allowed_extensions = @('rustytransfer@collapsinghierarchy')
} | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($hostManifest, $json, [System.Text.UTF8Encoding]::new($false))

$registryKey = 'HKCU:\Software\Mozilla\NativeMessagingHosts\org.rustytransfer.host'
New-Item -Path $registryKey -Force | Out-Null
Set-Item -Path $registryKey -Value $hostManifest

Write-Host "Installed Firefox native host: $binary"
Write-Host "Registered manifest: $hostManifest"
