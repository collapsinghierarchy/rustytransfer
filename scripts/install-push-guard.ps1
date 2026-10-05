# Run with: powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-push-guard.ps1
$ErrorActionPreference = 'Stop'
$repoRoot = (& git -C $PSScriptRoot rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot locate this Git repository.' }
$existingHooks = & git -C $repoRoot config --get core.hooksPath
if ($existingHooks) {
    throw 'An existing hooksPath is configured. Integrate the Betterleaks pre-push hook with it before enabling this guard.'
}
$installedHook = (& git -C $repoRoot rev-parse --git-path hooks/pre-push).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot locate the Git hooks directory.' }
if (![System.IO.Path]::IsPathRooted($installedHook)) { $installedHook = Join-Path $repoRoot $installedHook }
if ((Test-Path -LiteralPath $installedHook) -and !(Select-String -LiteralPath $installedHook -SimpleMatch '# rustytransfer-betterleaks-dispatch-v1' -Quiet)) {
    throw 'An existing pre-push hook is installed. Integrate it with the Betterleaks hook before replacing it.'
}
$version = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'betterleaks-version.txt') -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid pinned Betterleaks version.' }
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
$asset = "betterleaks_${version}_windows_${arch}.zip"
$toolRoot = Join-Path $repoRoot 'target/security-tools'
$destination = Join-Path $toolRoot "betterleaks-$version-windows-$arch"
$scanner = Join-Path $destination 'betterleaks.exe'
New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
if (!(Test-Path -LiteralPath $scanner)) {
    $release = "https://github.com/betterleaks/betterleaks/releases/download/v$version"
    $archive = Join-Path $toolRoot $asset
    $checksums = Join-Path $toolRoot "checksums-$version.txt"
    Invoke-WebRequest -Uri "$release/$asset" -OutFile $archive
    Invoke-WebRequest -Uri "$release/checksums.txt" -OutFile $checksums
    $lines = @(Get-Content -LiteralPath $checksums | Where-Object { $_ -match ('\s+\*?' + [regex]::Escape($asset) + '$') })
    if ($lines.Count -ne 1) { throw 'Pinned release checksum is missing or ambiguous.' }
    $expected = ($lines[0] -split '\s+')[0]
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if ($actual -ne $expected) { throw 'Betterleaks download checksum mismatch.' }
    Expand-Archive -LiteralPath $archive -DestinationPath $destination -Force
}
$toolVersion = & $scanner --version
if ($LASTEXITCODE -ne 0 -or $toolVersion.Trim() -notin @("betterleaks version $version", "betterleaks version v$version")) {
    throw 'Installed Betterleaks version does not match the repository pin.'
}
$commonGitDir = (& git -C $repoRoot rev-parse --path-format=absolute --git-common-dir).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot locate the shared Git directory.' }
$primaryRoot = Split-Path $commonGitDir
$dispatcher = Join-Path $primaryRoot '.githooks/dispatch-pre-push'
$hookText = (Get-Content -LiteralPath $dispatcher -Raw).Replace("`r`n", "`n")
New-Item -ItemType Directory -Path (Split-Path $installedHook) -Force | Out-Null
[System.IO.File]::WriteAllText($installedHook, $hookText, [System.Text.UTF8Encoding]::new($false))
Write-Output "Enabled Betterleaks $version pre-push guard for $repoRoot."
Write-Output 'Run the full history check from Git Bash: sh scripts/check-betterleaks.sh'
