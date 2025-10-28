# setup-and-run.ps1
# One-click bootstrap for jmkfivm/auto_accounting on Windows using uv
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ----------------- CONFIG -----------------
$RepoOwner    = 'jmkfivm'
$RepoName     = 'auto_accounting'
$Branch       = 'main'
$RepoUrl      = "https://github.com/$RepoOwner/$RepoName.git"
$ZipUrl       = "https://codeload.github.com/$RepoOwner/$RepoName/zip/refs/heads/$Branch"
$RawBase      = "https://raw.githubusercontent.com/$RepoOwner/$RepoName/$Branch"
$AppDir       = Join-Path $env:USERPROFILE $RepoName
$Requirements = 'requirements.txt'
# ------------------------------------------

# -------- Logging (ASCII only) --------
function Write-Info { param($msg) Write-Host ("[i] {0}" -f $msg) -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host ("[OK] {0}" -f $msg) -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host ("[!] {0}" -f $msg) -ForegroundColor Yellow }
function Write-Err  { param($msg) Write-Host ("[X] {0}" -f $msg) -ForegroundColor Red }

# -------- Helpers --------
function Ensure-Dir { param($p) if (-not (Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null } }

function Invoke-Download {
  param([string]$url, [string]$outPath)
  Write-Info "Downloading: $url"
  Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $outPath
  if (-not (Test-Path $outPath)) { throw "Download failed: $url" }
}

function Expand-ZipTo {
  param([string]$zipPath, [string]$destDir)
  Write-Info "Extracting zip to $destDir"
  if (Test-Path $destDir) { Remove-Item $destDir -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $destDir | Out-Null
  Expand-Archive -LiteralPath $zipPath -DestinationPath $destDir -Force
}

# -------- Detect Git (optional) --------
function Detect-Git {
  $g = Get-Command git -ErrorAction SilentlyContinue
  if ($g) { Write-Ok "Git found: $($g.Source)"; return $true }
  Write-Warn "Git not found. Will use zip/raw fallback."
  return $false
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
  $script:UvExe = Find-Uv
  if ($UvExe) { Write-Ok "uv found at $UvExe"; return }
  Write-Info "Installing uv..."
  iwr https://astral.sh/uv/install.ps1 -UseBasicParsing | iex
  $script:UvExe = Find-Uv
  if (-not $UvExe) { Write-Err "uv installed but not on PATH yet."; exit 1 }
  Write-Ok "uv installed at $UvExe"
}

# -------- Minimal raw fallback --------
function Fetch-Minimal {
  param([string]$rawBase, [string]$appDir)
  Write-Warn "Falling back to minimal raw-file fetch."
  Ensure-Dir $appDir
  Ensure-Dir (Join-Path $appDir 'app')

  $files = @(
    @{raw="$rawBase/requirements.txt"; dst=(Join-Path $appDir 'requirements.txt'); opt=$true},
    @{raw="$rawBase/app/main.py";      dst=(Join-Path $appDir 'app\main.py');     opt=$false},
    @{raw="$rawBase/.env.example";     dst=(Join-Path $appDir '.env.example');    opt=$true}
  )

  foreach ($f in $files) {
    try { Invoke-Download $f.raw $f.dst; Write-Ok "Saved $(Split-Path $f.dst -Leaf)" }
    catch { if (-not $f.opt) { Write-Err "Required file missing: $($f.raw)"; return $false } }
  }
  return $true
}

# -------- Acquire repository (Git -> Zip -> Raw) --------
function Ensure-Repo {
  $hasGit = Detect-Git
  if ($hasGit) {
    Write-Info "Using Git to obtain repository..."
    if (-not (Test-Path $AppDir)) {
      git clone --branch $Branch $RepoUrl $AppDir | Out-Host
    } else {
      $old = Get-Location
      Set-Location $AppDir
      git fetch origin $Branch | Out-Host
      git reset --hard "origin/$Branch" | Out-Host
      Set-Location $old
    }
    if (Test-Path $AppDir) { Write-Ok "Repository ready at $AppDir"; return }
    Write-Warn "Git step did not produce $AppDir. Trying zip..."
  }

  # Zip fallback
  $tmpZip = Join-Path $env:TEMP ("{0}-{1}.zip" -f $RepoName, $Branch)
  try {
    Invoke-Download $ZipUrl $tmpZip
    $extractRoot = Join-Path $env:TEMP ("{0}_extract_{1}" -f $RepoName, [guid]::NewGuid())
    Expand-ZipTo $tmpZip $extractRoot
    $inner = Get-ChildItem -Directory $extractRoot | Select-Object -First 1
    if ($inner) {
      if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
      Move-Item $inner.FullName $AppDir
      Remove-Item $tmpZip -Force
      Remove-Item $extractRoot -Recurse -Force
      if (Test-Path $AppDir) { Write-Ok "Repo extracted to $AppDir"; return }
    }
    Write-Warn "Zip extracted but could not move into $AppDir."
  } catch {
    Write-Warn "Zip download/extract failed: $($_.Exception.Message)"
  }

  # Raw fallback
  if (Fetch-Minimal $RawBase $AppDir) { Write-Ok "Minimal files fetched."; return }

  Write-Err "Failed to obtain repository via Git, Zip, or Raw."
  exit 1
}

# -------- Ensure project venv (.venv) --------
function Ensure-Venv {
  param([string]$appDir)
  $script:PyExe = Join-Path $appDir ".venv\Scripts\python.exe"
  if (Test-Path $PyExe) { Write-Ok "Using project venv: $($PyExe | Split-Path -Parent)"; return }
  Write-Info "Creating project venv with uv..."
  $old = Get-Location
  Set-Location $appDir
  & $UvExe venv | Out-Host
  Set-Location $old
  if (-not (Test-Path $PyExe)) { Write-Err "Failed to create .venv under $appDir"; exit 1 }
  Write-Ok "Venv created at $(Join-Path $appDir '.venv')"
}

# -------- Install deps + Playwright (inside .venv) --------
function Setup-Deps {
  $old = Get-Location
  Set-Location $AppDir

  if (Test-Path $Requirements) {
    & $UvExe pip install -r $Requirements -p $PyExe
  } else {
    Write-Warn "requirements.txt not found; installing minimal set."
    & $UvExe pip install playwright python-dotenv requests assemblyai -p $PyExe
  }

  & $UvExe run --python $PyExe python -m playwright install
  Write-Ok "Dependencies and Playwright browsers installed into .venv."

  Set-Location $old
}

# -------- Run app (inside .venv) --------
function Run-App {
  $old = Get-Location
  Set-Location $AppDir

  $entry = "app\main.py"
  if (-not (Test-Path $entry)) { Write-Err "Entry file '$entry' not found in $AppDir."; exit 1 }

  Write-Info "Launching app (headed) via project venv..."
  $env:PW_HEADLESS = "0"
  & $UvExe run --python $PyExe python $entry

  Set-Location $old
}

# ----------------- MAIN -----------------
Write-Info "Starting bootstrap for $RepoOwner/$RepoName ($Branch)"
Ensure-Uv
Ensure-Repo
Ensure-Venv -appDir $AppDir
Setup-Deps
Run-App
# ----------------------------------------
