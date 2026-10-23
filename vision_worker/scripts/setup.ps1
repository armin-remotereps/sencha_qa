# Set up a vision worker in vision_worker\.venv on Windows (native, no Docker).
# Usage: .\vision_worker\scripts\setup.ps1 [-Python <python-binary>]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

try {
    $null = & $Python --version 2>&1
} catch {
    Write-Host "Error: Python binary '$Python' not found." -ForegroundColor Red
    exit 1
}

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
$VenvPy = "$ProjectDir\.venv\Scripts\python"
$VenvPip = "$ProjectDir\.venv\Scripts\pip"
$WeightsDir = "$ProjectDir\omniparser\weights"

Write-Host "=== Vision Worker Setup ===" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path "$ProjectDir\.venv")) {
    Write-Host "[1/6] Creating Python virtual environment..."
    & $Python -m venv "$ProjectDir\.venv"
} else {
    Write-Host "[1/6] Virtual environment already exists, skipping..."
}
& $VenvPy -m pip install --quiet --upgrade pip

# PyPI's default Windows torch wheel is CPU-only; the CUDA build comes from
# PyTorch's own index. The CUDA 12.8 wheels bundle the CUDA runtime, so only
# the NVIDIA driver is needed.
Write-Host "[2/6] Installing torch..."
$hasNvidiaGpu = $null -ne (Get-Command "nvidia-smi" -ErrorAction SilentlyContinue)
if ($hasNvidiaGpu) {
    Write-Host "  NVIDIA GPU detected, installing the CUDA 12.8 build."
    & $VenvPip install --quiet --index-url https://download.pytorch.org/whl/cu128 "torch~=2.10.0" "torchvision~=0.25.0"
} else {
    Write-Host "  WARNING: no NVIDIA GPU detected (nvidia-smi not found); installing the CPU-only build." -ForegroundColor Yellow
    Write-Host "  A CPU worker works but parses each screenshot far more slowly." -ForegroundColor Yellow
    & $VenvPip install --quiet --index-url https://download.pytorch.org/whl/cpu "torch~=2.10.0" "torchvision~=0.25.0"
}

Write-Host "[3/6] Installing remaining dependencies..."
& $VenvPip install --quiet -r "$ProjectDir\requirements.txt"

Write-Host "[4/6] Downloading OmniParser model weights (this may take a while, ~1.5GB)..."
& "$ProjectDir\.venv\Scripts\hf" download microsoft/OmniParser-v2.0 --local-dir $WeightsDir
if ((Test-Path "$WeightsDir\icon_caption") -and (-not (Test-Path "$WeightsDir\icon_caption_florence"))) {
    Rename-Item "$WeightsDir\icon_caption" "icon_caption_florence"
    Write-Host "  Renamed icon_caption -> icon_caption_florence"
}

# EasyOCR/PaddleOCR download their own models when util.utils is imported,
# and the Florence-2 processor comes from the Hugging Face Hub on first load.
# Fetch them now so the first model load does not stall on downloads.
Write-Host "[5/6] Pre-warming OCR engines and the caption processor..."
& $VenvPy -c "import sys; sys.path.insert(0, r'$ProjectDir\omniparser'); import util.utils; from transformers import AutoProcessor; AutoProcessor.from_pretrained('microsoft/Florence-2-base', trust_remote_code=True)"

if (-not (Test-Path "$ProjectDir\.env")) {
    Write-Host "[6/6] Creating .env from example.env..."
    Copy-Item "$ProjectDir\example.env" "$ProjectDir\.env"
    Write-Host ""
    Write-Host "IMPORTANT: edit vision_worker\.env and set VISION_SERVER_URL and VISION_WORKER_TOKEN" -ForegroundColor Yellow
} else {
    Write-Host "[6/6] .env already exists, skipping..."
}

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "To check the install (dependencies, weights, device, model load, one inference):"
Write-Host "  cd $RootDir"
Write-Host "  vision_worker\.venv\Scripts\python -m vision_worker.diagnose --check-server"
Write-Host ""
Write-Host "To start the vision worker:"
Write-Host "  vision_worker\.venv\Scripts\python -m vision_worker"
