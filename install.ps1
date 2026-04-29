# Xelos Edge installer for Windows.
#
# Run in PowerShell (one-liner):
#   iwr -useb https://raw.githubusercontent.com/markmelnic/xelos-edge/main/install.ps1 | iex
#
# What it does:
#   1. Verifies a Python 3.11+ interpreter is available (offers to install
#      via winget if not).
#   2. Creates an isolated venv at %USERPROFILE%\.xelos\runtime.
#   3. Installs the `xelos-edge` package into that venv.
#   4. Drops a `xelos.cmd` shim into %USERPROFILE%\.xelos\bin and adds
#      that directory to the user PATH.
#
# Re-running is safe.

$ErrorActionPreference = "Stop"

$XelosHome   = if ($env:XELOS_HOME) { $env:XELOS_HOME } else { Join-Path $env:USERPROFILE ".xelos" }
$RuntimeDir  = Join-Path $XelosHome "runtime"
$BinDir      = Join-Path $XelosHome "bin"
$PackageSpec = if ($env:XELOS_PACKAGE_SPEC) { $env:XELOS_PACKAGE_SPEC } else { "git+https://github.com/markmelnic/xelos-edge.git@main" }

function Write-Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "OK  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "x   $msg" -ForegroundColor Red; exit 1 }

function Find-Python {
    # Prefer the `py` launcher — it picks the highest installed version.
    $candidates = @(
        @{ Cmd = "py"; Args = @("-3.13", "-V") },
        @{ Cmd = "py"; Args = @("-3.12", "-V") },
        @{ Cmd = "py"; Args = @("-3.11", "-V") },
        @{ Cmd = "py"; Args = @("-3", "-V") },
        @{ Cmd = "python3"; Args = @("-V") },
        @{ Cmd = "python"; Args = @("-V") }
    )
    foreach ($c in $candidates) {
        try {
            $output = & $c.Cmd $c.Args 2>&1
            if ($LASTEXITCODE -eq 0 -and $output -match 'Python\s+3\.(11|12|13|14|15)\b') {
                if ($c.Cmd -eq "py") {
                    return @{ Cmd = "py"; Switch = $c.Args[0] }
                }
                return @{ Cmd = $c.Cmd; Switch = $null }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments=$true)] [string[]] $Args)
    if ($script:PythonInfo.Switch) {
        & $script:PythonInfo.Cmd $script:PythonInfo.Switch @Args
    } else {
        & $script:PythonInfo.Cmd @Args
    }
}

# --- Main ------------------------------------------------------------------
Write-Info "Xelos Edge installer for Windows"

$script:PythonInfo = Find-Python
if ($null -eq $script:PythonInfo) {
    Write-Warn "No Python 3.11+ found on PATH."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $ans = Read-Host "Install Python 3.12 via winget now? [Y/n]"
        if ($ans -ne "n" -and $ans -ne "N") {
            winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
            Write-Info "Reload your shell after winget finishes, then re-run this installer."
            exit 0
        }
    }
    Write-Fail "Install Python 3.11+ from https://www.python.org/downloads/ and re-run."
}
Write-Ok ("Found Python: " + (Invoke-Python -V))

if (-not (Test-Path $XelosHome)) {
    New-Item -ItemType Directory -Path $XelosHome -Force | Out-Null
}

if (-not (Test-Path $RuntimeDir)) {
    Write-Info "Creating runtime venv at $RuntimeDir"
    Invoke-Python -m venv $RuntimeDir
} else {
    Write-Info "Reusing runtime venv at $RuntimeDir"
}

$VenvPython = Join-Path $RuntimeDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Fail "venv created but python.exe not found at $VenvPython"
}

Write-Info "Installing xelos-edge..."

# Ensure pip is available in the venv. ensurepip is the canonical bootstrap;
# if that fails, try host pip / pip3 as a last resort.
& $VenvPython -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    & $VenvPython -m ensurepip --upgrade *> $null
    if ($LASTEXITCODE -ne 0) {
        foreach ($sysPip in @("pip3", "pip")) {
            if (Get-Command $sysPip -ErrorAction SilentlyContinue) {
                & $sysPip install --quiet --upgrade --target=(Join-Path $RuntimeDir "Lib\site-packages") pip
                break
            }
        }
    }
}

& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet --upgrade $PackageSpec
Write-Ok "Package installed"

# --- Shim in BinDir --------------------------------------------------------
if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

$ShimPath = Join-Path $BinDir "xelos.cmd"
$VenvXelos = Join-Path $RuntimeDir "Scripts\xelos.exe"

@"
@echo off
"$VenvXelos" %*
"@ | Set-Content -Path $ShimPath -Encoding ASCII
Write-Ok "Launcher: $ShimPath"

# --- Add BinDir to user PATH ----------------------------------------------
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $UserPath) { $UserPath = "" }
$paths = $UserPath -split ";" | Where-Object { $_ -ne "" }

if ($paths -notcontains $BinDir) {
    $newPath = if ($UserPath.TrimEnd(';') -eq "") { $BinDir } else { "$($UserPath.TrimEnd(';'));$BinDir" }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    $env:Path = "$env:Path;$BinDir"
    Write-Ok "Added $BinDir to your user PATH (open a new terminal to pick it up)."
} else {
    Write-Ok "$BinDir already on PATH"
}

Write-Host ""
Write-Ok "Done."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Generate a pair code in the Xelos UI under Devices."
Write-Host "  2. Run:  xelos pair <CODE> --api <api-base-url>"
Write-Host "  3. Then: xelos serve"
