import argparse
import sys

from controller_client.browser_diagnostics import build_browser_steps
from controller_client.config import setup_logging
from controller_client.desktop import get_desktop_backend
from controller_client.diagnostics import run_diagnostics
from controller_client.input_diagnostics import format_summary, run_input_checks
from controller_client.person_window_diagnostics import (
    build_window_steps,
    run_window_check,
)
from controller_client.protocol import BrowserName

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
    if not run_diagnostics(build_window_steps(), print):
        print(
            "The Test Runner window is off on this machine; Human in the loop "
            "steps can still be answered in Punk Hazard. Fix the problem above "
            "and re-run."
        )
        sys.exit(1)
    if interactive and not run_window_check(input, print):
        sys.exit(1)
    print("All window checks passed.")


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
            "answers Human in the loop steps."
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
            "check tkinter, the display and the Test Runner window: shows a "
            "sample step, hides it and takes one screenshot, asking yes/no "
            "questions"
        ),
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help=(
            "with --input or --window, run only the automatic checks " "(no questions)"
        ),
    )
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)
    if args.no_interactive and not (args.input or args.window):
        parser.error("--no-interactive can only be used with --input or --window")
    setup_logging(args.log_level)

    if args.input:
        _run_input_checks(interactive=not args.no_interactive)
        return

    if args.window:
        _run_window_checks(interactive=not args.no_interactive)
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
