[CmdletBinding()]
param(
    [string] $LinuxCargoBin,
    [string] $LinuxTargetDir,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $AppArguments
)

$ErrorActionPreference = 'Stop'
$manifestPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'Cargo.toml')).Path
$wslManifestPath = (& wsl.exe -e wslpath -a $manifestPath).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($wslManifestPath)) {
    throw "Could not convert manifest path for WSL: $manifestPath"
}

$wslHome = (& wsl.exe -e printenv HOME).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($wslHome)) {
    throw 'Could not determine the WSL user home directory.'
}
if ([string]::IsNullOrWhiteSpace($LinuxCargoBin)) { $LinuxCargoBin = "$wslHome/.cargo/bin" }
if ([string]::IsNullOrWhiteSpace($LinuxTargetDir)) { $LinuxTargetDir = "$wslHome/rustytransfer-desktop-target" }
$linuxPath = "$LinuxCargoBin`:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
$cargoArgs = @(
    'run',
    '--manifest-path', $wslManifestPath,
    '--release',
    '--locked',
    '--'
)
if ($AppArguments) { $cargoArgs += $AppArguments }

& wsl.exe -e env "PATH=$linuxPath" "CARGO_TARGET_DIR=$linuxTargetDir" cargo @cargoArgs
exit $LASTEXITCODE
