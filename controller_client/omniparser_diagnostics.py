from __future__ import annotations

import base64
import importlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from controller_client.exceptions import ExecutionError, OmniParserError
from controller_client.executor import execute_screenshot
from controller_client.omniparser_config import (
    omniparser_max_result_bytes,
    omniparser_weights_dir,
)
from controller_client.omniparser_executor import (
    MAX_RESULT_BYTES_ENV_VAR,
    detect_device,
    execute_find_element,
    load_omniparser_model,
    measure_result_message_bytes,
    resolve_omniparser_weights,
)
from controller_client.protocol import FindElementPayload, FindElementResultPayload

REQUIRED_MODULES: Final[tuple[str, ...]] = (
    "torch",
    "torchvision",
    "ultralytics",
    "transformers",
    "easyocr",
    "paddleocr",
    "cv2",
    "supervision",
    "PIL",
    "pyautogui",
)

SETUP_HINT: Final[str] = (
    "Re-run controller_client/scripts/setup.sh (or setup.ps1 / setup.bat), or "
    "install the dependencies into the controller venv with "
    "`python -m pip install -r controller_client/requirements.txt`."
)
WEIGHTS_HINT: Final[str] = (
    "Run controller_client/scripts/download_omniparser_weights.sh (the setup "
    "scripts run it for you) or point OMNIPARSER_WEIGHTS_DIR in "
    "controller_client/.env at an existing weights folder."
)
SCREENSHOT_HINT: Final[str] = (
    "The controller must run inside an interactive desktop session that may "
    "capture the screen: on macOS grant Screen Recording to the terminal, on "
    "Linux make sure DISPLAY points at the desktop, on Windows run it from a "
    "logged-in desktop session rather than a service."
)


@dataclass(frozen=True)
class DiagnosticStep:
    name: str
    run: Callable[[], str]


def run_diagnostics(
    steps: Iterable[DiagnosticStep], report: Callable[[str], None]
) -> bool:
    """Run steps in order, reporting each; stop at the first failure."""
    for step in steps:
        try:
            detail = step.run()
        except Exception as e:
            report(f"[FAIL] {step.name}: {_describe_failure(e)}")
            return False
        report(f"[ok] {step.name}: {detail}")
    return True


def _describe_failure(error: Exception) -> str:
    if isinstance(error, OmniParserError):
        return f"{error} [{error.details()}]"
    return str(error)


def check_required_imports() -> str:
    versions: list[str] = []
    for name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError as e:
            raise RuntimeError(
                f"Python module {name!r} cannot be imported ({e}). {SETUP_HINT}"
            ) from e
        versions.append(f"{name} {getattr(module, '__version__', '?')}")
    return ", ".join(versions)


def check_weights_dir() -> str:
    weights_dir = Path(omniparser_weights_dir()).resolve()
    if not weights_dir.is_dir():
        raise RuntimeError(
            f"Weights directory {weights_dir} does not exist. {WEIGHTS_HINT}"
        )
    return str(weights_dir)


def check_weight_files() -> str:
    try:
        weights = resolve_omniparser_weights(omniparser_weights_dir())
    except FileNotFoundError as e:
        raise RuntimeError(f"{e} {WEIGHTS_HINT}") from e
    detector_bytes = weights.som_model_path.stat().st_size
    caption_files = sum(1 for p in weights.caption_model_path.iterdir() if p.is_file())
    return (
        f"{weights.som_model_path} ({detector_bytes} bytes), "
        f"{weights.caption_model_path} ({caption_files} files)"
    )


def check_screenshot() -> str:
    try:
        screenshot = execute_screenshot()
    except ExecutionError as e:
        raise RuntimeError(f"{e}. {SCREENSHOT_HINT}") from e
    png_bytes = len(base64.b64decode(screenshot.image_base64))
    return f"{screenshot.width}x{screenshot.height}, {png_bytes} PNG bytes"


def check_device() -> str:
    return detect_device()


def check_model_load() -> str:
    result = load_omniparser_model()
    return (
        f"loaded on {result.device} from {result.weights_dir} "
        f"in {result.load_seconds:.1f}s"
    )


class _InferenceOutcome:
    """Carries the inference result from the inference step to the size step."""

    def __init__(self) -> None:
        self.result: FindElementResultPayload | None = None

    def run_inference(self) -> str:
        started = time.monotonic()
        result = execute_find_element(
            FindElementPayload(box_threshold=None, iou_threshold=None)
        )
        elapsed = time.monotonic() - started
        self.result = result
        return (
            f"{len(result.elements)} elements in {elapsed:.1f}s "
            f"({result.image_width}x{result.image_height} screenshot)"
        )

    def check_result_size(self) -> str:
        if self.result is None:
            raise RuntimeError(
                "inference did not run, so there is no result to measure"
            )
        size = measure_result_message_bytes(self.result)
        limit = omniparser_max_result_bytes()
        return (
            f"{size} bytes serialized (limit {limit} bytes, {MAX_RESULT_BYTES_ENV_VAR})"
        )


def build_default_steps(skip_inference: bool) -> tuple[DiagnosticStep, ...]:
    steps = [
        DiagnosticStep("required imports", check_required_imports),
        DiagnosticStep("weights directory", check_weights_dir),
        DiagnosticStep("weight files", check_weight_files),
        DiagnosticStep("screenshot", check_screenshot),
        DiagnosticStep("device", check_device),
        DiagnosticStep("model construction", check_model_load),
    ]
    if not skip_inference:
        outcome = _InferenceOutcome()
        steps.append(DiagnosticStep("inference", outcome.run_inference))
        steps.append(DiagnosticStep("result size", outcome.check_result_size))
    return tuple(steps)
