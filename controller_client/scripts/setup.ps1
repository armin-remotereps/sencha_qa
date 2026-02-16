$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$RootDir = Split-Path -Parent $ProjectDir

Write-Host "=== Controller Client Setup ===" -ForegroundColor Cyan
Write-Host ""

# Create virtual environment
if (-not (Test-Path "$ProjectDir\.venv")) {
    Write-Host "[1/4] Creating Python virtual environment..."
    python -m venv "$ProjectDir\.venv"
} else {
    Write-Host "[1/4] Virtual environment already exists, skipping..."
}

# Install dependencies
Write-Host "[2/4] Installing dependencies..."
& "$ProjectDir\.venv\Scripts\pip" install --quiet --upgrade pip
& "$ProjectDir\.venv\Scripts\pip" install --quiet -r "$ProjectDir\requirements.txt"

# Install Playwright browsers
Write-Host "[3/4] Installing Playwright browsers..."
& "$ProjectDir\.venv\Scripts\playwright" install

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
