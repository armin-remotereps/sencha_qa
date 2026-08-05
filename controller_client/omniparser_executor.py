from __future__ import annotations

import base64
import gc
import io
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Final, TypedDict

import torch
from PIL import Image

from controller_client.exceptions import ExecutionError
from controller_client.executor import execute_screenshot
from controller_client.omniparser_config import (
    omniparser_box_threshold,
    omniparser_caption_batch_size,
    omniparser_iou_threshold,
    omniparser_weights_dir,
)
from controller_client.protocol import (
    FindElementPayload,
    FindElementResultPayload,
    PixelBBoxPayload,
    PixelElementPayload,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()

BOX_OVERLAY_DIVISOR: Final[int] = 3200
OCR_TEXT_THRESHOLD: Final[float] = 0.8


class _RawElementDict(TypedDict, total=False):
    type: str
    content: str
    bbox: list[float]
    interactivity: bool


def _detect_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _ensure_omniparser_on_path() -> None:
    omniparser_root = str(Path(__file__).resolve().parent / "omniparser")
    if omniparser_root not in sys.path:
        sys.path.insert(0, omniparser_root)


def _build_draw_config(image_size: tuple[int, ...]) -> dict[str, float | int]:
    box_overlay_ratio: float = max(image_size) / BOX_OVERLAY_DIVISOR
    return {
        "text_scale": 0.8 * box_overlay_ratio,
        "text_thickness": max(int(2 * box_overlay_ratio), 1),
        "text_padding": max(int(3 * box_overlay_ratio), 1),
        "thickness": max(int(3 * box_overlay_ratio), 1),
    }


def _run_ocr(image: Any) -> tuple[Any, Any]:
    # Deferred: util.utils only resolves once _ensure_omniparser_on_path()
    # has inserted the vendored package root onto sys.path. Whether mypy
    # itself can statically resolve "util" varies by environment, so the
    # ignore also tolerates being unused rather than failing either way.
    from util.utils import check_ocr_box  # type: ignore[import-not-found, unused-ignore]

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
    # Deferred for the same sys.path reason as _run_ocr above.
    from util.utils import get_som_labeled_img  # type: ignore[import-not-found, unused-ignore]

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


class _OmniParserModel:
    _instance: _OmniParserModel | None = None
    _parser: Any = None
    _device: str = "cpu"

    def __new__(cls) -> _OmniParserModel:
        if cls._instance is None:
            with _lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def _load(self) -> None:
        if self._parser is not None:
            return
        with _lock:
            if self._parser is not None:
                return
            _ensure_omniparser_on_path()
            # Deferred for the same sys.path reason as _run_ocr above.
            from util.omniparser import Omniparser  # type: ignore[import-not-found, unused-ignore]

            self._device = _detect_device()
            weights_dir = omniparser_weights_dir()
            config = {
                "som_model_path": str(Path(weights_dir) / "icon_detect" / "model.pt"),
                "caption_model_name": "florence2",
                "caption_model_path": str(
                    Path(weights_dir) / "icon_caption_florence"
                ),
                "BOX_TRESHOLD": omniparser_box_threshold(),
                "device": self._device,
            }
            self._parser = Omniparser(config)
            logger.info(
                "OmniParser models loaded from %s (device=%s)",
                weights_dir,
                self._device,
            )

    def parse(
        self,
        image_base64: str,
        box_threshold: float | None,
        iou_threshold: float | None,
    ) -> FindElementResultPayload:
        self._load()

        with _lock:
            try:
                image_bytes = base64.b64decode(image_base64)
                image = Image.open(io.BytesIO(image_bytes))
                width, height = image.size
                draw_config = _build_draw_config(image.size)
                effective_box = (
                    box_threshold
                    if box_threshold is not None
                    else omniparser_box_threshold()
                )
                effective_iou = (
                    iou_threshold
                    if iou_threshold is not None
                    else omniparser_iou_threshold()
                )
                ocr_text, ocr_bbox = _run_ocr(image)

                annotated_img, parsed_content_list = _run_som_labeling(
                    image=image,
                    som_model=self._parser.som_model,
                    caption_model_processor=self._parser.caption_model_processor,
                    ocr_text=ocr_text,
                    ocr_bbox=ocr_bbox,
                    draw_config=draw_config,
                    box_threshold=effective_box,
                    iou_threshold=effective_iou,
                )

                elements = tuple(
                    _to_pixel_element(i, raw, width, height)
                    for i, raw in enumerate(parsed_content_list)
                )

                return FindElementResultPayload(
                    success=True,
                    annotated_image_base64=annotated_img,
                    elements=elements,
                    image_width=width,
                    image_height=height,
                )
            finally:
                gc.collect()
                if self._device == "cuda":
                    torch.cuda.empty_cache()


_model = _OmniParserModel()


def execute_find_element(payload: FindElementPayload) -> FindElementResultPayload:
    try:
        screenshot = execute_screenshot()
    except ExecutionError as e:
        raise ExecutionError(f"Find element failed: {e}") from e

    try:
        return _model.parse(
            screenshot.image_base64, payload.box_threshold, payload.iou_threshold
        )
    except Exception as e:
        raise ExecutionError(f"Find element failed: {e}") from e
