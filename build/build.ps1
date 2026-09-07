# Build the dockerbundle binary into dist\.
#
#   $env:SKIP_TESTS = "true"   build without running the test suite
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "Python not found. Install Python 3.10+ and retry." }

Write-Host "Python: $python"

$scratch = Join-Path $root "build\__pycache__"
$env:PYTHONPYCACHEPREFIX = $scratch

# Editable so PyInstaller and pytest both work against this source tree rather than an
# installed copy. Dependencies come from pyproject.toml, the single place they live.
& $python -m pip install --upgrade --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "could not install dependencies" }

if ($env:SKIP_TESTS -ne "true") {
    # Docker-backed tests are excluded: a release runner has no daemon.
    & $python -m pytest -m "not docker" -q
    if ($LASTEXITCODE -ne 0) { throw "tests failed, build stopped" }
}

& $python -m PyInstaller --clean --noconfirm --distpath dist --workpath $scratch build\dockerbundle.spec
if ($LASTEXITCODE -ne 0) { throw "build failed" }

# A checksum beside each artifact, so install.ps1 can tell a truncated download from a
# good one. The release picks these up through the same RELEASE_FILES glob.
Get-ChildItem dist -File | Where-Object { $_.Extension -ne ".sha256" } | ForEach-Object {
    $sum = (Get-FileHash -Path $_.FullName -Algorithm SHA256).Hash.ToLower()
    # Two spaces: the format `sha256sum -c` and install.sh both expect.
    Set-Content -Path "$($_.FullName).sha256" -Value "$sum  $($_.Name)" -Encoding ascii -NoNewline:$false
}

Write-Host ""
Write-Host "Artifacts in $root\dist:"
Get-ChildItem dist
