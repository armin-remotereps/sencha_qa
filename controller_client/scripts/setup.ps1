param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

# Verify the Python binary exists
try {
    $null = & $Python --version 2>&1
} catch {
    Write-Host "Error: Python binary '$Python' not found." -ForegroundColor Red
    exit 1
}

# Check Python version >= 3.13
$versionOutput = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$major = & $Python -c "import sys; print(sys.version_info.major)"
$minor = & $Python -c "import sys; print(sys.version_info.minor)"

if (([int]$major -lt 3) -or (([int]$major -eq 3) -and ([int]$minor -lt 13))) {
    Write-Host "Error: Python 3.13+ is required, but '$Python' is Python $versionOutput." -ForegroundColor Red
    Write-Host "Hint: specify the correct binary with -Python, e.g.: .\setup.ps1 -Python python3.13" -ForegroundColor Yellow
    exit 1
}

Write-Host "Using Python $versionOutput ($Python)"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$RootDir = Split-Path -Parent $ProjectDir

Write-Host "=== Controller Client Setup ===" -ForegroundColor Cyan
Write-Host ""

# The server's cleanup_environment action empties the Downloads folder between
# test cases (CONTROLLER_CLEANUP_DIR, default ~/Downloads). The client refuses
# to delete its own files, but installing it there still means every cleanup
# skips part of the folder and logs warnings.
$DownloadsDir = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Downloads'
if ("$ProjectDir\".ToLower().StartsWith("$DownloadsDir\".ToLower())) {
    Write-Host "WARNING: the controller client is installed inside $DownloadsDir." -ForegroundColor Yellow
    Write-Host "         Test-run cleanup empties that folder. Move the client elsewhere (e.g. $env:USERPROFILE\controller_client)" -ForegroundColor Yellow
    Write-Host "         or point CONTROLLER_CLEANUP_DIR in .env at a different folder." -ForegroundColor Yellow
    Write-Host ""
}

# Create virtual environment
if (-not (Test-Path "$ProjectDir\.venv")) {
    Write-Host "[1/5] Creating Python virtual environment..."
    & $Python -m venv "$ProjectDir\.venv"
} else {
    Write-Host "[1/5] Virtual environment already exists, skipping..."
}

& "$ProjectDir\.venv\Scripts\pip" install --quiet --upgrade pip

# Install dependencies
Write-Host "[2/5] Installing dependencies..."
& "$ProjectDir\.venv\Scripts\pip" install --quiet -r "$ProjectDir\requirements.txt"

# Install Playwright browsers. The default set covers Chromium, Firefox and
# WebKit; Google Chrome and Microsoft Edge are used from their normal system
# install (Edge ships with Windows), so install Chrome the usual way if needed.
Write-Host "[3/5] Installing Playwright browsers..."
& "$ProjectDir\.venv\Scripts\playwright" install --with-deps

# Copy example.env to .env if not exists
if (-not (Test-Path "$ProjectDir\.env")) {
    Write-Host "[4/5] Creating .env from example.env..."
    Copy-Item "$ProjectDir\example.env" "$ProjectDir\.env"
    Write-Host ""
    Write-Host "IMPORTANT: Edit .env and set your CONTROLLER_API_KEY" -ForegroundColor Yellow
} else {
    Write-Host "[4/5] .env already exists, skipping..."
}

# Verify the environment isn't left in the corrupted-certifi state that has
# broken click/screenshot tooling on client machines (partial overwrite
# across the separate pip install passes above). Self-heal automatically if
# it is, since fixing it here is far cheaper than debugging it mid test run.
Write-Host "[5/5] Verifying environment..."
& "$ProjectDir\.venv\Scripts\python" -c "import certifi; certifi.where()" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  certifi looks corrupted, reinstalling..." -ForegroundColor Yellow
    & "$ProjectDir\.venv\Scripts\pip" install --quiet --force-reinstall --no-cache-dir certifi requests urllib3
    & "$ProjectDir\.venv\Scripts\python" -c "import certifi; certifi.where()"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: certifi is still broken after a forced reinstall. Try deleting $ProjectDir\.venv and re-running this script." -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "To start the controller client:"
Write-Host "  cd $RootDir"
Write-Host "  controller_client\.venv\Scripts\python -m controller_client.main"
Write-Host ""
Write-Host "To check that the Test Runner can capture the screen:"
Write-Host "  controller_client\.venv\Scripts\python -m controller_client.diagnose"
Write-Host ""
Write-Host "Chromium, Firefox and WebKit are installed. For Google Chrome or Microsoft Edge, install them normally."
Write-Host "To check which browsers the Test Runner can launch:"
Write-Host "  controller_client\.venv\Scripts\python -m controller_client.diagnose --browsers"
