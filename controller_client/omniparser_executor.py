from __future__ import annotations

import base64
import dataclasses
import gc
import io
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypedDict, TypeVar

import torch
from PIL import Image

from controller_client.exceptions import ExecutionError, OmniParserError
from controller_client.executor import execute_screenshot
from controller_client.omniparser_config import (
    omniparser_box_threshold,
    omniparser_caption_batch_size,
    omniparser_iou_threshold,
    omniparser_max_result_bytes,
    omniparser_weights_dir,
)
from controller_client.protocol import (
    ErrorCode,
    FindElementPayload,
    FindElementResultPayload,
    OmniParserState,
    OmniParserStatusPayload,
    PixelBBoxPayload,
    PixelElementPayload,
    ScreenshotResponsePayload,
    serialize_find_element_result,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

BOX_OVERLAY_DIVISOR: Final[int] = 3200
OCR_TEXT_THRESHOLD: Final[float] = 0.8

# Downscaling the annotated image below this long edge makes the numbered
# labels unreadable for the vision model, so shrinking stops there.
MIN_ANNOTATED_LONG_EDGE: Final[int] = 1280
DOWNSCALE_FACTOR: Final[float] = 0.75
MAX_RESULT_BYTES_ENV_VAR: Final[str] = "OMNIPARSER_MAX_RESULT_BYTES"
# Same length as the uuid4 request ids the server sends, so the measured
# size matches the message that actually goes over the wire.
_SIZE_PROBE_REQUEST_ID: Final[str] = "00000000-0000-0000-0000-000000000000"

LOAD_PHASE_NOT_STARTED: Final[str] = "not_started"
LOAD_PHASE_WEIGHTS: Final[str] = "weights"
LOAD_PHASE_IMPORTS: Final[str] = "imports"
LOAD_PHASE_DEVICE: Final[str] = "device"
LOAD_PHASE_MODEL_LOAD: Final[str] = "model_load"
FIND_PHASE_SCREENSHOT: Final[str] = "screenshot"
FIND_PHASE_OCR: Final[str] = "ocr"
FIND_PHASE_SOM: Final[str] = "som"
FIND_PHASE_SERIALIZE: Final[str] = "serialize"


class _RawElementDict(TypedDict, total=False):
    type: str
    content: str
    bbox: list[float]
    interactivity: bool


@dataclass(frozen=True)
class OmniParserLoadResult:
    device: str
    weights_dir: str
    load_seconds: float


@dataclass(frozen=True)
class OmniParserReadiness:
    state: OmniParserState
    message: str
    device: str
    weights_dir: str
    phase: str
    load_seconds: float

    def to_status_payload(self) -> OmniParserStatusPayload:
        return OmniParserStatusPayload(
            state=self.state,
            message=self.message,
            device=self.device,
            weights_dir=self.weights_dir,
            phase=self.phase,
            load_seconds=self.load_seconds,
        )


@dataclass(frozen=True)
class OmniParserWeights:
    weights_dir: str
    som_model_path: Path
    caption_model_path: Path


@dataclass(frozen=True)
class _PhaseTimings:
    ocr_seconds: float
    som_seconds: float


def detect_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _ensure_omniparser_on_path() -> None:
    omniparser_root = str(Path(__file__).resolve().parent / "omniparser")
    if omniparser_root not in sys.path:
        sys.path.insert(0, omniparser_root)


def resolve_omniparser_weights(weights_dir: str) -> OmniParserWeights:
    """Locate the detector and caption weights, failing loudly when absent.

    A missing weights path is not just a normal file-not-found: the vendored
    YOLO loader treats an unrecognized local path as a Hugging Face Hub model
    ID and silently starts downloading it instead of raising.
    """
    som_model_path = Path(weights_dir) / "icon_detect" / "model.pt"
    caption_model_path = Path(weights_dir) / "icon_caption_florence"
    if not som_model_path.is_file() or not caption_model_path.is_dir():
        raise FileNotFoundError(
            f"OmniParser weights not found at {weights_dir!r} "
            f"(expected {som_model_path} and {caption_model_path}). "
            "Run controller_client/scripts/setup.sh (or the weights "
            "download step) before using find_element."
        )
    return OmniParserWeights(
        weights_dir=weights_dir,
        som_model_path=som_model_path,
        caption_model_path=caption_model_path,
    )


def _import_omniparser_class() -> Any:
    _ensure_omniparser_on_path()
    # Deferred: util.omniparser only resolves once _ensure_omniparser_on_path()
    # has inserted the vendored package root onto sys.path. Whether mypy
    # itself can statically resolve "util" varies by environment, so the
    # ignore also tolerates being unused rather than failing either way.
    from util.omniparser import Omniparser  # type: ignore[import-not-found, unused-ignore]  # isort: skip

    return Omniparser


def _build_model_config(weights: OmniParserWeights, device: str) -> dict[str, object]:
    return {
        "som_model_path": str(weights.som_model_path),
        "caption_model_name": "florence2",
        "caption_model_path": str(weights.caption_model_path),
        "BOX_TRESHOLD": omniparser_box_threshold(),
        "device": device,
    }


def _build_draw_config(image_size: tuple[int, ...]) -> dict[str, float | int]:
    box_overlay_ratio: float = max(image_size) / BOX_OVERLAY_DIVISOR
    return {
        "text_scale": 0.8 * box_overlay_ratio,
        "text_thickness": max(int(2 * box_overlay_ratio), 1),
        "text_padding": max(int(3 * box_overlay_ratio), 1),
        "thickness": max(int(3 * box_overlay_ratio), 1),
    }


def _run_ocr(image: Any) -> tuple[Any, Any]:
    # Deferred for the same sys.path reason as _import_omniparser_class.
    from util.utils import check_ocr_box  # type: ignore[import-not-found, unused-ignore]  # isort: skip

    (text, ocr_bbox), _ = check_ocr_box(
        image,
        display_img=False,
        output_bb_format="xyxy",
        easyocr_args={"text_threshold": OCR_TEXT_THRESHOLD},
        use_paddleocr=False,
    )
    return text, ocr_bbox


def _run_som_labeling(
    image: Any,
    som_model: Any,
    caption_model_processor: Any,
    ocr_text: Any,
    ocr_bbox: Any,
    draw_config: dict[str, float | int],
    box_threshold: float,
    iou_threshold: float,
) -> tuple[str, list[_RawElementDict]]:
    # Deferred for the same sys.path reason as _import_omniparser_class.
    from util.utils import get_som_labeled_img  # type: ignore[import-not-found, unused-ignore]  # isort: skip

    annotated_img, _label_coords, parsed_content_list = get_som_labeled_img(
        image,
        som_model,
        BOX_TRESHOLD=box_threshold,
        output_coord_in_ratio=True,
        ocr_bbox=ocr_bbox,
        draw_bbox_config=draw_config,
        caption_model_processor=caption_model_processor,
        ocr_text=ocr_text,
        use_local_semantics=True,
        iou_threshold=iou_threshold,
        scale_img=False,
        batch_size=omniparser_caption_batch_size(),
    )
    return annotated_img, parsed_content_list


def _to_pixel_element(
    index: int, raw: _RawElementDict, width: int, height: int
) -> PixelElementPayload:
    bbox_raw = raw["bbox"]
    return PixelElementPayload(
        index=index,
        type=raw.get("type", "unknown"),
        content=raw.get("content", ""),
        bbox=PixelBBoxPayload(
            x_min=round(bbox_raw[0] * width),
            y_min=round(bbox_raw[1] * height),
            x_max=round(bbox_raw[2] * width),
            y_max=round(bbox_raw[3] * height),
        ),
        center_x=round((bbox_raw[0] + bbox_raw[2]) / 2 * width),
        center_y=round((bbox_raw[1] + bbox_raw[3]) / 2 * height),
        interactivity=bool(raw.get("interactivity", False)),
    )


def measure_result_message_bytes(result: FindElementResultPayload) -> int:
    message = serialize_find_element_result(_SIZE_PROBE_REQUEST_ID, result)
    return len(message.encode("utf-8"))


def _decode_png_base64(image_base64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(image_base64)))


