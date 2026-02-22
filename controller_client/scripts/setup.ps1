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

# Create virtual environment
if (-not (Test-Path "$ProjectDir\.venv")) {
    Write-Host "[1/4] Creating Python virtual environment..."
    & $Python -m venv "$ProjectDir\.venv"
} else {
    Write-Host "[1/4] Virtual environment already exists, skipping..."
}

# Install dependencies
Write-Host "[2/4] Installing dependencies..."
& "$ProjectDir\.venv\Scripts\pip" install --quiet --upgrade pip
& "$ProjectDir\.venv\Scripts\pip" install --quiet -r "$ProjectDir\requirements.txt"

# Install Playwright browsers
Write-Host "[3/4] Installing Playwright browsers..."
& "$ProjectDir\.venv\Scripts\playwright" install --with-deps

# Copy example.env to .env if not exists
if (-not (Test-Path "$ProjectDir\.env")) {
    Write-Host "[4/4] Creating .env from example.env..."
    Copy-Item "$ProjectDir\example.env" "$ProjectDir\.env"
    Write-Host ""
    Write-Host "IMPORTANT: Edit .env and set your CONTROLLER_API_KEY" -ForegroundColor Yellow
} else {
    Write-Host "[4/4] .env already exists, skipping..."
}

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "To start the controller client:"
Write-Host "  cd $RootDir"
Write-Host "  controller_client\.venv\Scripts\python -m controller_client.main"
