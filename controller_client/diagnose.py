import argparse
import asyncio
import os
import sys
import tempfile

from controller_client.browser_diagnostics import build_browser_steps
from controller_client.capture_platform import detect_capture_platform
from controller_client.capture_session import interactive_session_problem
from controller_client.config import (
    TARGET_ENV_NAME,
    load_mobile_settings,
    setup_logging,
)
from controller_client.desktop import get_desktop_backend
from controller_client.diagnostics import run_diagnostics
from controller_client.input_diagnostics import format_summary, run_input_checks
from controller_client.mobile_diagnostics import mobile_diagnostic_steps
from controller_client.person_window_diagnostics import (
    build_window_steps,
    run_window_check,
)
from controller_client.protocol import (
    MANUAL_SHOT_MAX_BYTES,
    MANUAL_SHOT_MAX_PIXELS,
    BrowserName,
    CaptureStatus,
    CaptureTarget,
    CaptureWindowPayload,
    ManualShotMime,
)
from controller_client.window_capture import WindowCaptureService
from controller_client.window_capture_policy import build_deny_rules, is_denied
from controller_client.window_inventory import platform_capture
from controller_client.window_model import (
    CaptureUnavailableError,
    DesktopInfo,
    DesktopWindow,
)

_ALL_BROWSERS = "all"


def _parse_browsers(value: str) -> tuple[BrowserName, ...]:
    if value == _ALL_BROWSERS:
        return tuple(BrowserName)
    names = [name.strip() for name in value.split(",") if name.strip()]
    try:
        return tuple(BrowserName(name) for name in names)
    except ValueError as e:
        choices = ", ".join(b.value for b in BrowserName)
        raise argparse.ArgumentTypeError(
            f"unknown browser in {value!r}; choose from {choices}"
        ) from e


def _run_browser_diagnostics(browsers: tuple[BrowserName, ...]) -> None:
    # Each browser is checked on its own, so a missing one does not hide the
    # result of the others.
    results = [
        run_diagnostics((step,), print) for step in build_browser_steps(browsers)
    ]
    if all(results):
        print("All browser checks passed.")
        return
    print("Some browsers could not be launched; follow the hints above and re-run.")
    sys.exit(1)


def _run_input_checks(*, interactive: bool) -> None:
    report = run_input_checks(interactive=interactive, ask=input, report=print)
    print()
    print(format_summary(report))
    if report.failed:
        sys.exit(1)


def _run_window_checks(*, interactive: bool) -> None:
    if not run_diagnostics(build_window_steps(interactive=interactive), print):
        print(
            "The Test Runner window is off on this machine; Human in the loop "
            "steps can still be answered in Punk Hazard. Fix the problem above "
            "and re-run."
        )
        sys.exit(1)
    if interactive and not run_window_check(input, print):
        sys.exit(1)
    print("All window checks passed.")


def _run_mobile_checks(*, interactive: bool) -> None:
    settings, problem = load_mobile_settings()
    if problem is not None:
        print(f"{problem}. Fix it in controller_client/.env and re-run.")
        sys.exit(1)
    if settings is None:
        print(
            f"{TARGET_ENV_NAME} is desktop: set {TARGET_ENV_NAME}=android or "
            f"{TARGET_ENV_NAME}=ios in controller_client/.env to check a phone "
            "or tablet."
        )
        sys.exit(1)
    if run_diagnostics(mobile_diagnostic_steps(settings, interactive), print):
        print("All phone checks passed.")
        return
    print("Phone checks failed; fix the problem above and re-run.")
    sys.exit(1)


def _describe_desktop(window: DesktopWindow) -> str:
    desktop = window.desktop
    where = "current" if desktop.current else "other"
    index = "?" if desktop.index is None else str(desktop.index)
    name = f" ({desktop.name})" if desktop.name else ""
    return f"{index}{name}, {where}"


def _print_desktops(info: DesktopInfo) -> None:
    current = "unknown" if info.current is None else str(info.current)
    count = "unknown" if info.count is None else str(info.count)
    print(f"Desktops: current {current} of {count}")
    for index, name in enumerate(info.names, start=1):
        print(f"  desktop {index}: {name or '(unnamed)'}")


def _capture_failed(what: str, status: CaptureStatus, detail: str) -> None:
    print(f"[FAIL] {what}: {status.value}" + (f": {detail}" if detail else ""))
    sys.exit(1)


