<#
.SYNOPSIS
    Install the dockerbundle binary from a GitHub release.

.DESCRIPTION
    Windows counterpart of install.sh, with the same contract.

        irm https://raw.githubusercontent.com/cat-of-summer/DockerBundle---python/v1.2.3/install.ps1 | iex
        .\install.ps1 v1.2.3

    The version is required on purpose. A stand that silently rebuilds with a different
    generator is the thing this replaces: pinning the tag puts a generator upgrade in the
    diff of a repository variable rather than in nobody's hands.

.PARAMETER Version
    Release tag to install, e.g. v1.2.3. Falls back to $env:DOCKERBUNDLE_VERSION.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Version = $env:DOCKERBUNDLE_VERSION,

    [Alias('d')]
    [string]$Dir = $env:DOCKERBUNDLE_INSTALL_DIR,

    [string]$Repo = $(if ($env:DOCKERBUNDLE_REPO) { $env:DOCKERBUNDLE_REPO } else { 'cat-of-summer/DockerBundle---python' }),

    [string]$Sha256 = $env:DOCKERBUNDLE_SHA256
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Info { param([string]$Message) Write-Host "dockerbundle: $Message" }
function Write-Warn { param([string]$Message) Write-Warning "dockerbundle: $Message" }

if (-not $Version) {
    Write-Host @'
usage: install.ps1 <version> [-Dir DIR] [-Sha256 HEX] [-Repo OWNER/NAME]

  <version>   release tag to install, e.g. v1.2.3

Pin the tag in your CI configuration so the generator cannot change under you:

  SETUP_COMMAND = irm https://raw.githubusercontent.com/cat-of-summer/DockerBundle---python/v1.2.3/install.ps1 | iex
'@
    exit 2
}

$baseUrl = if ($env:DOCKERBUNDLE_BASE_URL) {
    $env:DOCKERBUNDLE_BASE_URL
} else {
    "https://github.com/$Repo/releases/download"
}

# Mirrors build/dockerbundle.spec, which is where the artifact names are decided.
$arch = switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { 'x64' }
    'ARM64' { 'arm64' }
    'x86'   { 'x86' }
    default { throw "dockerbundle: unsupported architecture: $($env:PROCESSOR_ARCHITECTURE)" }
}

# Releases cut before the asset names were fixed carry the truncated form
# (`windows-x64.exe` rather than `dockerbundle-windows-x64.exe`), so both are tried.
$candidates = @("dockerbundle-windows-$arch.exe", "windows-$arch.exe")

$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("dockerbundle-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null

try {
    $binary = Join-Path $temp 'binary.exe'
    $asset = $null
    foreach ($candidate in $candidates) {
        try {
            Invoke-WebRequest -Uri "$baseUrl/$Version/$candidate" -OutFile $binary -UseBasicParsing
            $asset = $candidate
            break
        } catch {
            continue
        }
    }

    if (-not $asset) {
        Write-Warn "no asset for windows-$arch in release $Version of $Repo"
        Write-Warn "tried: $($candidates -join ', ')"
        throw "dockerbundle: check that the tag exists and that it has a build for this platform"
    }

    Write-Info "downloaded $asset from $Version"

    # Only against a digest the caller supplied. A checksum file served from the same
    # host as the binary proves nothing an attacker could not also forge, and HTTPS
    # already covers the transport. A digest pinned in CI is a different thing - it says
    # "these exact bytes". GitHub prints one beside every asset.
    if ($Sha256) {
        # Accept the "sha256:..." form GitHub displays as well as a bare digest.
        $expected = $Sha256 -replace '^sha256:', ''
        $actual = (Get-FileHash -Path $binary -Algorithm SHA256).Hash
        if ($actual -ine $expected) {
            Write-Warn "expected $expected"
            Write-Warn "got      $actual"
            throw "dockerbundle: checksum mismatch for $asset - refusing to install"
        }
        Write-Info 'checksum ok'
    }

    if (-not $Dir) {
        $Dir = if ($env:RUNNER_TEMP) {
            Join-Path $env:RUNNER_TEMP 'dockerbundle-bin'
        } else {
            Join-Path $env:LOCALAPPDATA 'dockerbundle\bin'
        }
    }

    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    $target = Join-Path $Dir 'dockerbundle.exe'
    Copy-Item -Path $binary -Destination $target -Force

    # On a runner the next step is a fresh shell, so the directory has to be published
    # rather than merely exported.
    if ($env:GITHUB_PATH) {
        Add-Content -Path $env:GITHUB_PATH -Value $Dir -Encoding utf8
        Write-Info "added $Dir to GITHUB_PATH"
    }

    Write-Info "installed $target"

    try {
        $reported = & $target --version 2>$null
        if ($reported) { Write-Info ($reported -join ' ') }
    } catch {
        Write-Warn 'the binary did not answer --version; it may not run on this platform'
    }
} finally {
    Remove-Item -Path $temp -Recurse -Force -ErrorAction SilentlyContinue
}
