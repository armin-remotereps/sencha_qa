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
# Window capture (server capture requests) outside GNOME (Wayland or Xorg): on X11 it reads
# windows through libX11 and the Composite extension; on Sway and Hyprland it
# lists windows with swaymsg / hyprctl (part of the compositor) and grabs
# with grim. GNOME (Wayland and Xorg) uses the bundled capture extension (installed below).
LINUX_X11_CAPTURE_PACKAGES=(libx11-6 libxcomposite1)
LINUX_WLROOTS_CAPTURE_PACKAGES=(grim)
IS_WLROOTS_SESSION=""
if [[ -n "${SWAYSOCK:-}" ]] || [[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then
    IS_WLROOTS_SESSION="yes"
fi
if [[ "$(uname -s)" == "Linux" ]]; then
    echo "[1/6] Installing system dependencies..."
    if [[ "${XDG_SESSION_TYPE:-}" == "x11" ]]; then
        LINUX_PACKAGES+=("${LINUX_X11_CAPTURE_PACKAGES[@]}")
    fi
    if [[ -n "$IS_WLROOTS_SESSION" ]]; then
        LINUX_PACKAGES+=("${LINUX_WLROOTS_CAPTURE_PACKAGES[@]}")
    fi
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
        echo "           (Manual runs and window capture also work on X11, Sway and Hyprland.)"
    fi
    echo "  Window capture needs, by session type:"
    echo "    X11: ${LINUX_X11_CAPTURE_PACKAGES[*]} (usually present)"
    echo "    Sway or Hyprland: ${LINUX_WLROOTS_CAPTURE_PACKAGES[*]}, plus swaymsg (sway) or hyprctl (hyprland) on PATH"
    echo "    GNOME (Wayland or Xorg): the gnome-extensions tool (package gnome-shell) and the bundled capture extension"
    if [[ -n "${SWAYSOCK:-}" ]] && ! command -v swaymsg &> /dev/null; then
        echo "  WARNING: swaymsg is not on PATH; window capture on Sway needs it (it ships with sway)."
    fi
    if [[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]] && ! command -v hyprctl &> /dev/null; then
        echo "  WARNING: hyprctl is not on PATH; window capture on Hyprland needs it (it ships with hyprland)."
    fi
    if [[ -n "$IS_WLROOTS_SESSION" ]] && ! command -v grim &> /dev/null; then
        echo "  WARNING: grim is not on PATH; window capture on Sway and Hyprland needs it."
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

# Window capture on GNOME (Wayland and Xorg): GNOME keeps window lists and
# screenshots for its own callers on Wayland, and draws its notifications and
# dialogs inside the Shell on both, so a small bundled Shell extension does the
# capture for the Test Runner. Installed on any GNOME session. GNOME loads a new
# or updated extension only at the next login. The self-update re-installs it.
GNOME_HELPER_UUID="test-runner-capture@punk-hazard"
if [[ "$(uname -s)" == "Linux" ]] && [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]]; then
    echo "Installing the Test Runner capture extension for GNOME..."
    if command -v gnome-extensions &> /dev/null; then
        PACK_DIR="$(mktemp -d)"
        if gnome-extensions pack "$PROJECT_DIR/gnome_capture_helper" --force --out-dir="$PACK_DIR" > /dev/null \
            && gnome-extensions install --force "$PACK_DIR/$GNOME_HELPER_UUID.shell-extension.zip"; then
            if ! gnome-extensions enable "$GNOME_HELPER_UUID" &> /dev/null; then
                # GNOME on Wayland knows a new extension only after the next
                # login; mark it enabled for then.
                if ! "$PYTHON_BIN" - "$GNOME_HELPER_UUID" <<'PY'
import ast
import subprocess
import sys

uuid = sys.argv[1]
key = ["org.gnome.shell", "enabled-extensions"]
raw = subprocess.run(
    ["gsettings", "get", *key], capture_output=True, text=True, check=True
).stdout.strip()
if raw.startswith("@as"):
    raw = raw[3:].strip()
items = [str(item) for item in ast.literal_eval(raw)]
if uuid not in items:
    items.append(uuid)
    subprocess.run(["gsettings", "set", *key, str(items)], check=True)
PY
                then
                    echo "  WARNING: could not enable it; run after logging in again: gnome-extensions enable $GNOME_HELPER_UUID"
                fi
            fi
            if [[ "$(gsettings get org.gnome.shell disable-user-extensions 2> /dev/null || true)" == "true" ]]; then
                echo "  WARNING: GNOME user extensions are turned off; turn them on in the Extensions app"
                echo "           (or: gsettings set org.gnome.shell disable-user-extensions false)."
            fi
            echo "  Installed. Log out and back in once so GNOME loads it."
        else
            echo "  WARNING: the capture extension could not be installed; window capture will report it missing."
        fi
        rm -rf "$PACK_DIR"
    else
        echo "  WARNING: gnome-extensions is not on PATH (package gnome-shell); window capture will report the extension missing."
    fi
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

# Phone and tablet target (CONTROLLER_TARGET=android or ios in .env): the
# Test Runner drives the device through Appium. These are OS-level tools this
# script never installs; it only lists them.
RUNNER_TARGET="${CONTROLLER_TARGET:-}"
if [[ -z "$RUNNER_TARGET" ]] && [[ -f "$PROJECT_DIR/.env" ]]; then
    RUNNER_TARGET=$(grep -E '^[[:space:]]*CONTROLLER_TARGET=' "$PROJECT_DIR/.env" | tail -n 1 | cut -d= -f2- | tr -d '[:space:]"'"'" || true)
fi
RUNNER_TARGET=$(echo "$RUNNER_TARGET" | tr '[:upper:]' '[:lower:]')
if [[ "$RUNNER_TARGET" == "android" || "$RUNNER_TARGET" == "ios" ]]; then
    echo "This Test Runner drives a phone or tablet (CONTROLLER_TARGET=$RUNNER_TARGET). Install yourself:"
    echo "  - Node.js 22.22.2 or newer (or 24.15+): https://nodejs.org"
    echo "  - Appium 3: npm i -g appium@3"
    if [[ "$RUNNER_TARGET" == "android" ]]; then
        echo "  - its Android driver: appium driver install uiautomator2"
        echo "  - Android platform-tools (adb) on PATH, and ANDROID_HOME set (Android Studio or the SDK command-line tools)"
        echo "  - on the phone: Developer options > USB debugging on, then accept this computer's key"
    else
        echo "  - its iOS driver: appium driver install xcuitest"
        echo "  - Xcode with its command-line tools (xcode-select --install), on this Mac"
        echo "  - iPhone/iPad: trust this Mac, turn on Developer Mode, and set CONTROLLER_IOS_TEAM_ID in .env"
    fi
    echo "Then pick the device (CONTROLLER_DEVICE; the first interactive start asks) and check it with:"
    echo "  controller_client/.venv/bin/python -m controller_client.diagnose --mobile"
fi
