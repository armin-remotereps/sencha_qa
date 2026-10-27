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
# frames from PipeWire with the gst-launch-1.0 CLI.
LINUX_PACKAGES=(gstreamer1.0-tools gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-good)
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
            echo "  Installing ${MISSING_PACKAGES[*]} (screen capture through PipeWire)..."
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

echo ""
echo "Setup complete!"
echo ""
echo "To start the controller client:"
echo "  cd $ROOT_DIR"
echo "  controller_client/.venv/bin/python -m controller_client.main"
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