def _encode_png_base64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def downscale_png_base64(image_base64: str, factor: float) -> str:
    image = _decode_png_base64(image_base64)
    width, height = image.size
    new_size = (max(1, round(width * factor)), max(1, round(height * factor)))
    return _encode_png_base64(image.resize(new_size, Image.Resampling.LANCZOS))


def _annotated_long_edge(result: FindElementResultPayload) -> int:
    return max(_decode_png_base64(result.annotated_image_base64).size)


def bound_find_element_result(
    result: FindElementResultPayload, max_bytes: int
) -> FindElementResultPayload:
    """Shrink the annotated image until the serialized message fits max_bytes.

    Only the annotated PNG changes; ``elements``, ``image_width`` and
    ``image_height`` stay in the original screenshot's pixel coordinates
    because the annotated image is only used for visual matching.
    """
    size = measure_result_message_bytes(result)
    while size > max_bytes:
        long_edge = _annotated_long_edge(result)
        if long_edge * DOWNSCALE_FACTOR < MIN_ANNOTATED_LONG_EDGE:
            raise ExecutionError(
                f"find_element result is {size} bytes, above the {max_bytes}-byte "
                f"limit even with the annotated image downscaled to a {long_edge}px "
                f"long edge. Raise {MAX_RESULT_BYTES_ENV_VAR} in "
                "controller_client/.env (keeping it below the server's WebSocket "
                "message limit) or lower the screen resolution."
            )
        result = dataclasses.replace(
            result,
            annotated_image_base64=downscale_png_base64(
                result.annotated_image_base64, DOWNSCALE_FACTOR
            ),
        )
        size = measure_result_message_bytes(result)
        logger.info(
            "Downscaled annotated image to fit the result limit: "
            "message_bytes=%d max_bytes=%d",
            size,
            max_bytes,
        )
    return result


