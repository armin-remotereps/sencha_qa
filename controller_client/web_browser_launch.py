"""The Test Runner's desktop browser launcher (WEB_BROWSER_V1).

Starts Chrome, Edge or Firefox detached, with the browser's dedicated profile
``<runner root>/web_profiles/<browser>/`` and remote control on 127.0.0.1, so
the runner can attach to it whoever started it:

- Chrome / Edge: ``--user-data-dir``, ``--remote-debugging-port=0`` (the
  browser writes the port it picked to ``DevToolsActivePort``);
- Firefox: ``-profile <dir> -marionette -no-remote`` with ``marionette.port``
  0 in the profile's ``user.js`` (port in ``MarionetteActivePort``).

Used by the runner's open action and by a person's shortcut:
``python -m controller_client.web_browser_launch <chrome|edge|firefox> [url]``,
which works whether the runner is running or not.
"""

import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import psutil

from controller_client.config import load_mobile_settings
from controller_client.exceptions import (
    BrowserNotInstalledError,
    ExecutionError,
    ProtocolError,
    WebBrowserUnsupportedError,
)
from controller_client.protocol import WebBrowser, validate_web_url
from controller_client.update_layout import (
    RUNNING_PACKAGE_DIR,
    WEB_PROFILES_DIR_NAME,
    base_for_install_root,
    write_open_browser_script,
)
from controller_client.web_browser_presence import (
    MARIONETTE_PORT_FILE,
    firefox_open_port,
)

logger = logging.getLogger(__name__)

_USER_JS: Final[str] = "user.js"
_MARIONETTE_PREF: Final[str] = 'user_pref("marionette.port", 0);'
_FIREFOX_PREFS: Final[tuple[str, ...]] = (
    _MARIONETTE_PREF,
    'user_pref("browser.shell.checkDefaultBrowser", false);',
)
_VERSION_TIMEOUT_SECONDS: Final[float] = 5.0
_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+){1,3}")
_LAUNCHABLE: Final[frozenset[WebBrowser]] = frozenset(
    {WebBrowser.CHROME, WebBrowser.EDGE, WebBrowser.FIREFOX}
)
_CHROMIUM: Final[frozenset[WebBrowser]] = frozenset(
    {WebBrowser.CHROME, WebBrowser.EDGE}
)

# Processes this process started; polled so they never linger as zombies.
_STARTED: Final[list[subprocess.Popen[bytes]]] = []

_PATH_NAMES: Final[dict[WebBrowser, tuple[str, ...]]] = {
    WebBrowser.CHROME: (
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "chromium",
        "chromium-browser",
    ),
    WebBrowser.EDGE: ("microsoft-edge", "microsoft-edge-stable", "msedge"),
    WebBrowser.FIREFOX: ("firefox",),
}


def default_runner_root() -> Path:
    """The Test Runner folder of this install (the ``controller_client``
    package folder, next to its ``.env``)."""
    return RUNNING_PACKAGE_DIR


def profile_dir(runner_root: Path, browser: WebBrowser) -> Path:
    return runner_root / WEB_PROFILES_DIR_NAME / browser.value


def _windows_candidates(browser: WebBrowser) -> list[Path]:
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    relative = {
        WebBrowser.CHROME: Path("Google/Chrome/Application/chrome.exe"),
        WebBrowser.EDGE: Path("Microsoft/Edge/Application/msedge.exe"),
        WebBrowser.FIREFOX: Path("Mozilla Firefox/firefox.exe"),
    }[browser]
    return [Path(root) / relative for root in roots if root]


def _macos_candidates(browser: WebBrowser) -> list[Path]:
    app = {
        WebBrowser.CHROME: "Google Chrome.app/Contents/MacOS/Google Chrome",
        WebBrowser.EDGE: "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        WebBrowser.FIREFOX: "Firefox.app/Contents/MacOS/firefox",
    }[browser]
    return [Path("/Applications") / app, Path.home() / "Applications" / app]


def _linux_candidates(browser: WebBrowser) -> list[Path]:
    return {
        WebBrowser.CHROME: [Path("/opt/google/chrome/chrome")],
        WebBrowser.EDGE: [Path("/opt/microsoft/msedge/msedge")],
        WebBrowser.FIREFOX: [Path("/usr/lib/firefox/firefox")],
    }[browser]


