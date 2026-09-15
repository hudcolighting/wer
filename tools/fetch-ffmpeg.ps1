<#
.SYNOPSIS
    Downloads the pinned ffmpeg build into vendor/ffmpeg/ and verifies its hash.
.DESCRIPTION
    The ffmpeg binary is not committed (97 MB). Run this once after cloning.
    Re-running is cheap: if a correct ffmpeg.exe is already present it exits early.
    See vendor/ffmpeg/PIN.md for why this build and why not in git.
#>
[CmdletBinding()]
param(
    # Re-download even if a verified ffmpeg.exe is already in place.
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# --- The pin. Change these three together, and update vendor/ffmpeg/PIN.md. ---
$Version = '8.1.2'
$Url     = 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip'
$Sha256  = 'DB580001CAA24AC104C8CB856CD113A87B0A443F7BDF47D8C12B1D740584A2EC'

$repoRoot  = Split-Path -Parent $PSScriptRoot
$vendorDir = Join-Path $repoRoot 'vendor\ffmpeg'
$ffmpegExe = Join-Path $vendorDir 'ffmpeg.exe'

New-Item -ItemType Directory -Force -Path $vendorDir | Out-Null

if ((Test-Path $ffmpegExe) -and -not $Force) {
    $reported = & $ffmpegExe -version 2>&1 | Select-Object -First 1
    if ($reported -match [regex]::Escape($Version)) {
        Write-Host "ffmpeg $Version already present. Use -Force to re-download." -ForegroundColor Green
        exit 0
    }
    Write-Host "Found a different ffmpeg ($reported). Replacing with $Version." -ForegroundColor Yellow
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("wer-ffmpeg-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    $zip = Join-Path $tmp 'ffmpeg.zip'

    Write-Host "Downloading ffmpeg $Version (about 105 MB)..." -ForegroundColor Cyan
    Write-Host "  $Url"
    $ProgressPreference = 'SilentlyContinue'   # the progress bar makes this ~10x slower
    Invoke-WebRequest -Uri $Url -OutFile $zip -TimeoutSec 900 -UseBasicParsing

    Write-Host 'Verifying SHA256...' -ForegroundColor Cyan
    $actual = (Get-FileHash $zip -Algorithm SHA256).Hash
    if ($actual -ne $Sha256) {
        throw ("SHA256 mismatch. This archive is not the pinned build - do not use it.`n" +
               "  expected $Sha256`n  actual   $actual")
    }
    Write-Host '  OK' -ForegroundColor Green

    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $extracted = Get-ChildItem $tmp -Directory | Select-Object -First 1
    $srcExe = Join-Path $extracted.FullName 'bin\ffmpeg.exe'
    if (-not (Test-Path $srcExe)) { throw "ffmpeg.exe not found inside the archive at $srcExe" }

    Copy-Item $srcExe $ffmpegExe -Force
    # Keep the license next to the binary. GPL v3: it travels with the build.
    Copy-Item (Join-Path $extracted.FullName 'LICENSE')    (Join-Path $vendorDir 'LICENSE')    -Force
    Copy-Item (Join-Path $extracted.FullName 'README.txt') (Join-Path $vendorDir 'README.txt') -Force

    $sizeMb = [math]::Round((Get-Item $ffmpegExe).Length / 1MB, 1)
    Write-Host "Installed vendor\ffmpeg\ffmpeg.exe ($sizeMb MB)" -ForegroundColor Green
    & $ffmpegExe -version 2>&1 | Select-Object -First 1
}
finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
