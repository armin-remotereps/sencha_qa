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

:: The server's cleanup_environment action empties the Downloads folder between
:: test cases (CONTROLLER_CLEANUP_DIR, default ~/Downloads). The client refuses
:: to delete its own files, but installing it there still means every cleanup
:: skips part of the folder and logs warnings.
for %%I in ("%PROJECT_DIR%") do set PROJECT_DIR_FULL=%%~fI
set "CLEANUP_DIR_PREFIX=%USERPROFILE%\Downloads\"
set "PROJECT_DIR_CHECK=%PROJECT_DIR_FULL%\"
call set "PROJECT_DIR_OUTSIDE=%%PROJECT_DIR_CHECK:%CLEANUP_DIR_PREFIX%=%%"
if /I not "%PROJECT_DIR_OUTSIDE%"=="%PROJECT_DIR_CHECK%" (
    echo WARNING: the controller client is installed inside %USERPROFILE%\Downloads.
    echo          Test-run cleanup empties that folder. Move the client elsewhere ^(e.g. %USERPROFILE%\controller_client^)
    echo          or point CONTROLLER_CLEANUP_DIR in .env at a different folder.
    echo.
)

:: Create virtual environment
if not exist "%PROJECT_DIR%\.venv" (
    echo [1/5] Creating Python virtual environment...
    %PYTHON_BIN% -m venv "%PROJECT_DIR%\.venv"
) else (
    echo [1/5] Virtual environment already exists, skipping...
)

"%PROJECT_DIR%\.venv\Scripts\pip" install --quiet --upgrade pip

:: Install dependencies
echo [2/5] Installing dependencies...
"%PROJECT_DIR%\.venv\Scripts\pip" install --quiet -r "%PROJECT_DIR%\requirements.txt"

:: Install Playwright browsers. The default set covers Chromium, Firefox and
:: WebKit; Google Chrome and Microsoft Edge are used from their normal system
:: install (Edge ships with Windows), so install Chrome the usual way if needed.
echo [3/5] Installing Playwright browsers...
"%PROJECT_DIR%\.venv\Scripts\playwright" install --with-deps

:: Copy example.env to .env if not exists
if not exist "%PROJECT_DIR%\.env" (
    echo [4/5] Creating .env from example.env...
    copy "%PROJECT_DIR%\example.env" "%PROJECT_DIR%\.env"
    echo.
    echo IMPORTANT: Edit .env and set your CONTROLLER_API_KEY
) else (
    echo [4/5] .env already exists, skipping...
)

:: Verify the environment isn't left in the corrupted-certifi state that has
:: broken click/screenshot tooling on client machines (partial overwrite
:: across the separate pip install passes above). Self-heal automatically if
:: it is, since fixing it here is far cheaper than debugging it mid test run.
echo [5/5] Verifying environment...
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
:: Self-update (Update Test Runner on the Environment page) installs the new
:: version side by side in <base>\test-runner-<version>\ and deletes the old
:: copy; <base> is the parent of a test-runner-* folder an update created
:: (it holds .punk-hazard-runner), else this root.
for %%I in ("%ROOT_DIR%") do set "ROOT_NAME=%%~nxI"
for %%I in ("%ROOT_DIR%\..") do set "ROOT_PARENT=%%~fI"
set "UPDATE_BASE_DIR=%ROOT_DIR%"
if /I "%ROOT_NAME:~0,12%"=="test-runner-" if exist "%ROOT_DIR%\.punk-hazard-runner" set "UPDATE_BASE_DIR=%ROOT_PARENT%"
:: Paths are printed with delayed expansion, which inserts them after the
:: line is parsed, so characters such as & or ) in a folder name are printed
:: instead of being run. Enabled only here: the sets above would mangle a !.
setlocal EnableDelayedExpansion
echo To start the controller client:
echo   cd /d "!ROOT_DIR!"
echo   controller_client\.venv\Scripts\python -m controller_client.main
echo.
echo After an update from the panel (Update Test Runner), the new version runs from
echo   !UPDATE_BASE_DIR!\test-runner-^<version^>\
echo and this copy is deleted. Start it from then on with:
echo   "!UPDATE_BASE_DIR!\start-test-runner.cmd"
endlocal
echo.
echo To check that the Test Runner can capture the screen:
echo   controller_client\.venv\Scripts\python -m controller_client.diagnose
echo.
echo Chromium, Firefox and WebKit are installed. For Google Chrome or Microsoft Edge, install them normally.
echo To check which browsers the Test Runner can launch:
echo   controller_client\.venv\Scripts\python -m controller_client.diagnose --browsers

endlocal
