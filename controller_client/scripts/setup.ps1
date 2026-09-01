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
    Write-Host "[1/8] Creating Python virtual environment..."
    & $Python -m venv "$ProjectDir\.venv"
} else {
    Write-Host "[1/8] Virtual environment already exists, skipping..."
}

& "$ProjectDir\.venv\Scripts\pip" install --quiet --upgrade pip

# Install torch/torchvision with a platform-appropriate build: CUDA wheel if
# an NVIDIA GPU is detected, CPU-only wheel otherwise. Unlike Linux, PyPI's
# default Windows wheel is CPU-only (no bundled CUDA runtime) — the
# CUDA-enabled build must be installed explicitly from PyTorch's own index.
Write-Host "[2/8] Installing torch (platform-appropriate build)..."
$hasNvidiaGpu = $null -ne (Get-Command "nvidia-smi" -ErrorAction SilentlyContinue)
if ($hasNvidiaGpu) {
    Write-Host "  NVIDIA GPU detected, installing CUDA-enabled torch."
    & "$ProjectDir\.venv\Scripts\pip" install --quiet --index-url https://download.pytorch.org/whl/cu128 "torch~=2.10.0" "torchvision~=0.25.0"
} else {
    Write-Host "  No NVIDIA GPU detected, installing CPU-only torch."
    & "$ProjectDir\.venv\Scripts\pip" install --quiet --index-url https://download.pytorch.org/whl/cpu "torch~=2.10.0" "torchvision~=0.25.0"
}

# Install remaining dependencies
Write-Host "[3/8] Installing remaining dependencies..."
& "$ProjectDir\.venv\Scripts\pip" install --quiet -r "$ProjectDir\requirements.txt"

# Install Playwright browsers
Write-Host "[4/8] Installing Playwright browsers..."
& "$ProjectDir\.venv\Scripts\playwright" install --with-deps

# Download OmniParser model weights (huggingface_hub[cli] comes from
# requirements.txt above, resolved together with transformers instead of a
# separate late pip install that could drag it past transformers' <1.0 ceiling)
Write-Host "[5/8] Downloading OmniParser model weights (this may take a while, ~1.5GB)..."
& "$ProjectDir\.venv\Scripts\hf" download microsoft/OmniParser-v2.0 --local-dir "$ProjectDir\omniparser\weights"
if ((Test-Path "$ProjectDir\omniparser\weights\icon_caption") -and (-not (Test-Path "$ProjectDir\omniparser\weights\icon_caption_florence"))) {
    Rename-Item "$ProjectDir\omniparser\weights\icon_caption" "icon_caption_florence"
}

# Pre-warm the OCR engines' own model downloads (EasyOCR/PaddleOCR construct
# their models at import time, independent of the OmniParser weights above).
# Without this, that download happens silently on the first real
# find_element call, on top of the OmniParser model load, risking a timeout.
Write-Host "[6/8] Pre-warming OCR engines (downloads their own models on first use)..."
& "$ProjectDir\.venv\Scripts\python" -c "import sys; sys.path.insert(0, r'$ProjectDir\omniparser'); import util.utils"

# Copy example.env to .env if not exists
if (-not (Test-Path "$ProjectDir\.env")) {
    Write-Host "[7/8] Creating .env from example.env..."
    Copy-Item "$ProjectDir\example.env" "$ProjectDir\.env"
    Write-Host ""
    Write-Host "IMPORTANT: Edit .env and set your CONTROLLER_API_KEY" -ForegroundColor Yellow
} else {
    Write-Host "[7/8] .env already exists, skipping..."
}

# Verify the environment isn't left in the corrupted-certifi state that has
# broken click/screenshot tooling on client machines (partial overwrite
# across the separate pip install passes above). Self-heal automatically if
# it is, since fixing it here is far cheaper than debugging it mid test run.
Write-Host "[8/8] Verifying environment..."
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
Write-Host "To check the OmniParser setup end to end (dependencies, weights, screenshot, model load, one inference):"
Write-Host "  controller_client\.venv\Scripts\python -m controller_client.diagnose"
