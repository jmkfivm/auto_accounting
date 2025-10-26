# setup-and-run.ps1
# One-click bootstrap for user/auto_accounting on Windows using uv

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ----------------- CONFIG -----------------
$RepoOwner   = 'user'
$RepoName    = 'auto_accounting'
$Branch      = 'main'
$RepoUrl     = "https://github.com/$RepoOwner/$RepoName.git"
$AppDir      = Join-Path $env:USERPROFILE $RepoName
$Requirements = 'requirements.txt'   # fallback to explicit list if missing
# ------------------------------------------

function Write-Info($msg){ Write-Host "[i] $msg" -ForegroundColor Cyan }
function Write-Ok($msg){ Write-Host "[✓] $msg" -ForegroundColor Green }
function Write-Warn($msg){ Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg){ Write-Host "[x] $msg" -ForegroundColor Red }

# Ensure ExecutionPolicy allows script (user scope only)
try {
  $cur = Get-ExecutionPolicy -Scope CurrentUser -ErrorAction SilentlyContinue
  if ($cur -eq $null -or $cur -eq 'Undefined') {
    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
    Write-Ok "Execution policy set to RemoteSigned (CurrentUser)."
  }
} catch { Write-Warn "Could not set execution policy (continuing): $($_.Exception.Message)" }

# -------- Ensure Git --------
function Ensure-Git {
  if (Get-Command git -ErrorAction SilentlyContinue) { Write-Ok "Git found."; return }
  Write-Info "Git not found. Installing via winget..."
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Err "winget is not available. Please install Git manually from https://git-scm.com/download/win"
    exit 1
  }
  winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Err "Git installation failed or not on PATH."
    exit 1
  }
  Write-Ok "Git installed."
}

# -------- Ensure uv --------
function Find-Uv {
  $cmd = Get-Command uv -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $candidates = @(
    "$env:LOCALAPPDATA\Programs\uv\uv.exe",
    "$env:USERPROFILE\.local\bin\uv.exe",
    "$env:USERPROFILE\.cargo\bin\uv.exe"
  )
  foreach ($p in $candidates) { if (Test-Path $p) { return $p } }
  return $null
}
function Ensure-Uv {
  $global:UvExe = Find-Uv
  if ($UvExe) { Write-Ok "uv found at $UvExe"; return }
  Write-Info "Installing uv..."
  try {
    iwr https://astral.sh/uv/install.ps1 -UseBasicParsing | iex
  } catch {
    Write-Err "uv install script failed: $($_.Exception.Message)"
    exit 1
  }
  $global:UvExe = Find-Uv
  if (-not $UvExe) {
    Write-Err "uv installed but not found on PATH yet. Re-open PowerShell or add uv to PATH."
    exit 1
  }
  Write-Ok "uv installed at $UvExe"
}

# -------- Repo clone / update --------
function Ensure-Repo {
  if (-not (Test-Path $AppDir)) {
    Write-Info "Cloning $RepoUrl → $AppDir"
    git clone --branch $Branch $RepoUrl $AppDir
    Write-Ok "Cloned repository."
  } else {
    Write-Info "Updating repository at $AppDir"
    Push-Location $AppDir
    try {
      git fetch origin $Branch
      git reset --hard "origin/$Branch"
      Write-Ok "Repository updated to latest $Branch."
    } finally { Pop-Location }
  }
}

# -------- Install deps + Playwright browsers --------
function Setup-Deps {
  Push-Location $AppDir
  try {
    if (Test-Path $Requirements) {
      & $UvExe pip install -r $Requirements
    } else {
      Write-Warn "requirements.txt not found; installing minimal set."
      & $UvExe pip install playwright python-dotenv requests assemblyai
    }
    # Install Playwright browsers for headed runs (Windows: no --with-deps)
    & $UvExe run python -m playwright install
    Write-Ok "Dependencies and browsers installed."
  } finally { Pop-Location }
}

# -------- Run app --------
function Run-App {
  Push-Location $AppDir
  try {
    $entry = "app\main.py"
    if (-not (Test-Path $entry)) {
      Write-Err "Entry file '$entry' not found in $AppDir."
      exit 1
    }
    Write-Info "Launching app (headed)..."
    # Tip: set env var to force headed in your code if you key off it.
    $env:PW_HEADLESS = "0"
    & $UvExe run python $entry
  } finally { Pop-Location }
}

# ----------------- MAIN -----------------
Ensure-Git
Ensure-Uv
Ensure-Repo
Setup-Deps
Run-App
# ----------------------------------------