def _not_started_readiness() -> OmniParserReadiness:
    return OmniParserReadiness(
        state=OmniParserState.LOADING,
        message="OmniParser models have not started loading",
        device="",
        weights_dir="",
        phase=LOAD_PHASE_NOT_STARTED,
        load_seconds=0.0,
    )


def _describe_load_failure(phase: str, error: Exception, weights_dir: str) -> str:
    if phase == LOAD_PHASE_IMPORTS and isinstance(error, ImportError):
        missing = error.name or "an OmniParser dependency"
        return (
            f"OmniParser dependency import failed: {missing!r} is not installed "
            f"({error}). Re-run controller_client/scripts/setup.sh (or setup.ps1 / "
            "setup.bat) to install the controller's dependencies."
        )
    if phase == LOAD_PHASE_MODEL_LOAD:
        return f"Loading OmniParser models from {weights_dir!r} failed: {error}"
    return str(error)


class _OmniParserModel:
    """Process-wide holder of the loaded OmniParser models and their readiness.

    ``_model_lock`` serializes loading and inference (the models are not
    thread-safe); ``_state_lock`` guards the readiness snapshot so callers can
    read it while a load is in progress.
    """

    def __init__(self) -> None:
        self._model_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._parser: Any = None
        self._load_result: OmniParserLoadResult | None = None
        self._failure: OmniParserError | None = None
        self._readiness = _not_started_readiness()

    def reset(self) -> None:
        with self._model_lock, self._state_lock:
            self._parser = None
            self._load_result = None
            self._failure = None
            self._readiness = _not_started_readiness()

    def readiness(self) -> OmniParserReadiness:
        with self._state_lock:
            return self._readiness

    def _set_loading(self, phase: str, device: str, weights_dir: str) -> None:
        with self._state_lock:
            self._readiness = OmniParserReadiness(
                state=OmniParserState.LOADING,
                message=f"Loading OmniParser models ({phase})",
                device=device,
                weights_dir=weights_dir,
                phase=phase,
                load_seconds=0.0,
            )

    def _record_failure(self, error: OmniParserError) -> None:
        with self._state_lock:
            self._failure = error
            self._readiness = OmniParserReadiness(
                state=OmniParserState.FAILED,
                message=str(error),
                device=error.device,
                weights_dir=error.weights_dir,
                phase=error.phase,
                load_seconds=0.0,
            )

    def _record_ready(self, parser: Any, result: OmniParserLoadResult) -> None:
        with self._state_lock:
            self._parser = parser
            self._load_result = result
            self._failure = None
            self._readiness = OmniParserReadiness(
                state=OmniParserState.READY,
                message=f"OmniParser ready on {result.device}",
                device=result.device,
                weights_dir=result.weights_dir,
                phase=LOAD_PHASE_MODEL_LOAD,
                load_seconds=result.load_seconds,
            )

    def load(self) -> OmniParserLoadResult:
        with self._model_lock:
            if self._load_result is not None:
                return self._load_result
            return self._load_locked()

    def _load_locked(self) -> OmniParserLoadResult:
        weights_dir = omniparser_weights_dir()
        device = ""
        phase = LOAD_PHASE_WEIGHTS
        self._set_loading(phase, device, weights_dir)
        started = time.monotonic()
        try:
            weights = resolve_omniparser_weights(weights_dir)
            phase = LOAD_PHASE_IMPORTS
            self._set_loading(phase, device, weights_dir)
            omniparser_class = _import_omniparser_class()
            phase = LOAD_PHASE_DEVICE
            self._set_loading(phase, device, weights_dir)
            device = detect_device()
            phase = LOAD_PHASE_MODEL_LOAD
            self._set_loading(phase, device, weights_dir)
            parser = omniparser_class(_build_model_config(weights, device))
        except Exception as e:
            error = OmniParserError(
                _describe_load_failure(phase, e, weights_dir),
                phase=phase,
                device=device,
                weights_dir=weights_dir,
                code=ErrorCode.OMNIPARSER_NOT_READY.value,
            )
            self._record_failure(error)
            logger.error("OmniParser load failed (%s): %s", error.details(), error)
            raise error from e

        result = OmniParserLoadResult(
            device=device,
            weights_dir=weights_dir,
            load_seconds=time.monotonic() - started,
        )
        self._record_ready(parser, result)
        logger.info(
            "OmniParser models loaded from %s (device=%s) in %.1fs",
            weights_dir,
            device,
            result.load_seconds,
        )
        return result

    def find_element(self, payload: FindElementPayload) -> FindElementResultPayload:
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise _not_ready_error(failure)

        load_result = self.load()
        total_started = time.monotonic()
        screenshot = _find_phase(FIND_PHASE_SCREENSHOT, load_result, execute_screenshot)
        with self._model_lock:
            try:
                result, timings = self._parse_locked(screenshot, payload, load_result)
            finally:
                gc.collect()
                if load_result.device == "cuda":
                    torch.cuda.empty_cache()

        bounded = _find_phase(
            FIND_PHASE_SERIALIZE,
            load_result,
            lambda: bound_find_element_result(result, omniparser_max_result_bytes()),
        )
        _log_measurements(
            screenshot, bounded, timings, time.monotonic() - total_started
        )
        return bounded

    def _parse_locked(
        self,
        screenshot: ScreenshotResponsePayload,
        payload: FindElementPayload,
        load_result: OmniParserLoadResult,
    ) -> tuple[FindElementResultPayload, _PhaseTimings]:
        image = _decode_png_base64(screenshot.image_base64)
        width, height = image.size
        draw_config = _build_draw_config(image.size)
        effective_box = (
            payload.box_threshold
            if payload.box_threshold is not None
            else omniparser_box_threshold()
        )
        effective_iou = (
            payload.iou_threshold
            if payload.iou_threshold is not None
            else omniparser_iou_threshold()
        )

        ocr_started = time.monotonic()
        ocr_text, ocr_bbox = _find_phase(
            FIND_PHASE_OCR, load_result, lambda: _run_ocr(image)
        )
        ocr_seconds = time.monotonic() - ocr_started

        som_started = time.monotonic()
        annotated_img, parsed_content_list = _find_phase(
            FIND_PHASE_SOM,
            load_result,
            lambda: _run_som_labeling(
                image=image,
                som_model=self._parser.som_model,
                caption_model_processor=self._parser.caption_model_processor,
                ocr_text=ocr_text,
                ocr_bbox=ocr_bbox,
                draw_config=draw_config,
                box_threshold=effective_box,
                iou_threshold=effective_iou,
            ),
        )
        som_seconds = time.monotonic() - som_started

        elements = tuple(
            _to_pixel_element(i, raw, width, height)
            for i, raw in enumerate(parsed_content_list)
        )
        result = FindElementResultPayload(
            success=True,
            annotated_image_base64=annotated_img,
            elements=elements,
            image_width=width,
            image_height=height,
        )
        return result, _PhaseTimings(ocr_seconds=ocr_seconds, som_seconds=som_seconds)


