#!/usr/bin/env bash
set -euo pipefail

# Parse --python argument
PYTHON_BIN="python3"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --python)
            PYTHON_BIN="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--python <python-binary>]" >&2
            exit 1
            ;;
    esac
done

# Verify the Python binary exists
if ! command -v "$PYTHON_BIN" &> /dev/null; then
    echo "Error: Python binary '$PYTHON_BIN' not found." >&2
    exit 1
fi

# Check Python version >= 3.13
PYTHON_VERSION=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.major)")
PYTHON_MINOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.minor)")

if [[ "$PYTHON_MAJOR" -lt 3 ]] || { [[ "$PYTHON_MAJOR" -eq 3 ]] && [[ "$PYTHON_MINOR" -lt 13 ]]; }; then
    echo "Error: Python 3.13+ is required, but '$PYTHON_BIN' is Python $PYTHON_VERSION." >&2
    echo "Hint: specify the correct binary with --python, e.g.: $0 --python python3.13" >&2
    exit 1
fi

echo "Using Python $PYTHON_VERSION ($PYTHON_BIN)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ROOT_DIR="$(dirname "$PROJECT_DIR")"

echo "=== Controller Client Setup ==="
echo ""

# The server's cleanup_environment action empties the Downloads folder between
# test cases (CONTROLLER_CLEANUP_DIR, default ~/Downloads). The client refuses
# to delete its own files, but installing it there still means every cleanup
# skips part of the folder and logs warnings.
case "$PROJECT_DIR/" in
    "$HOME/Downloads/"*)
        echo "WARNING: the controller client is installed inside $HOME/Downloads." >&2
        echo "         Test-run cleanup empties that folder. Move the client elsewhere (e.g. $HOME/controller_client)" >&2
        echo "         or point CONTROLLER_CLEANUP_DIR in .env at a different folder." >&2
        echo ""
        ;;
esac

