@echo off
setlocal

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..
for %%I in ("%PROJECT_DIR%\..") do set ROOT_DIR=%%~fI

:: Parse optional Python binary argument (default: python)
set PYTHON_BIN=python
if not "%~1"=="" set PYTHON_BIN=%~1

:: Verify the Python binary exists
where %PYTHON_BIN% >nul 2>&1
if errorlevel 1 (
    echo Error: Python binary '%PYTHON_BIN%' not found.
    exit /b 1
)

:: Check Python version >= 3.13
for /f "delims=" %%V in ('%PYTHON_BIN% -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"') do set PYTHON_VERSION=%%V
for /f "delims=" %%M in ('%PYTHON_BIN% -c "import sys; print(sys.version_info.major)"') do set PYTHON_MAJOR=%%M
for /f "delims=" %%N in ('%PYTHON_BIN% -c "import sys; print(sys.version_info.minor)"') do set PYTHON_MINOR=%%N

if %PYTHON_MAJOR% LSS 3 (
    echo Error: Python 3.13+ is required, but '%PYTHON_BIN%' is Python %PYTHON_VERSION%.
    echo Hint: specify the correct binary, e.g.: setup.bat python3.13
    exit /b 1
)
if %PYTHON_MAJOR% EQU 3 if %PYTHON_MINOR% LSS 13 (
    echo Error: Python 3.13+ is required, but '%PYTHON_BIN%' is Python %PYTHON_VERSION%.
    echo Hint: specify the correct binary, e.g.: setup.bat python3.13
    exit /b 1
)

echo Using Python %PYTHON_VERSION% (%PYTHON_BIN%)

echo === Controller Client Setup ===
echo.

:: Create virtual environment
if not exist "%PROJECT_DIR%\.venv" (
    echo [1/8] Creating Python virtual environment...
    %PYTHON_BIN% -m venv "%PROJECT_DIR%\.venv"
) else (
    echo [1/8] Virtual environment already exists, skipping...
)

"%PROJECT_DIR%\.venv\Scripts\pip" install --quiet --upgrade pip

:: Install torch/torchvision with a platform-appropriate build. Unlike Linux,
:: PyPI's default Windows wheel is CPU-only (no bundled CUDA runtime) - the
:: CUDA-enabled build must be installed explicitly from PyTorch's own index.
echo [2/8] Installing torch (platform-appropriate build)...
where nvidia-smi >nul 2>&1
if errorlevel 1 (
    echo   No NVIDIA GPU detected, installing CPU-only torch.
    "%PROJECT_DIR%\.venv\Scripts\pip" install --quiet --index-url https://download.pytorch.org/whl/cpu torch~=2.10.0 torchvision~=0.25.0
) else (
    echo   NVIDIA GPU detected, installing CUDA-enabled torch.
    "%PROJECT_DIR%\.venv\Scripts\pip" install --quiet --index-url https://download.pytorch.org/whl/cu128 torch~=2.10.0 torchvision~=0.25.0
)

:: Install remaining dependencies
echo [3/8] Installing remaining dependencies...
"%PROJECT_DIR%\.venv\Scripts\pip" install --quiet -r "%PROJECT_DIR%\requirements.txt"

:: Install Playwright browsers
echo [4/8] Installing Playwright browsers...
"%PROJECT_DIR%\.venv\Scripts\playwright" install --with-deps

:: Download OmniParser model weights (huggingface_hub[cli] comes from
:: requirements.txt above, resolved together with transformers instead of a
:: separate late pip install that could drag it past transformers' <1.0 ceiling)
echo [5/8] Downloading OmniParser model weights (this may take a while, ~1.5GB)...
"%PROJECT_DIR%\.venv\Scripts\hf" download microsoft/OmniParser-v2.0 --local-dir "%PROJECT_DIR%\omniparser\weights"
if exist "%PROJECT_DIR%\omniparser\weights\icon_caption" if not exist "%PROJECT_DIR%\omniparser\weights\icon_caption_florence" (
    ren "%PROJECT_DIR%\omniparser\weights\icon_caption" icon_caption_florence
)

:: Pre-warm the OCR engines' own model downloads (EasyOCR/PaddleOCR construct
:: their models at import time, independent of the OmniParser weights above).
:: Without this, that download happens silently on the first real
:: find_element call, on top of the OmniParser model load, risking a timeout.
echo [6/8] Pre-warming OCR engines (downloads their own models on first use)...
"%PROJECT_DIR%\.venv\Scripts\python" -c "import sys; sys.path.insert(0, r'%PROJECT_DIR%\omniparser'); import util.utils"

:: Copy example.env to .env if not exists
if not exist "%PROJECT_DIR%\.env" (
    echo [7/8] Creating .env from example.env...
    copy "%PROJECT_DIR%\example.env" "%PROJECT_DIR%\.env"
    echo.
    echo IMPORTANT: Edit .env and set your CONTROLLER_API_KEY
) else (
    echo [7/8] .env already exists, skipping...
)

:: Verify the environment isn't left in the corrupted-certifi state that has
:: broken click/screenshot tooling on client machines (partial overwrite
:: across the separate pip install passes above). Self-heal automatically if
:: it is, since fixing it here is far cheaper than debugging it mid test run.
echo [8/8] Verifying environment...
"%PROJECT_DIR%\.venv\Scripts\python" -c "import certifi; certifi.where()" >nul 2>&1
if errorlevel 1 (
    echo   certifi looks corrupted, reinstalling...
    "%PROJECT_DIR%\.venv\Scripts\pip" install --quiet --force-reinstall --no-cache-dir certifi requests urllib3
    "%PROJECT_DIR%\.venv\Scripts\python" -c "import certifi; certifi.where()" >nul 2>&1
    if errorlevel 1 (
        echo Error: certifi is still broken after a forced reinstall. Try deleting %PROJECT_DIR%\.venv and re-running this script.
        exit /b 1
    )
)

echo.
echo Setup complete!
echo.
echo To start the controller client:
echo   cd %ROOT_DIR%
echo   controller_client\.venv\Scripts\python -m controller_client.main

endlocal