def _run_capture_checks() -> None:
    """Session check, platform path, every window (process, size, desktop,
    state; no titles) and one test capture of the focused window (or the
    topmost one the Test Runner may capture) into a temp file; never
    uploads."""
    print(f"Capture path: {detect_capture_platform().value}")
    problem = interactive_session_problem()
    if problem is not None:
        _capture_failed("Session", *problem)
    print("[ok] Session: interactive")
    try:
        parts = platform_capture()
        info, windows = parts.inventory.list_windows()
        deny = build_deny_rules(windows)
    except CaptureUnavailableError as e:
        _capture_failed("Window list", e.status, e.detail)
        return
    print(f"[ok] Window list: {len(windows)} windows")
    _print_desktops(info)
    for window in windows:
        flags = [
            flag
            for flag, on in (
                ("minimised", window.minimized),
                ("focused", window.focused),
                ("overlay", not window.capturable),
                ("not drawn", not window.drawn),
                ("never captured", is_denied(window, deny) or window.app_cloaked),
            )
            if on
        ]
        print(
            f"  id {window.window_id}: process {window.process or '(unknown)'}, "
            f"{window.rect.width}x{window.rect.height}, desktop "
            f"{_describe_desktop(window)}" + (f", {', '.join(flags)}" if flags else "")
        )
    candidates = [
        w
        for w in windows
        if w.capturable
        and w.pid is not None
        and w.desktop.current
        and not w.minimized
        and not w.app_cloaked
        and not is_denied(w, deny)
    ]
    if not candidates:
        print(
            "[FAIL] Test capture: no window on this desktop may be captured "
            "(terminals, the system shell and the Test Runner never are); open "
            "an application window and re-run."
        )
        sys.exit(1)
    pick = next((w for w in candidates if w.focused), candidates[0])
    payload = CaptureWindowPayload(
        target=CaptureTarget.WINDOW,
        process=None,
        title_contains=None,
        window_id=pick.window_id,
        region=None,
        delay_ms=0,
        allowed=(pick.process,),
        max_bytes=MANUAL_SHOT_MAX_BYTES,
        max_pixels=MANUAL_SHOT_MAX_PIXELS,
        switch_desktop=False,
    )
    result = asyncio.run(WindowCaptureService(None).capture(payload))
    if result.status is not CaptureStatus.OK:
        _capture_failed("Test capture", result.status, result.detail)
    suffix = ".png" if result.mime is ManualShotMime.PNG else ".jpg"
    fd, path = tempfile.mkstemp(prefix="test-runner-capture-", suffix=suffix)
    with os.fdopen(fd, "wb") as file:
        file.write(result.data)
    print(
        f"[ok] Test capture: {pick.process}, {result.width}x{result.height}, "
        f"saved to {path} (not uploaded)"
    )
    print(
        "All capture checks passed. Allowlist the process names shown above "
        "in the project's Settings."
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m controller_client.diagnose",
        description=(
            "Check that this machine lets the Test Runner capture the screen "
            "and drive input. "
            "With --browsers, check the browsers instead. "
            "With --input, check the pointer mapping, scroll direction and "
            "modifier release live, ending with a summary to copy and paste. "
            "With --window, check the Test Runner window where a person "
            "answers Human in the loop steps. "
            "With --mobile, check the phone or tablet set in CONTROLLER_TARGET. "
            "With --capture, check window listing and capture for screenshots."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--browsers",
        nargs="?",
        const=_ALL_BROWSERS,
        default=None,
        type=_parse_browsers,
        metavar="NAME[,NAME...]",
        help=(
            "only check that these browsers launch (comma separated; all of "
            f"{', '.join(b.value for b in BrowserName)} when no list is given)"
        ),
    )
    mode.add_argument(
        "--input",
        action="store_true",
        help=(
            "check screenshot-to-pointer mapping, scroll direction and "
            "modifier release; moves the pointer and asks yes/no questions"
        ),
    )
    mode.add_argument(
        "--window",
        action="store_true",
        help=(
            "check tkinter, the display, the Test Runner window and the Manual "
            "run shortcuts: asks for one shortcut press, shows a sample step, "
            "hides it and takes one screenshot, asking yes/no questions"
        ),
    )
    mode.add_argument(
        "--mobile",
        action="store_true",
        help=(
            "check Node, Appium and its driver, the platform tools, the device "
            "(CONTROLLER_TARGET / CONTROLLER_DEVICE), a session and a "
            "screenshot; asks before a test tap at the screen centre"
        ),
    )
    mode.add_argument(
        "--capture",
        action="store_true",
        help=(
            "check the session, list every window with its process, size and "
            "desktop, and save one test capture of the focused window to a "
            "temp file (never uploaded)"
        ),
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help=(
            "with --input, --window or --mobile, run only the automatic checks "
            "(no questions)"
        ),
    )
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)
    if args.no_interactive and not (args.input or args.window or args.mobile):
        parser.error(
            "--no-interactive can only be used with --input, --window or --mobile"
        )
    setup_logging(args.log_level)

    if args.input:
        _run_input_checks(interactive=not args.no_interactive)
        return

    if args.window:
        _run_window_checks(interactive=not args.no_interactive)
        return

    if args.mobile:
        _run_mobile_checks(interactive=not args.no_interactive)
        return

    if args.capture:
        _run_capture_checks()
        return

    if args.browsers is not None:
        _run_browser_diagnostics(args.browsers)
        return

    passed = run_diagnostics(get_desktop_backend().diagnostic_steps(), print)
    if passed:
        print("All diagnostics passed.")
        return
    print("Diagnostics failed; fix the problem above and re-run.")
    sys.exit(1)


if __name__ == "__main__":
    main()
