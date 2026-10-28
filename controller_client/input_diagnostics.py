"""Live input checks for ``python -m controller_client.diagnose --input``.

Each check yields a verdict with its measured numbers, and every check runs
even when an earlier one fails, so one pasted summary answers all of them:
the screenshot-to-input mapping against the OS display scale, the scroll
direction, and whether modifiers stay held after a key combination.

Only harmless keys are sent: F19 (macOS) and F24 (Windows) with modifiers,
never a letter, a lone Win key, or anything that locks, quits or closes.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from controller_client import executor
from controller_client.desktop import get_desktop_backend
from controller_client.desktop_backend import DesktopBackend
from controller_client.display_scale import (
    NO_REFERENCE,
    compare_spaces,
    read_display_scale,
)
from controller_client.input_guard import ensure_input_not_blocked
from controller_client.protocol import (
    CLIENT_VERSION,
    HoverPayload,
    ScrollDirection,
    ScrollPayload,
    TypeTextPayload,
)

# pyautogui crashes on import without X, so Linux (GNOME Wayland) never loads
# it; the checks that need it report N/A there (same rule as desktop.py).
if sys.platform != "linux":
    import pyautogui

    from controller_client.desktop_pyautogui import held_modifiers, press_combination

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import termios

if sys.platform == "darwin":
    import Quartz

OK: Final = "OK"
MISMATCH: Final = "MISMATCH"
FAIL: Final = "FAIL"
YES: Final = "YES"
NO: Final = "NO"
SKIPPED: Final = "SKIPPED"
NOT_APPLICABLE: Final = "N/A"
INFO: Final = "INFO"

_ATTENTION_VERDICTS: Final = frozenset({MISMATCH, FAIL, NO})

_BEGIN_MARKER: Final = "----- BEGIN PUNK HAZARD INPUT CHECK -----"
_END_MARKER: Final = "----- END PUNK HAZARD INPUT CHECK -----"

_POINTER_HOLD_SECONDS: Final = 3.0
_LOOK_SECONDS: Final = 1.5
_PLACE_POINTER_SECONDS: Final = 5
_SCROLL_NOTCHES: Final = 5
_SETTLE_SECONDS: Final = 0.25
_FULLSCREEN_ANIMATION_SECONDS: Final = 1.5
_READBACK_TOLERANCE: Final = 1
_CORNER_INSET: Final = 40

# Function keys with no default binding, held with modifiers only.
if sys.platform == "darwin":
    _MODIFIER_COMBOS: Final[tuple[tuple[str, ...], ...]] = (
        ("ctrl", "command", "f19"),
        ("shift", "option", "f19"),
        ("command", "shift", "f19"),
    )
else:
    # A non-modifier key between Win down and up keeps Start closed.
    _MODIFIER_COMBOS = (
        ("ctrl", "shift", "f24"),
        ("ctrl", "alt", "f24"),
        ("win", "f24"),
    )


@dataclass(frozen=True)
class CheckResult:
    name: str
    verdict: str
    detail: str


@dataclass(frozen=True)
class InputCheckReport:
    environment: str
    results: tuple[CheckResult, ...]

    @property
    def attention(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.verdict in _ATTENTION_VERDICTS)

    @property
    def failed(self) -> bool:
        return bool(self.attention)


@dataclass(frozen=True)
class _Target:
    name: str
    where: str
    x: int
    y: int


class _Stopped(Exception):
    """The person ended input at a prompt (EOF or Ctrl+C)."""


class _Session:
    def __init__(
        self,
        backend: DesktopBackend,
        ask: Callable[[str], str],
        report: Callable[[str], None],
    ) -> None:
        self.backend = backend
        self._ask = ask
        self.report = report

    def ask(self, question: str) -> str:
        try:
            return self._ask(question).strip().lower()
        except (EOFError, KeyboardInterrupt) as e:
            raise _Stopped() from e

    def ask_yes_no_skip(self, question: str) -> str:
        while True:
            answer = self.ask(f"{question} [y/n/s] ")
            if answer in ("y", "yes"):
                return YES
            if answer in ("n", "no"):
                return NO
            if answer in ("s", "skip"):
                return SKIPPED
            self.report("Please answer y (yes), n (no) or s (skip).")

    def ask_default_no(self, question: str) -> bool:
        return self.ask(f"{question} [y/N] ") in ("y", "yes")

    def wait_for_enter(self, instruction: str) -> None:
        self.ask(f"{instruction} Press Enter when ready. ")

    def countdown(self, seconds: int, what: str) -> None:
        for left in range(seconds, 0, -1):
            self.report(f"  {what} in {left} s ...")
            time.sleep(1)


def run_input_checks(
    *,
    interactive: bool,
    ask: Callable[[str], str],
    report: Callable[[str], None],
) -> InputCheckReport:
    session = _Session(get_desktop_backend(), ask, report)
    environment = _environment(session.backend)
    report(f"env: {environment}")

    checks: tuple[tuple[str, bool, Callable[[_Session], CheckResult]], ...] = (
        ("spaces", False, _check_spaces),
        ("os-scale", False, _check_os_scale),
        ("pointer-readback", False, _check_pointer_readback),
        ("pointer-seen", True, _check_pointer_seen),
        ("scroll", True, _check_scroll),
        ("modifiers", False, _check_modifiers),
        ("fullscreen-repro", True, _check_fullscreen_repro),
    )
    results: list[CheckResult] = []
    stopped = False
    for name, needs_person, check in checks:
        if stopped:
            result = CheckResult(name, SKIPPED, "input ended at a prompt")
        elif needs_person and not interactive:
            result = CheckResult(name, SKIPPED, "--no-interactive")
        else:
            report(f"\n== {name} ==")
            try:
                result = check(session)
            except (_Stopped, KeyboardInterrupt):
                stopped = True
                result = CheckResult(name, SKIPPED, "input ended at a prompt")
            except Exception as e:
                result = CheckResult(name, FAIL, f"{type(e).__name__}: {e}")
        report(_line(result))
        results.append(result)
    return InputCheckReport(environment, tuple(results))


def format_summary(report: InputCheckReport) -> str:
    attention = report.attention
    if attention:
        names = ", ".join(r.name for r in attention)
        outcome = f"ATTENTION ({len(attention)} checks: {names})"
    else:
        outcome = "PASS"
    lines = [
        _BEGIN_MARKER,
        f"env: {report.environment}",
        *(_line(r) for r in report.results),
        f"result: {outcome}",
        _END_MARKER,
    ]
    return "\n".join(_ascii(line) for line in lines)


def _line(result: CheckResult) -> str:
    return f"{result.name}: {result.verdict} {result.detail}".rstrip()


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


# --- environment -----------------------------------------------------------


def _environment(backend: DesktopBackend) -> str:
    parts = [
        _os_description(),
        platform.platform(),
        f"py {platform.python_version()}",
        f"client {CLIENT_VERSION}",
        f"backend {backend.name}",
        _package_versions(),
    ]
    return " | ".join(part for part in parts if part)


def _os_description() -> str:
    try:
        if sys.platform == "darwin":
            return f"macOS {platform.mac_ver()[0]} {platform.machine()}"
        if sys.platform == "win32":
            release, version, _, _ = platform.win32_ver()
            edition = platform.win32_edition() or "unknown edition"
            return f"Windows {release} {version} {edition} {platform.machine()}"
        session = os.environ.get("XDG_SESSION_TYPE", "unknown session")
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown desktop")
        return (
            f"Linux {session} {desktop} {_gnome_shell_version()} {platform.machine()}"
        )
    except Exception as e:
        return f"OS unknown ({type(e).__name__}: {e})"


def _gnome_shell_version() -> str:
    try:
        completed = subprocess.run(
            ["gnome-shell", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "gnome-shell unknown"
    return completed.stdout.strip() or "gnome-shell unknown"


def _package_versions() -> str:
    if sys.platform == "darwin":
        packages: tuple[str, ...] = ("PyAutoGUI", "Pillow", "pyobjc-core")
    elif sys.platform == "win32":
        packages = ("PyAutoGUI", "Pillow")
    else:
        packages = ("Pillow", "dbus-fast")
    versions = []
    for package in packages:
        try:
            versions.append(f"{package} {importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            versions.append(f"{package} missing")
    return " ".join(versions)


# --- screen spaces and OS scale ----------------------------------------------


def _size(size: tuple[int, int]) -> str:
    return f"{size[0]}x{size[1]}"


def _check_spaces(session: _Session) -> CheckResult:
    session.backend.screenshot()
    capture = session.backend.capture_size()
    input_size = session.backend.input_size()
    ratio_x = input_size[0] / capture[0] if capture[0] else 0.0
    ratio_y = input_size[1] / capture[1] if capture[1] else 0.0
    mapping = "mapping active" if capture != input_size else "mapping no-op"
    return CheckResult(
        "spaces",
        INFO,
        f"capture {_size(capture)} input {_size(input_size)} "
        f"ratio {ratio_x:.3f}x{ratio_y:.3f} {mapping}",
    )


def _check_os_scale(session: _Session) -> CheckResult:
    capture = session.backend.capture_size()
    input_size = session.backend.input_size()
    scale = read_display_scale()
    verdict, explanation = compare_spaces(capture, input_size, scale)
    # The explanation already names the sizes and the source.
    parts = [explanation]
    if scale.detail:
        parts.append(scale.detail)
    if verdict == NO_REFERENCE:
        # GNOME: both sizes are the stream frame, so the mapping is identity.
        return CheckResult(
            "os-scale", NOT_APPLICABLE, "no reference; " + "; ".join(parts)
        )
    return CheckResult("os-scale", verdict, "; ".join(parts))


# --- pointer -----------------------------------------------------------------


def _targets(backend: DesktopBackend) -> tuple[_Target, ...]:
    # The quarter target exposes a factor-2 error that a corner target alone
    # hides: the OS clamps an out-of-range move to the corner.
    width, height = backend.capture_size()
    return (
        _Target("centre", "in the middle of the screen", width // 2, height // 2),
        _Target(
            "quarter",
            "in the middle of the top-left quarter of the screen",
            width // 4,
            height // 4,
        ),
        _Target(
            "corner",
            f"about {_CORNER_INSET} px from the bottom-right corner",
            max(0, width - _CORNER_INSET),
            max(0, height - _CORNER_INSET),
        ),
    )


if sys.platform == "linux":

    def _check_pointer_readback(session: _Session) -> CheckResult:
        return CheckResult(
            "pointer-readback",
            NOT_APPLICABLE,
            "mutter cannot report the pointer position",
        )

else:

    def _check_pointer_readback(session: _Session) -> CheckResult:
        original = pyautogui.position()
        entries: list[str] = []
        all_ok = True
        try:
            for target in _targets(session.backend):
                expected = executor.to_input(target.x, target.y)
                executor.execute_hover(HoverPayload(target.x, target.y))
                got = pyautogui.position()
                ok = (
                    abs(got.x - expected[0]) <= _READBACK_TOLERANCE
                    and abs(got.y - expected[1]) <= _READBACK_TOLERANCE
                )
                all_ok = all_ok and ok
                entries.append(
                    f"{target.name} ({target.x},{target.y})->({expected[0]},{expected[1]}) "
                    f"got ({got.x},{got.y}) {OK if ok else MISMATCH}"
                )
        finally:
            session.backend.move_to(original.x, original.y)
        return CheckResult(
            "pointer-readback", OK if all_ok else MISMATCH, "; ".join(entries)
        )


def _check_pointer_seen(session: _Session) -> CheckResult:
    answers: list[tuple[str, str]] = []
    for target in _targets(session.backend):
        session.report(
            f"Look at the screen: the pointer moves {target.where} and stays "
            f"there for {_POINTER_HOLD_SECONDS:.0f} s."
        )
        time.sleep(_LOOK_SECONDS)
        executor.execute_hover(HoverPayload(target.x, target.y))
        time.sleep(_POINTER_HOLD_SECONDS)
        answer = session.ask_yes_no_skip(f"Was the pointer {target.where}?")
        answers.append((target.name, answer))
    return CheckResult(
        "pointer-seen",
        _combined(a for _, a in answers),
        ", ".join(f"{name} {answer}" for name, answer in answers),
    )


def _combined(answers: Iterable[str]) -> str:
    values = list(answers)
    if NO in values:
        return NO
    if YES in values:
        return YES
    return SKIPPED


# --- scroll ------------------------------------------------------------------


def _scroll_once(session: _Session, direction: ScrollDirection, question: str) -> str:
    executor.execute_scroll(ScrollPayload(direction, _SCROLL_NOTCHES, None, None))
    return session.ask_yes_no_skip(question)


def _place_pointer(session: _Session, content: str) -> None:
    session.wait_for_enter(
        f"Open {content} and leave it visible next to this terminal. After you "
        f"press Enter you have {_PLACE_POINTER_SECONDS} s to rest the pointer "
        "over it WITHOUT clicking (this terminal keeps the keyboard)."
    )
    session.countdown(_PLACE_POINTER_SECONDS, "Scrolling")


if sys.platform == "win32":
    _SPI_GETMOUSEWHEELROUTING: Final = 0x201C
    _WHEEL_ROUTING_NAMES: Final[dict[int, str]] = {
        0: "focus",
        1: "hybrid",
        2: "pointer",
    }

    def _wheel_routing() -> str:
        # Where Windows sends wheel input: the focused window, or the window
        # under the pointer ("Scroll inactive windows when I hover over them").
        try:
            # A private instance so these argtypes touch no other module's.
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            spi = user32.SystemParametersInfoW
            spi.argtypes = [
                wintypes.UINT,
                wintypes.UINT,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.UINT,
            ]
            spi.restype = wintypes.BOOL
            value = wintypes.DWORD()
            if not spi(_SPI_GETMOUSEWHEELROUTING, 0, ctypes.byref(value), 0):
                return "unknown"
        except (OSError, AttributeError):
            return "unknown"
        routing = int(value.value)
        return _WHEEL_ROUTING_NAMES.get(routing, f"unknown ({routing})")

else:

    def _wheel_routing() -> str:
        return "n/a"


def _check_scroll(session: _Session) -> CheckResult:
    answers: dict[ScrollDirection, str] = {}
    platform_detail = sys.platform
    if sys.platform == "win32":
        routing = _wheel_routing()
        platform_detail = f"{sys.platform} wheel-routing {routing}"
        session.report(
            "Windows scrolls the window under the pointer only when Settings > "
            "Mouse > 'Scroll inactive windows when I hover over them' is on "
            f"(wheel routing here: {routing}). Keep the pointer over the window; "
            "if that setting is off, click the window to focus it first."
        )
    _place_pointer(session, "a long scrollable window (a long web page or document)")
    answers[ScrollDirection.DOWN] = _scroll_once(
        session,
        ScrollDirection.DOWN,
        "Did the content move to show later content, as when scrolling down?",
    )
    answers[ScrollDirection.UP] = _scroll_once(
        session,
        ScrollDirection.UP,
        "Did it go back up to where it started?",
    )
    if session.ask_default_no(
        "Test left/right too? It needs horizontally scrollable content, such as "
        "a zoomed-in page or a wide spreadsheet."
    ):
        _place_pointer(session, "horizontally scrollable content")
        answers[ScrollDirection.RIGHT] = _scroll_once(
            session,
            ScrollDirection.RIGHT,
            "Did later content to the right come into view?",
        )
        answers[ScrollDirection.LEFT] = _scroll_once(
            session,
            ScrollDirection.LEFT,
            "Did it go back left to where it started?",
        )
    order = (
        ScrollDirection.DOWN,
        ScrollDirection.UP,
        ScrollDirection.RIGHT,
        ScrollDirection.LEFT,
    )
    detail = ", ".join(f"{d.value} {answers.get(d, SKIPPED)}" for d in order)
    return CheckResult(
        "scroll", _combined(answers.values()), f"{platform_detail} {detail}"
    )


# --- modifiers ---------------------------------------------------------------


def _names(held: Sequence[str]) -> str:
    return "[" + ",".join(held) + "]"


def _resolve(backend: DesktopBackend, keys: Sequence[str]) -> list[str]:
    resolved = []
    for key in keys:
        name = backend.resolve_key(key)
        if name is None:
            raise ValueError(f"the backend does not know the key {key!r}")
        resolved.append(name)
    return resolved


def _discard_terminal_input() -> None:
    # The combinations go to the focused window, which is usually this
    # terminal; drop anything they queued so it never reaches the next prompt.
    if sys.platform == "win32":
        while msvcrt.kbhit():
            msvcrt.getwch()
    elif sys.stdin.isatty():
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)


if sys.platform == "linux":

    def _check_modifiers(session: _Session) -> CheckResult:
        return CheckResult(
            "modifiers",
            NOT_APPLICABLE,
            "GNOME releases every key it pressed; no OS modifier state to read",
        )

else:

    def _check_modifiers(session: _Session) -> CheckResult:
        already = held_modifiers()
        if already:
            return CheckResult(
                "modifiers", SKIPPED, f"a modifier was already down {_names(already)}"
            )
        ensure_input_not_blocked()
        entries: list[str] = []
        verdicts: list[str] = []
        try:
            for combo in _MODIFIER_COMBOS:
                keys = _resolve(session.backend, combo)
                before = press_combination(keys)
                after = held_modifiers()
                time.sleep(_SETTLE_SECONDS)
                later = held_modifiers()
                if after or later:
                    verdict = FAIL
                elif before:
                    verdict = "INFO (released)"
                else:
                    verdict = OK
                verdicts.append(verdict)
                entries.append(
                    f"{'+'.join(combo)} {verdict} before{_names(before)} "
                    f"after{_names(after)} 250ms{_names(later)}"
                )
        finally:
            _discard_terminal_input()
        if FAIL in verdicts:
            overall = FAIL
        elif any(v != OK for v in verdicts):
            overall = INFO
        else:
            overall = OK
        return CheckResult("modifiers", overall, "; ".join(entries))


if sys.platform == "darwin":
    _TEXTEDIT_OWNER: Final = "TextEdit"

    def _frontmost_window_owner() -> str | None:
        # On-screen windows come front to back; layer 0 is ordinary app
        # windows (menu bar, Dock and overlays sit on other layers).
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        for window in windows or ():
            if window.get(Quartz.kCGWindowLayer) == 0:
                owner = window.get(Quartz.kCGWindowOwnerName)
                return owner if isinstance(owner, str) else None
        return None

    def _not_textedit(when: str) -> CheckResult | None:
        owner = _frontmost_window_owner()
        if owner == _TEXTEDIT_OWNER:
            return None
        return CheckResult(
            "fullscreen-repro",
            SKIPPED,
            f"frontmost window {when} belongs to {owner or 'nothing'}, not "
            f"{_TEXTEDIT_OWNER}",
        )

    def _check_fullscreen_repro(session: _Session) -> CheckResult:
        if not session.ask_default_no(
            "Run the TextEdit reproduction? It toggles full screen twice in the "
            "window you pick."
        ):
            return CheckResult("fullscreen-repro", SKIPPED, "not chosen")
        session.wait_for_enter(
            "Open a TextEdit document. After you press Enter you have "
            f"{_PLACE_POINTER_SECONDS} s to click into the document."
        )
        session.countdown(_PLACE_POINTER_SECONDS, "Toggling full screen")
        skipped = _not_textedit("before the shortcut")
        if skipped is not None:
            return skipped
        keys = _resolve(session.backend, ("ctrl", "command", "f"))
        first = press_combination(keys)
        time.sleep(_FULLSCREEN_ANIMATION_SECONDS)
        second = press_combination(keys)
        time.sleep(_FULLSCREEN_ANIMATION_SECONDS)
        skipped = _not_textedit("before typing")
        if skipped is not None:
            return skipped
        executor.execute_type_text(TypeTextPayload(" E1 typed", 0.0))
        # Let any terminal echo arrive before it is discarded.
        time.sleep(_SETTLE_SECONDS)
        _discard_terminal_input()
        answer = session.ask_yes_no_skip(
            "Click back into this terminal. Did ' E1 typed' arrive intact in "
            "TextEdit, with no emoji picker?"
        )
        return CheckResult(
            "fullscreen-repro",
            answer,
            f"first before{_names(first)} second before{_names(second)}",
        )

else:

    def _check_fullscreen_repro(session: _Session) -> CheckResult:
        return CheckResult("fullscreen-repro", NOT_APPLICABLE, "macOS only")