def _not_ready_error(failure: OmniParserError) -> OmniParserError:
    return OmniParserError(
        f"OmniParser is not ready: {failure}. Fix the reported problem and "
        "restart the controller client.",
        phase=failure.phase,
        device=failure.device,
        weights_dir=failure.weights_dir,
        code=ErrorCode.OMNIPARSER_NOT_READY.value,
    )


def _find_phase(
    phase: str, load_result: OmniParserLoadResult, step: Callable[[], T]
) -> T:
    try:
        return step()
    except OmniParserError:
        raise
    except Exception as e:
        raise OmniParserError(
            f"Find element failed during {phase}: {e}",
            phase=phase,
            device=load_result.device,
            weights_dir=load_result.weights_dir,
            code=ErrorCode.FIND_ELEMENT_FAILED.value,
        ) from e


def _log_measurements(
    screenshot: ScreenshotResponsePayload,
    result: FindElementResultPayload,
    timings: _PhaseTimings,
    total_seconds: float,
) -> None:
    elements_json = json.dumps(
        [dataclasses.asdict(element) for element in result.elements]
    )
    logger.info(
        "find_element measurements: image=%dx%d screenshot_bytes=%d "
        "annotated_png_bytes=%d annotated_base64_len=%d elements=%d "
        "elements_json_len=%d message_bytes=%d ocr_seconds=%.2f "
        "som_seconds=%.2f total_seconds=%.2f",
        result.image_width,
        result.image_height,
        len(base64.b64decode(screenshot.image_base64)),
        len(base64.b64decode(result.annotated_image_base64)),
        len(result.annotated_image_base64),
        len(result.elements),
        len(elements_json),
        measure_result_message_bytes(result),
        timings.ocr_seconds,
        timings.som_seconds,
        total_seconds,
    )


_model = _OmniParserModel()


def load_omniparser_model() -> OmniParserLoadResult:
    return _model.load()


def get_omniparser_readiness() -> OmniParserReadiness:
    return _model.readiness()


def reset_omniparser_model() -> None:
    """Forget any loaded models and failures; intended for tests."""
    _model.reset()


def execute_find_element(payload: FindElementPayload) -> FindElementResultPayload:
    return _model.find_element(payload)
