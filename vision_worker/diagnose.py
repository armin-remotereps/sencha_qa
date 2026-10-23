"""Check a vision worker install step by step.

python -m vision_worker.diagnose [--image PATH] [--check-server]
"""

import argparse
import asyncio
import base64
import importlib
import io
import sys
import time
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw

from vision_worker.client import (
    connect,
    gather_system_info,
    perform_handshake,
    resolve_device_label,
    resolve_worker_name,
)
from vision_worker.config import (
    ConfigError,
    EngineConfig,
    load_config,
    load_engine_config,
    setup_logging,
)
from vision_worker.diagnostics import DiagnosticStep, run_diagnostics
from vision_worker.engine import (
    MAX_RESULT_BYTES_ENV_VAR,
    SETUP_HINT,
    WEIGHTS_HINT,
    VisionEngine,
    detect_device,
    gpu_info,
    measure_result_message_bytes,
    resolve_omniparser_weights,
)
from vision_worker.protocol import (
    MAX_WORKER_NAME_LENGTH,
    ParseRequestPayload,
    ParseResultPayload,
)

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
    "websockets",
)
# Generous: the diagnose parse never waits in a queue.
_DIAGNOSE_TIMEOUT_SECONDS: Final[float] = 600.0
# Suffix for the --check-server connection, so it never supersedes (and so
# stops) a worker that is already running under the configured name.
_DIAGNOSE_NAME_SUFFIX: Final[str] = "-diagnose"
_TEST_IMAGE_SIZE: Final[tuple[int, int]] = (1280, 800)


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


def _check_weights(config: EngineConfig) -> str:
    weights_dir = Path(config.weights_dir).resolve()
    if not weights_dir.is_dir():
        raise RuntimeError(
            f"Weights directory {weights_dir} does not exist. {WEIGHTS_HINT}"
        )
    weights = resolve_omniparser_weights(config.weights_dir)
    detector_bytes = weights.som_model_path.stat().st_size
    caption_files = sum(1 for p in weights.caption_model_path.iterdir() if p.is_file())
    return (
        f"{weights.som_model_path} ({detector_bytes} bytes), "
        f"{weights.caption_model_path} ({caption_files} files)"
    )


def _check_device(config: EngineConfig) -> str:
    device = detect_device(config.device)
    gpu = gpu_info(device)
    if gpu is None:
        return f"{device} (VISION_DEVICE={config.device.value})"
    return f"{device}: {gpu.name}, {gpu.vram_mb} MiB VRAM"


def _test_image_base64() -> str:
    """A small fake UI with text and buttons, built in memory."""
    image = Image.new("RGB", _TEST_IMAGE_SIZE, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, _TEST_IMAGE_SIZE[0], 60), fill=(40, 44, 52))
    draw.text((20, 20), "Punk Hazard vision worker diagnose", fill="white")
    for row, label in enumerate(("Sign in", "Create account", "Cancel")):
        top = 150 + row * 90
        draw.rectangle((100, top, 340, top + 50), outline="black", width=2)
        draw.text((120, top + 18), label, fill="black")
    draw.text((450, 160), "Email address", fill="black")
    draw.rectangle((450, 185, 900, 225), outline="gray", width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _read_image_base64(path: Path) -> str:
    with Image.open(path) as image:
        image.load()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _InferenceOutcome:
    """Carries the inference result from the inference step to the size step."""

    def __init__(self, engine: VisionEngine, image_path: Path | None) -> None:
        self._engine = engine
        self._image_path = image_path
        self.result: ParseResultPayload | None = None

    def load_model(self) -> str:
        result = self._engine.load()
        return (
            f"loaded on {result.device} from {result.weights_dir} "
            f"in {result.load_seconds:.1f}s"
        )

    def run_inference(self) -> str:
        if self._image_path is None:
            source = "generated test image"
            image_base64 = _test_image_base64()
        else:
            source = str(self._image_path)
            image_base64 = _read_image_base64(self._image_path)
        started = time.monotonic()
        result = self._engine.parse(
            ParseRequestPayload(
                image_base64=image_base64,
                box_threshold=None,
                iou_threshold=None,
                timeout_seconds=_DIAGNOSE_TIMEOUT_SECONDS,
            )
        )
        elapsed = time.monotonic() - started
        self.result = result
        return (
            f"{len(result.elements)} elements in {elapsed:.1f}s "
            f"(ocr {result.ocr_seconds:.1f}s, som {result.som_seconds:.1f}s; "
            f"{result.image_width}x{result.image_height} {source})"
        )

    def check_result_size(self) -> str:
        if self.result is None:
            raise RuntimeError(
                "inference did not run, so there is no result to measure"
            )
        size = measure_result_message_bytes(self.result)
        limit = self._engine.config.max_result_bytes
        return (
            f"{size} bytes serialized (limit {limit} bytes, {MAX_RESULT_BYTES_ENV_VAR})"
        )


async def _handshake_once() -> str:
    config = load_config([])
    device = resolve_device_label(config)
    base = resolve_worker_name(config, device)
    name = (
        base[: MAX_WORKER_NAME_LENGTH - len(_DIAGNOSE_NAME_SUFFIX)]
        + _DIAGNOSE_NAME_SUFFIX
    )
    async with connect(config.server_url) as connection:
        ack = await perform_handshake(
            connection,
            token=config.token,
            worker_name=name,
            system_info=gather_system_info(device),
        )
    return f"{config.server_url} accepted the handshake as {name!r} ({ack.status})"


def check_server() -> str:
    try:
        return asyncio.run(_handshake_once())
    except ConfigError as e:
        raise RuntimeError(f"Invalid configuration: {e}") from e


def build_steps(
    config: EngineConfig, image_path: Path | None, include_server: bool
) -> tuple[DiagnosticStep, ...]:
    engine = VisionEngine(config)
    outcome = _InferenceOutcome(engine, image_path)
    steps = [
        DiagnosticStep("required imports", check_required_imports),
        DiagnosticStep("weights", lambda: _check_weights(config)),
        DiagnosticStep("device", lambda: _check_device(config)),
        DiagnosticStep("model load", outcome.load_model),
        DiagnosticStep("inference", outcome.run_inference),
        DiagnosticStep("result size", outcome.check_result_size),
    ]
    if include_server:
        steps.append(DiagnosticStep("server handshake", check_server))
    return tuple(steps)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m vision_worker.diagnose",
        description=(
            "Check the vision worker step by step: dependencies, weights, "
            "device, model load, one inference and the result size. With "
            "--check-server, also connect to VISION_SERVER_URL and complete a "
            "handshake."
        ),
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=None,
        metavar="PATH",
        help="parse this screenshot instead of a generated test image",
    )
    parser.add_argument(
        "--check-server",
        action="store_true",
        help="also connect to the server and complete a handshake",
    )
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    if args.image is not None and not args.image.is_file():
        print(f"--image {args.image} is not a file.")
        sys.exit(2)
    try:
        engine_config = load_engine_config()
    except ConfigError as e:
        print(f"Invalid configuration: {e}")
        sys.exit(2)

    passed = run_diagnostics(
        build_steps(engine_config, args.image, args.check_server), print
    )
    if passed:
        print("All vision worker diagnostics passed.")
        return
    print("Diagnostics stopped at the first failure; fix it and re-run.")
    sys.exit(1)


if __name__ == "__main__":
    main()
