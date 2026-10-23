"""OmniParser inference for the vision worker.

The server sends a PNG screenshot; the engine runs OCR and Set-of-Marks
labelling on it and returns the elements in that image's pixel coordinates
plus an annotated copy. Images stay in memory: nothing is written to disk.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import gc
import io
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

from vision_worker.config import DevicePreference, EngineConfig
from vision_worker.exceptions import EngineError
from vision_worker.protocol import (
    ParseRequestPayload,
    ParseResultPayload,
    PixelBBoxPayload,
    PixelElementPayload,
    StatusPayload,
    WorkerState,
    serialize_parse_result,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

BOX_OVERLAY_DIVISOR: Final[int] = 3200
OCR_TEXT_THRESHOLD: Final[float] = 0.8
CAPTION_MODEL_NAME: Final[str] = "florence2"
_BYTES_PER_MIB: Final[int] = 1024 * 1024

# Downscaling the annotated image below this long edge makes the numbered
# labels unreadable for the vision model, so shrinking stops there.
MIN_ANNOTATED_LONG_EDGE: Final[int] = 1280
DOWNSCALE_FACTOR: Final[float] = 0.75
MAX_RESULT_BYTES_ENV_VAR: Final[str] = "VISION_MAX_RESULT_BYTES"
# Same length as the uuid4 request ids the server sends, so the measured
# size matches the message that actually goes over the wire.
_SIZE_PROBE_REQUEST_ID: Final[str] = "00000000-0000-0000-0000-000000000000"

LOAD_PHASE_NOT_STARTED: Final[str] = "not_started"
LOAD_PHASE_WEIGHTS: Final[str] = "weights"
LOAD_PHASE_IMPORTS: Final[str] = "imports"
LOAD_PHASE_DEVICE: Final[str] = "device"
LOAD_PHASE_MODEL_LOAD: Final[str] = "model_load"
PARSE_PHASE_DECODE: Final[str] = "decode"
PARSE_PHASE_OCR: Final[str] = "ocr"
PARSE_PHASE_SOM: Final[str] = "som"
PARSE_PHASE_SERIALIZE: Final[str] = "serialize"

SETUP_HINT: Final[str] = (
    "Re-run vision_worker/scripts/setup.sh (or setup.ps1) to install the "
    "vision worker's dependencies."
)
WEIGHTS_HINT: Final[str] = (
    "Re-run vision_worker/scripts/setup.sh (or setup.ps1) to download the "
    "weights, or point OMNIPARSER_WEIGHTS_DIR at an existing weights folder."
)


class _RawElementDict(TypedDict, total=False):
    type: str
    content: str
    bbox: list[float]
    interactivity: bool


@dataclass(frozen=True)
class LoadResult:
    device: str
    weights_dir: str
    load_seconds: float


@dataclass(frozen=True)
class Readiness:
    state: WorkerState
    message: str
    device: str
    weights_dir: str
    phase: str
    load_seconds: float

    def to_status_payload(self) -> StatusPayload:
        return StatusPayload(
            state=self.state,
            message=self.message,
            device=self.device,
            phase=self.phase,
            load_seconds=self.load_seconds,
        )


@dataclass(frozen=True)
class OmniParserWeights:
    weights_dir: str
    som_model_path: Path
    caption_model_path: Path


@dataclass(frozen=True)
class GpuInfo:
    name: str
    vram_mb: int


@dataclass(frozen=True)
class _Models:
    som_model: Any
    caption_model_processor: Any


@dataclass(frozen=True)
class _PhaseTimings:
    ocr_seconds: float
    som_seconds: float


def detect_device(preference: DevicePreference) -> str:
    """Resolve VISION_DEVICE to a torch device name, failing when it is absent.

    Pick one GPU with CUDA_VISIBLE_DEVICES (one worker process per GPU):
    ``cuda:N`` is not supported because easyocr always uses the default device.
    """
    cuda = torch.cuda.is_available()
    mps = torch.backends.mps.is_available()
    if preference is DevicePreference.AUTO:
        if cuda:
            return DevicePreference.CUDA.value
        if mps:
            return DevicePreference.MPS.value
        return DevicePreference.CPU.value
    if preference is DevicePreference.CUDA and not cuda:
        raise RuntimeError(
            "VISION_DEVICE=cuda but torch sees no CUDA device. Check `nvidia-smi`, "
            "and that torch was installed from the CUDA wheel index."
        )
    if preference is DevicePreference.MPS and not mps:
        raise RuntimeError("VISION_DEVICE=mps but torch sees no MPS device")
    if preference is DevicePreference.CPU and cuda:
        logger.warning(
            "VISION_DEVICE=cpu on a machine with CUDA: the caption model runs on "
            "the CPU, but OCR and the detector still use the GPU. Set "
            "CUDA_VISIBLE_DEVICES= (empty) to keep everything on the CPU."
        )
    return preference.value


def gpu_info(device: str) -> GpuInfo | None:
    if device != DevicePreference.CUDA.value or not torch.cuda.is_available():
        return None
    properties = torch.cuda.get_device_properties(0)
    return GpuInfo(
        name=torch.cuda.get_device_name(0),
        vram_mb=int(properties.total_memory) // _BYTES_PER_MIB,
    )


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
            f"(expected {som_model_path} and {caption_model_path}). {WEIGHTS_HINT}"
        )
    return OmniParserWeights(
        weights_dir=weights_dir,
        som_model_path=som_model_path,
        caption_model_path=caption_model_path,
    )


def _import_model_loaders() -> tuple[Any, Any]:
    _ensure_omniparser_on_path()
    # Deferred: util.utils only resolves once _ensure_omniparser_on_path()
    # has inserted the vendored package root onto sys.path, and importing it
    # builds the OCR readers. Whether mypy itself can statically resolve
    # "util" varies by environment, so the ignore also tolerates being unused.
    from util.utils import get_caption_model_processor, get_yolo_model  # type: ignore[import-not-found, unused-ignore]  # isort: skip

    return get_yolo_model, get_caption_model_processor


def _build_draw_config(image_size: tuple[int, ...]) -> dict[str, float | int]:
    box_overlay_ratio: float = max(image_size) / BOX_OVERLAY_DIVISOR
    return {
        "text_scale": 0.8 * box_overlay_ratio,
        "text_thickness": max(int(2 * box_overlay_ratio), 1),
        "text_padding": max(int(3 * box_overlay_ratio), 1),
        "thickness": max(int(3 * box_overlay_ratio), 1),
    }


def _run_ocr(image: Any) -> tuple[Any, Any]:
    # Deferred for the same sys.path reason as _import_model_loaders.
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
    models: _Models,
    ocr_text: Any,
    ocr_bbox: Any,
    draw_config: dict[str, float | int],
    box_threshold: float,
    iou_threshold: float,
    batch_size: int,
) -> tuple[str, list[_RawElementDict]]:
    # Deferred for the same sys.path reason as _import_model_loaders.
    from util.utils import get_som_labeled_img  # type: ignore[import-not-found, unused-ignore]  # isort: skip

    annotated_img, _label_coords, parsed_content_list = get_som_labeled_img(
        image,
        models.som_model,
        BOX_TRESHOLD=box_threshold,
        output_coord_in_ratio=True,
        ocr_bbox=ocr_bbox,
        draw_bbox_config=draw_config,
        caption_model_processor=models.caption_model_processor,
        ocr_text=ocr_text,
        use_local_semantics=True,
        iou_threshold=iou_threshold,
        scale_img=False,
        batch_size=batch_size,
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


def measure_result_message_bytes(result: ParseResultPayload) -> int:
    message = serialize_parse_result(_SIZE_PROBE_REQUEST_ID, result)
    return len(message.encode("utf-8"))


def decode_image_base64(image_base64: str) -> Image.Image:
    """Decode the base64 image the server sent, fully, in memory."""
    try:
        raw = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"image is not valid base64: {e}") from e
    image = Image.open(io.BytesIO(raw))
    image.load()
    return image


def _encode_png_base64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def downscale_png_base64(image_base64: str, factor: float) -> str:
    image = decode_image_base64(image_base64)
    width, height = image.size
    new_size = (max(1, round(width * factor)), max(1, round(height * factor)))
    return _encode_png_base64(image.resize(new_size, Image.Resampling.LANCZOS))


def _annotated_long_edge(result: ParseResultPayload) -> int:
    return max(decode_image_base64(result.annotated_image_base64).size)


def bound_result(result: ParseResultPayload, max_bytes: int) -> ParseResultPayload:
    """Shrink the annotated image until the serialized message fits max_bytes.

    Only the annotated PNG changes; ``elements``, ``image_width`` and
    ``image_height`` stay in the received image's pixel coordinates because
    the annotated image is only used for visual matching.
    """
    size = measure_result_message_bytes(result)
    while size > max_bytes:
        long_edge = _annotated_long_edge(result)
        if long_edge * DOWNSCALE_FACTOR < MIN_ANNOTATED_LONG_EDGE:
            raise ValueError(
                f"parse result is {size} bytes, above the {max_bytes}-byte limit "
                f"even with the annotated image downscaled to a {long_edge}px long "
                f"edge. Raise {MAX_RESULT_BYTES_ENV_VAR} in the worker's .env "
                "(keeping it below the server's WebSocket message limit)."
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


def _not_started_readiness() -> Readiness:
    return Readiness(
        state=WorkerState.LOADING,
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
            f"({error}). {SETUP_HINT}"
        )
    if phase == LOAD_PHASE_MODEL_LOAD:
        return f"Loading OmniParser models from {weights_dir!r} failed: {error}"
    return str(error)


class VisionEngine:
    """Holder of the loaded OmniParser models and their readiness.

    ``_model_lock`` serializes loading and inference (the models are not
    thread-safe); ``_state_lock`` guards the readiness snapshot so callers can
    read it while a load is in progress.
    """

    def __init__(self, config: EngineConfig) -> None:
        self._config = config
        self._model_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._models: _Models | None = None
        self._load_result: LoadResult | None = None
        self._failure: EngineError | None = None
        self._readiness = _not_started_readiness()

    @property
    def config(self) -> EngineConfig:
        return self._config

    def readiness(self) -> Readiness:
        with self._state_lock:
            return self._readiness

    def _set_loading(self, phase: str, device: str, weights_dir: str) -> None:
        with self._state_lock:
            self._readiness = Readiness(
                state=WorkerState.LOADING,
                message=f"Loading OmniParser models ({phase})",
                device=device,
                weights_dir=weights_dir,
                phase=phase,
                load_seconds=0.0,
            )

    def _record_failure(self, error: EngineError) -> None:
        with self._state_lock:
            self._failure = error
            self._readiness = Readiness(
                state=WorkerState.FAILED,
                message=str(error),
                device=error.device,
                weights_dir=error.weights_dir,
                phase=error.phase,
                load_seconds=0.0,
            )

    def _record_ready(self, models: _Models, result: LoadResult) -> None:
        with self._state_lock:
            self._models = models
            self._load_result = result
            self._failure = None
            self._readiness = Readiness(
                state=WorkerState.READY,
                message=f"OmniParser ready on {result.device}",
                device=result.device,
                weights_dir=result.weights_dir,
                phase=LOAD_PHASE_MODEL_LOAD,
                load_seconds=result.load_seconds,
            )

    def load(self) -> LoadResult:
        """Load the models once; a failed load is retried on the next call."""
        with self._model_lock:
            if self._load_result is not None:
                return self._load_result
            return self._load_locked()

    def _load_locked(self) -> LoadResult:
        weights_dir = self._config.weights_dir
        device = ""
        phase = LOAD_PHASE_WEIGHTS
        self._set_loading(phase, device, weights_dir)
        started = time.monotonic()
        try:
            weights = resolve_omniparser_weights(weights_dir)
            phase = LOAD_PHASE_IMPORTS
            self._set_loading(phase, device, weights_dir)
            get_yolo_model, get_caption_model_processor = _import_model_loaders()
            phase = LOAD_PHASE_DEVICE
            self._set_loading(phase, device, weights_dir)
            device = detect_device(self._config.device)
            phase = LOAD_PHASE_MODEL_LOAD
            self._set_loading(phase, device, weights_dir)
            # Built here rather than through the vendored Omniparser class,
            # which ignores the configured device for the caption model.
            models = _Models(
                som_model=get_yolo_model(model_path=str(weights.som_model_path)),
                caption_model_processor=get_caption_model_processor(
                    model_name=CAPTION_MODEL_NAME,
                    model_name_or_path=str(weights.caption_model_path),
                    device=device,
                ),
            )
        except Exception as e:
            error = EngineError(
                _describe_load_failure(phase, e, weights_dir),
                phase=phase,
                device=device,
                weights_dir=weights_dir,
            )
            self._record_failure(error)
            logger.error("OmniParser load failed (%s): %s", error.details(), error)
            raise error from e

        result = LoadResult(
            device=device,
            weights_dir=weights_dir,
            load_seconds=time.monotonic() - started,
        )
        self._record_ready(models, result)
        logger.info(
            "OmniParser models loaded from %s (device=%s) in %.1fs",
            weights_dir,
            device,
            result.load_seconds,
        )
        return result

    def parse(
        self, payload: ParseRequestPayload, *, queue_seconds: float = 0.0
    ) -> ParseResultPayload:
        """Parse the image in ``payload``; blocking, run it off the event loop."""
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise _not_ready_error(failure)

        load_result = self.load()
        total_started = time.monotonic()
        image = _phase(
            PARSE_PHASE_DECODE,
            load_result,
            lambda: decode_image_base64(payload.image_base64),
        )
        input_bytes = len(payload.image_base64)
        with self._model_lock:
            try:
                result, timings = self._parse_locked(
                    image, payload, load_result, queue_seconds
                )
            finally:
                del image
                gc.collect()
                if load_result.device == DevicePreference.CUDA.value:
                    torch.cuda.empty_cache()

        bounded = _phase(
            PARSE_PHASE_SERIALIZE,
            load_result,
            lambda: bound_result(result, self._config.max_result_bytes),
        )
        _log_measurements(
            input_bytes, bounded, timings, time.monotonic() - total_started
        )
        return bounded

    def _parse_locked(
        self,
        image: Image.Image,
        payload: ParseRequestPayload,
        load_result: LoadResult,
        queue_seconds: float,
    ) -> tuple[ParseResultPayload, _PhaseTimings]:
        models = self._models
        if models is None:  # load() returned, so the models are set
            raise EngineError(
                "OmniParser models are not loaded",
                phase=LOAD_PHASE_MODEL_LOAD,
                device=load_result.device,
                weights_dir=load_result.weights_dir,
            )
        width, height = image.size
        draw_config = _build_draw_config(image.size)
        effective_box = (
            payload.box_threshold
            if payload.box_threshold is not None
            else self._config.box_threshold
        )
        effective_iou = (
            payload.iou_threshold
            if payload.iou_threshold is not None
            else self._config.iou_threshold
        )

        ocr_started = time.monotonic()
        ocr_text, ocr_bbox = _phase(
            PARSE_PHASE_OCR, load_result, lambda: _run_ocr(image)
        )
        ocr_seconds = time.monotonic() - ocr_started

        som_started = time.monotonic()
        annotated_img, parsed_content_list = _phase(
            PARSE_PHASE_SOM,
            load_result,
            lambda: _run_som_labeling(
                image=image,
                models=models,
                ocr_text=ocr_text,
                ocr_bbox=ocr_bbox,
                draw_config=draw_config,
                box_threshold=effective_box,
                iou_threshold=effective_iou,
                batch_size=self._config.caption_batch_size,
            ),
        )
        som_seconds = time.monotonic() - som_started

        elements = tuple(
            _to_pixel_element(i, raw, width, height)
            for i, raw in enumerate(parsed_content_list)
        )
        result = ParseResultPayload(
            annotated_image_base64=annotated_img,
            elements=elements,
            image_width=width,
            image_height=height,
            ocr_seconds=ocr_seconds,
            som_seconds=som_seconds,
            queue_seconds=queue_seconds,
        )
        return result, _PhaseTimings(ocr_seconds=ocr_seconds, som_seconds=som_seconds)


def _not_ready_error(failure: EngineError) -> EngineError:
    return EngineError(
        f"OmniParser is not ready: {failure}. Fix the reported problem and "
        "restart the vision worker.",
        phase=failure.phase,
        device=failure.device,
        weights_dir=failure.weights_dir,
    )


def _phase(phase: str, load_result: LoadResult, step: Callable[[], T]) -> T:
    try:
        return step()
    except EngineError:
        raise
    except Exception as e:
        raise EngineError(
            f"Parse failed during {phase}: {e}",
            phase=phase,
            device=load_result.device,
            weights_dir=load_result.weights_dir,
        ) from e


def _log_measurements(
    input_base64_len: int,
    result: ParseResultPayload,
    timings: _PhaseTimings,
    total_seconds: float,
) -> None:
    # Sizes and timings only: image data never reaches the log.
    logger.info(
        "parse measurements: image=%dx%d input_base64_len=%d "
        "annotated_base64_len=%d elements=%d "
        "queue_seconds=%.2f ocr_seconds=%.2f som_seconds=%.2f total_seconds=%.2f",
        result.image_width,
        result.image_height,
        input_base64_len,
        len(result.annotated_image_base64),
        len(result.elements),
        result.queue_seconds,
        timings.ocr_seconds,
        timings.som_seconds,
        total_seconds,
    )
