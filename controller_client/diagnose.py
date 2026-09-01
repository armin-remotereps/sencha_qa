import argparse
import sys

from controller_client.config import setup_logging
from controller_client.omniparser_diagnostics import (
    build_default_steps,
    run_diagnostics,
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m controller_client.diagnose",
        description=(
            "Check the OmniParser setup step by step: dependencies, weights, "
            "screenshot capture, device, model load and one inference."
        ),
    )
    parser.add_argument(
        "--skip-inference",
        action="store_true",
        help="stop after loading the models instead of also parsing a screenshot",
    )
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)
    setup_logging(args.log_level)

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
