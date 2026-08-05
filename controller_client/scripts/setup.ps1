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
    Write-Host "[1/6] Creating Python virtual environment..."
    & $Python -m venv "$ProjectDir\.venv"
} else {
    Write-Host "[1/6] Virtual environment already exists, skipping..."
}

& "$ProjectDir\.venv\Scripts\pip" install --quiet --upgrade pip

# Install torch/torchvision with a platform-appropriate build: CUDA wheel if
# an NVIDIA GPU is detected, CPU-only wheel otherwise.
Write-Host "[2/6] Installing torch (platform-appropriate build)..."
$hasNvidiaGpu = $null -ne (Get-Command "nvidia-smi" -ErrorAction SilentlyContinue)
if ($hasNvidiaGpu) {
    Write-Host "  NVIDIA GPU detected, installing CUDA-enabled torch."
    & "$ProjectDir\.venv\Scripts\pip" install --quiet "torch~=2.10.0" "torchvision~=0.25.0"
} else {
    Write-Host "  No NVIDIA GPU detected, installing CPU-only torch."
    & "$ProjectDir\.venv\Scripts\pip" install --quiet --index-url https://download.pytorch.org/whl/cpu "torch~=2.10.0" "torchvision~=0.25.0"
}

# Install remaining dependencies
Write-Host "[3/6] Installing remaining dependencies..."
& "$ProjectDir\.venv\Scripts\pip" install --quiet -r "$ProjectDir\requirements.txt"

# Install Playwright browsers
Write-Host "[4/6] Installing Playwright browsers..."
& "$ProjectDir\.venv\Scripts\playwright" install --with-deps

# Download OmniParser model weights
Write-Host "[5/6] Downloading OmniParser model weights (this may take a while, ~1.5GB)..."
& "$ProjectDir\.venv\Scripts\pip" install --quiet "huggingface_hub[cli]"
& "$ProjectDir\.venv\Scripts\hf" download microsoft/OmniParser-v2.0 --local-dir "$ProjectDir\omniparser\weights"
if ((Test-Path "$ProjectDir\omniparser\weights\icon_caption") -and (-not (Test-Path "$ProjectDir\omniparser\weights\icon_caption_florence"))) {
    Rename-Item "$ProjectDir\omniparser\weights\icon_caption" "icon_caption_florence"
}

# Copy example.env to .env if not exists
if (-not (Test-Path "$ProjectDir\.env")) {
    Write-Host "[6/6] Creating .env from example.env..."
    Copy-Item "$ProjectDir\example.env" "$ProjectDir\.env"
    Write-Host ""
    Write-Host "IMPORTANT: Edit .env and set your CONTROLLER_API_KEY" -ForegroundColor Yellow
} else {
    Write-Host "[6/6] .env already exists, skipping..."
}

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "To start the controller client:"
Write-Host "  cd $RootDir"
Write-Host "  controller_client\.venv\Scripts\python -m controller_client.main"
