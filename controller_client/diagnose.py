import argparse
import sys

from controller_client.browser_diagnostics import build_browser_steps
from controller_client.config import setup_logging
from controller_client.diagnostics import DiagnosticStep, run_diagnostics
from controller_client.protocol import BrowserName
from controller_client.screen_capture import describe_screen_capture_failure

_ALL_BROWSERS = "all"

_SCREEN_CAPTURE_HINT = (
    "The Test Runner must run inside an interactive desktop session that may "
    "capture the screen: on macOS grant Screen Recording to the terminal, on "
    "Linux make sure DISPLAY points at the desktop, on Windows run it from a "
    "logged-in desktop session rather than a service."
)


def _check_screen_capture() -> str:
    failure = describe_screen_capture_failure()
    if failure is not None:
        raise RuntimeError(f"{failure}. {_SCREEN_CAPTURE_HINT}")
    return "the screen can be captured"


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m controller_client.diagnose",
        description=(
            "Check that this machine lets the Test Runner capture the screen. "
            "With --browsers, check the browsers instead."
        ),
    )
    parser.add_argument(
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
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    if args.browsers is not None:
        _run_browser_diagnostics(args.browsers)
        return

    passed = run_diagnostics(
        (DiagnosticStep("screen capture", _check_screen_capture),), print
    )
    if passed:
        print("All diagnostics passed.")
        return
    print("Diagnostics failed; fix the problem above and re-run.")
    sys.exit(1)


if __name__ == "__main__":
    main()
