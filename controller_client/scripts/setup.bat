@echo off
setlocal

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..
for %%I in ("%PROJECT_DIR%\..") do set ROOT_DIR=%%~fI

echo === Controller Client Setup ===
echo.

:: Create virtual environment
if not exist "%PROJECT_DIR%\.venv" (
    echo [1/4] Creating Python virtual environment...
    python -m venv "%PROJECT_DIR%\.venv"
) else (
    echo [1/4] Virtual environment already exists, skipping...
)

:: Install dependencies
echo [2/4] Installing dependencies...
"%PROJECT_DIR%\.venv\Scripts\pip" install --quiet --upgrade pip
"%PROJECT_DIR%\.venv\Scripts\pip" install --quiet -r "%PROJECT_DIR%\requirements.txt"

:: Install Playwright browsers
echo [3/4] Installing Playwright browsers...
"%PROJECT_DIR%\.venv\Scripts\playwright" install

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
