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

:: The Test Runner window (where a person can answer Human in the loop steps
:: on this machine) needs tkinter. Without it the runner still works and steps
:: are answered on the web only, so this only warns.
"%PROJECT_DIR%\.venv\Scripts\python" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo   WARNING: tkinter is missing from this Python, so the Test Runner window is off
    echo            ^(Human in the loop steps can still be answered in Punk Hazard^).
    echo            The python.org installer includes Tk: run it again, choose Modify and tick
    echo            "tcl/tk and IDLE". Then check it with:
    echo            controller_client\.venv\Scripts\python -m controller_client.diagnose --window
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
echo On its first interactive start the Test Runner asks for your Punk Hazard email
echo ^(CONTROLLER_PERSON_EMAIL in .env^), so steps you answer in its window are recorded
echo as yours. Leave it blank to report as "a person at the Test Runner".
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
echo To check the Test Runner window ^(Human in the loop steps^):
echo   controller_client\.venv\Scripts\python -m controller_client.diagnose --window

:: Phone and tablet target (CONTROLLER_TARGET=android in .env): the Test
:: Runner drives the device through Appium. These are OS-level tools this
:: script never installs; it only lists them.
set "RUNNER_TARGET=%CONTROLLER_TARGET%"
if not defined RUNNER_TARGET if exist "%PROJECT_DIR%\.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%PROJECT_DIR%\.env") do (
        if /I "%%A"=="CONTROLLER_TARGET" set "RUNNER_TARGET=%%B"
    )
)
if /I "%RUNNER_TARGET%"=="android" (
    echo.
    echo This Test Runner drives an Android phone or tablet ^(CONTROLLER_TARGET=android^). Install yourself:
    echo   - Node.js 22.22.2 or newer ^(or 24.15+^): https://nodejs.org
    echo   - Appium 3: npm i -g appium@3
    echo   - its Android driver: appium driver install uiautomator2
    echo   - Android platform-tools ^(adb^) on PATH, and ANDROID_HOME set
    echo   - on the phone: Developer options ^> USB debugging on, then accept this computer's key
    echo Then pick the device ^(CONTROLLER_DEVICE; the first interactive start asks^) and check it with:
    echo   controller_client\.venv\Scripts\python -m controller_client.diagnose --mobile
)
if /I "%RUNNER_TARGET%"=="ios" (
    echo.
    echo CONTROLLER_TARGET=ios needs a Mac with Xcode: iPhones, iPads and the iOS Simulator cannot be driven from Windows.
)

endlocal