def find_executable(browser: WebBrowser) -> Path | None:
    """The browser's executable: the standard install paths of this OS, then
    PATH. None when it is not installed (and always for Safari, which is not
    launched this way)."""
    if browser not in _LAUNCHABLE:
        return None
    if sys.platform == "win32":
        candidates = _windows_candidates(browser)
    elif sys.platform == "darwin":
        candidates = _macos_candidates(browser)
    else:
        candidates = _linux_candidates(browser)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for name in _PATH_NAMES[browser]:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def installed_version(browser: WebBrowser, executable: Path) -> str:
    """The installed version, read without starting a window; "" when
    unknown."""
    try:
        if sys.platform == "darwin":
            info = executable.parent.parent / "Info.plist"
            with info.open("rb") as handle:
                plist = plistlib.load(handle)
            value = plist.get("CFBundleShortVersionString", "")
            return value if isinstance(value, str) else ""
        if browser is WebBrowser.FIREFOX:
            ini = executable.resolve().parent / "application.ini"
            for line in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("Version="):
                    return line.removeprefix("Version=").strip()
            return ""
        if sys.platform == "win32":
            # Chrome and Edge install into Application\<version>\.
            versions = sorted(
                (
                    child.name
                    for child in executable.parent.iterdir()
                    if child.is_dir() and _VERSION_RE.fullmatch(child.name)
                ),
                key=lambda name: tuple(int(part) for part in name.split(".")),
            )
            return versions[-1] if versions else ""
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
        match = _VERSION_RE.search(completed.stdout.decode("utf-8", errors="ignore"))
        return match.group(0) if match else ""
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        plistlib.InvalidFileException,
    ):
        logger.debug("Could not read the %s version", browser.value, exc_info=True)
        return ""


def _prepare_firefox_profile(profile: Path) -> None:
    """``marionette.port`` 0 in ``user.js`` (other lines kept) and no stale
    MarionetteActivePort."""
    user_js = profile / _USER_JS
    try:
        existing = user_js.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        existing = []
    kept = [
        line
        for line in existing
        if not any(pref.split(",")[0] in line for pref in _FIREFOX_PREFS)
    ]
    user_js.write_text("\n".join([*kept, *_FIREFOX_PREFS]) + "\n", encoding="utf-8")
    (profile / MARIONETTE_PORT_FILE).unlink(missing_ok=True)


def _command(
    browser: WebBrowser, executable: Path, profile: Path, url: str | None
) -> list[str]:
    if browser in _CHROMIUM:
        command = [
            str(executable),
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    else:
        command = [
            str(executable),
            "-profile",
            str(profile),
            "-marionette",
            "-no-remote",
        ]
    if url is not None:
        command.append(url)
    return command


def _reap_started() -> None:
    """Collect started processes that already exited (a Chromium launch
    hands its window to a running instance and exits at once)."""
    _STARTED[:] = [process for process in _STARTED if process.poll() is None]


def _spawn_detached(command: list[str]) -> int:
    _reap_started()
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
    else:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    _STARTED.append(process)
    return process.pid


def launch_browser(
    browser: WebBrowser, runner_root: Path, url: str | None
) -> int | None:
    """Start ``browser`` detached with its dedicated profile; returns the
    started process id, or None when Firefox's profile is already open
    (``-no-remote`` cannot hand over). A Chromium profile already open
    receives the new window from the started process."""
    if browser not in _LAUNCHABLE:
        raise WebBrowserUnsupportedError("This browser is not started this way")
    executable = find_executable(browser)
    if executable is None:
        raise BrowserNotInstalledError("Browser not installed")
    profile = profile_dir(runner_root, browser)
    try:
        profile.mkdir(parents=True, exist_ok=True)
        if browser is WebBrowser.FIREFOX:
            if firefox_open_port(profile) is not None:
                return None
            _prepare_firefox_profile(profile)
        return _spawn_detached(_command(browser, executable, profile, url))
    except OSError as e:
        raise ExecutionError("Could not start the browser") from e


def _uses_profile(cmdline: list[str], profile: Path) -> bool:
    text = str(profile)
    if f"--user-data-dir={text}" in cmdline:
        return True
    for index, arg in enumerate(cmdline[:-1]):
        if arg in ("-profile", "--profile") and cmdline[index + 1] == text:
            return True
    return False


def find_profile_processes(profile: Path) -> list[psutil.Process]:
    """Processes of this machine started with ``profile`` (the main browser
    process and its helpers that carry the flag)."""
    found: list[psutil.Process] = []
    for process in psutil.process_iter(["cmdline"]):
        cmdline = process.info.get("cmdline")
        if isinstance(cmdline, list) and _uses_profile(cmdline, profile):
            found.append(process)
    return found


WRITE_SHORTCUT_FLAG: Final[str] = "--write-shortcut"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python -m controller_client.web_browser_launch <chrome|edge|firefox> [url]"
    if args == [WRITE_SHORTCUT_FLAG]:
        # Setup: write the test browser shortcut next to the start script,
        # on a desktop target only (a phone's browsers are opened on it).
        mobile, _problem = load_mobile_settings()
        if mobile is not None:
            return 0
        install_root = RUNNING_PACKAGE_DIR.parent
        script = write_open_browser_script(
            base_for_install_root(install_root), install_root
        )
        print(f"Open a test browser with: {script} <chrome|edge|firefox> [url]")
        return 0
    if not 1 <= len(args) <= 2:
        print(usage, file=sys.stderr)
        return 2
    try:
        browser = WebBrowser(args[0].strip().lower())
        url = validate_web_url(args[1]) if len(args) == 2 else None
    except (ValueError, ProtocolError) as e:
        print(f"{usage}\n{e}", file=sys.stderr)
        return 2
    try:
        pid = launch_browser(browser, default_runner_root(), url)
    except ExecutionError as e:
        print(str(e), file=sys.stderr)
        return 1
    if pid is None:
        print("The test browser is already open.")
    else:
        print("Started the test browser.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
