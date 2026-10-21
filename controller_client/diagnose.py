import argparse
import sys

from controller_client.browser_diagnostics import build_browser_steps
from controller_client.config import setup_logging
from controller_client.omniparser_diagnostics import (
    build_default_steps,
    run_diagnostics,
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m controller_client.diagnose",
        description=(
            "Check the OmniParser setup step by step: dependencies, weights, "
            "screenshot capture, device, model load and one inference. With "
            "--browsers, check the browsers instead."
        ),
    )
    parser.add_argument(
        "--skip-inference",
        action="store_true",
        help="stop after loading the models instead of also parsing a screenshot",
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
        build_default_steps(skip_inference=args.skip_inference), print
    )
    if passed:
        print("All OmniParser diagnostics passed.")
        return
    print("Diagnostics stopped at the first failure; fix it and re-run.")
    sys.exit(1)


if __name__ == "__main__":
    main()
