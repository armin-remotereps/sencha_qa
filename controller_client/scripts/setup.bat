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
    echo [1/4] Creating Python virtual environment...
    %PYTHON_BIN% -m venv "%PROJECT_DIR%\.venv"
) else (
    echo [1/4] Virtual environment already exists, skipping...
)

:: Install dependencies
echo [2/4] Installing dependencies...
"%PROJECT_DIR%\.venv\Scripts\pip" install --quiet --upgrade pip
"%PROJECT_DIR%\.venv\Scripts\pip" install --quiet -r "%PROJECT_DIR%\requirements.txt"

:: Install Playwright browsers
echo [3/4] Installing Playwright browsers...
"%PROJECT_DIR%\.venv\Scripts\playwright" install --with-deps

:: Copy example.env to .env if not exists
if not exist "%PROJECT_DIR%\.env" (
    echo [4/4] Creating .env from example.env...
    copy "%PROJECT_DIR%\example.env" "%PROJECT_DIR%\.env"
    echo.
    echo IMPORTANT: Edit .env and set your CONTROLLER_API_KEY
) else (
    echo [4/4] .env already exists, skipping...
)

echo.
echo Setup complete!
echo.
echo To start the controller client:
echo   cd %ROOT_DIR%
echo   controller_client\.venv\Scripts\python -m controller_client.main

endlocal