# Install system dependencies (Linux only). On Linux the Test Runner drives
# GNOME on Wayland through Mutter's remote-desktop D-Bus API and reads screen
# frames from PipeWire with the gst-launch-1.0 CLI. python3-tk gives the
# system Python (and so the venv) tkinter for the Test Runner window, where a
# person can answer Human in the loop steps.
LINUX_PACKAGES=(gstreamer1.0-tools gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-good python3-tk)
if [[ "$(uname -s)" == "Linux" ]]; then
    echo "[1/6] Installing system dependencies..."
    if command -v apt-get &> /dev/null; then
        MISSING_PACKAGES=()
        for package in "${LINUX_PACKAGES[@]}"; do
            if ! dpkg -s "$package" &> /dev/null; then
                MISSING_PACKAGES+=("$package")
            fi
        done
        if [[ ${#MISSING_PACKAGES[@]} -gt 0 ]]; then
            echo "  Installing ${MISSING_PACKAGES[*]} (screen capture through PipeWire, Test Runner window)..."
            sudo apt-get install -y "${MISSING_PACKAGES[@]}"
        else
            echo "  GStreamer PipeWire tools already installed."
        fi
    else
        echo "  WARNING: Non-apt system detected. Please install these packages (or your distribution's equivalents) manually:"
        echo "           ${LINUX_PACKAGES[*]}"
    fi
    if [[ "${XDG_SESSION_TYPE:-}" != "wayland" ]] || [[ "${XDG_CURRENT_DESKTOP:-}" != *GNOME* ]]; then
        echo "  WARNING: this session is '${XDG_SESSION_TYPE:-unknown}' with desktop '${XDG_CURRENT_DESKTOP:-unknown}'."
        echo "           The Test Runner on Linux needs Ubuntu's default GNOME on Wayland session."
        echo "           Log in to 'Ubuntu' (not 'Ubuntu on Xorg') and run the Test Runner from that session."
    fi
else
    echo "[1/6] System dependencies check skipped (not Linux)."
fi

# Create virtual environment
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "[2/6] Creating Python virtual environment..."
    "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
else
    echo "[2/6] Virtual environment already exists, skipping..."
fi

"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip

# Install dependencies
echo "[3/6] Installing dependencies..."
"$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

# Install Playwright browsers. The default set covers Chromium, Firefox and
# WebKit; Google Chrome and Microsoft Edge are used from their normal system
# install, so install those the usual way if test cases need them.
echo "[4/6] Installing Playwright browsers..."
"$PROJECT_DIR/.venv/bin/playwright" install --with-deps

# Copy example.env to .env if not exists
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[5/6] Creating .env from example.env..."
    cp "$PROJECT_DIR/example.env" "$PROJECT_DIR/.env"
    echo ""
    echo "IMPORTANT: Edit .env and set your CONTROLLER_API_KEY"
else
    echo "[5/6] .env already exists, skipping..."
fi

# Verify the environment isn't left in the corrupted-certifi state that has
# broken click/screenshot tooling on client machines (partial overwrite
# across the separate pip install passes above). Self-heal automatically if
# it is, since fixing it here is far cheaper than debugging it mid test run.
echo "[6/6] Verifying environment..."
if ! "$PROJECT_DIR/.venv/bin/python" -c "import certifi; certifi.where()" &> /dev/null; then
    echo "  certifi looks corrupted, reinstalling..."
    "$PROJECT_DIR/.venv/bin/pip" install --quiet --force-reinstall --no-cache-dir certifi requests urllib3
    if ! "$PROJECT_DIR/.venv/bin/python" -c "import certifi; certifi.where()" &> /dev/null; then
        echo "Error: certifi is still broken after a forced reinstall. Try deleting $PROJECT_DIR/.venv and re-running this script." >&2
        exit 1
    fi
fi

# The Test Runner window (where a person can answer Human in the loop steps
# on this machine) needs tkinter. Without it the runner still works and steps
# are answered on the web only, so this only warns.
if ! "$PROJECT_DIR/.venv/bin/python" -c "import tkinter" &> /dev/null; then
    echo "  WARNING: tkinter is missing from this Python, so the Test Runner window is off"
    echo "           (Human in the loop steps can still be answered in Punk Hazard)."
    if [[ "$(uname -s)" == "Linux" ]]; then
        echo "           Install it with: sudo apt-get install -y python3-tk (or your distribution's Tk package for Python $PYTHON_VERSION)"
    elif [[ "$("$PYTHON_BIN" -c "import sys; print(sys.prefix)")" == *Cellar* ]] || [[ "$("$PYTHON_BIN" -c "import sys; print(sys.prefix)")" == /opt/homebrew/* ]]; then
        echo "           Homebrew Python: brew install python-tk@$PYTHON_VERSION"
    else
        echo "           The python.org installer for macOS includes Tk: install Python $PYTHON_VERSION from python.org,"
        echo "           delete $PROJECT_DIR/.venv and re-run this script with --python pointing at it."
    fi
    echo "           Check it afterwards with: controller_client/.venv/bin/python -m controller_client.diagnose --window"
fi

echo ""
echo "Setup complete!"
echo ""
echo "To start the controller client:"
echo "  cd $ROOT_DIR"
echo "  controller_client/.venv/bin/python -m controller_client.main"
echo ""
echo "On its first interactive start the Test Runner asks for your Punk Hazard email"
echo "(CONTROLLER_PERSON_EMAIL in .env), so steps you answer in its window are recorded"
echo "as yours. Leave it blank to report as \"a person at the Test Runner\"."
echo ""
# Self-update (Update Test Runner on the Environment page) installs the new
# version side by side in <base>/test-runner-<version>/ and deletes the old
# copy; <base> is the parent of a test-runner-* folder an update created (it
# holds .punk-hazard-runner), else this root.
UPDATE_BASE_DIR="$ROOT_DIR"
case "$(basename "$ROOT_DIR")" in
    test-runner-[0-9]*)
        if [[ -f "$ROOT_DIR/.punk-hazard-runner" ]]; then
            UPDATE_BASE_DIR="$(dirname "$ROOT_DIR")"
        fi
        ;;
esac
echo "After an update from the panel (Update Test Runner), the new version runs from"
echo "$UPDATE_BASE_DIR/test-runner-<version>/ and this copy is deleted. Start it from then on with:"
echo "  $UPDATE_BASE_DIR/start-test-runner.sh"
echo ""
if [[ "$(uname -s)" == "Linux" ]]; then
    echo "Start the Test Runner from a terminal inside the desktop session, not over SSH:"
    echo "it needs that session's D-Bus and screen."
    echo ""
    echo "Unattended machines (e.g. Night runs): the lock screen stops remote control, so the"
    echo "machine must stay logged in and unlocked. This script does not change these settings;"
    echo "run them yourself if this machine is meant to run unattended:"
    echo "  gsettings set org.gnome.desktop.screensaver lock-enabled false"
    echo "  gsettings set org.gnome.desktop.session idle-delay 0"
    echo "  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'"
    echo "  and enable automatic login in /etc/gdm3/custom.conf (needs sudo), under [daemon]:"
    echo "    AutomaticLoginEnable=true"
    echo "    AutomaticLogin=${USER:-<your user name>}"
    echo ""
fi
echo "To check that the Test Runner can capture the screen:"
echo "  controller_client/.venv/bin/python -m controller_client.diagnose"
echo ""
echo "Chromium, Firefox and WebKit are installed. For Google Chrome or Microsoft Edge, install them normally."
echo "To check which browsers the Test Runner can launch:"
echo "  controller_client/.venv/bin/python -m controller_client.diagnose --browsers"
echo "To check the Test Runner window (Human in the loop steps):"
echo "  controller_client/.venv/bin/python -m controller_client.diagnose --window"
