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

from omniparser_service.config import settings
from omniparser_service.types import (
    BBox,
    ParseResult,
    PixelBBox,
    PixelParseResult,
    PixelUIElement,
    UIElement,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()

BOX_OVERLAY_DIVISOR: Final[int] = 3200
OCR_TEXT_THRESHOLD: Final[float] = 0.8


class RawElementDict(TypedDict, total=False):
    type: str
    content: str
    bbox: list[float]
    interactivity: bool


def _ensure_omniparser_on_path() -> None:
    omniparser_root = str(Path(__file__).resolve().parent.parent / "OmniParser")
    if omniparser_root not in sys.path:
        sys.path.insert(0, omniparser_root)


def _build_element(index: int, raw: RawElementDict) -> UIElement:
    bbox_raw: list[float] = raw["bbox"]
    bbox = BBox(
        x_min=bbox_raw[0],
        y_min=bbox_raw[1],
        x_max=bbox_raw[2],
        y_max=bbox_raw[3],
    )
    return UIElement(
        index=index,
        type=raw.get("type", "unknown"),
        content=raw.get("content", ""),
        bbox=bbox,
        center_x=(bbox.x_min + bbox.x_max) / 2,
        center_y=(bbox.y_min + bbox.y_max) / 2,
        interactivity=bool(raw.get("interactivity", False)),
    )


def _to_pixel_element(element: UIElement, width: int, height: int) -> PixelUIElement:
    return PixelUIElement(
        index=element.index,
        type=element.type,
        content=element.content,
        bbox=PixelBBox(
            x_min=round(element.bbox.x_min * width),
            y_min=round(element.bbox.y_min * height),
            x_max=round(element.bbox.x_max * width),
            y_max=round(element.bbox.y_max * height),
        ),
        center_x=round(element.center_x * width),
        center_y=round(element.center_y * height),
        interactivity=element.interactivity,
    )


def _decode_image(image_base64: str) -> tuple[Image.Image, int, int]:
    image_bytes = base64.b64decode(image_base64)
    image = Image.open(io.BytesIO(image_bytes))
    width: int = image.size[0]
    height: int = image.size[1]
    return image, width, height


def _build_draw_config(image_size: tuple[int, ...]) -> dict[str, float | int]:
    box_overlay_ratio: float = max(image_size) / BOX_OVERLAY_DIVISOR
    return {
        "text_scale": 0.8 * box_overlay_ratio,
        "text_thickness": max(int(2 * box_overlay_ratio), 1),
        "text_padding": max(int(3 * box_overlay_ratio), 1),
        "thickness": max(int(3 * box_overlay_ratio), 1),
    }


def _resolve_thresholds(
    box_threshold: float | None,
    iou_threshold: float | None,
) -> tuple[float, float]:
    effective_box = (
        box_threshold if box_threshold is not None else settings.box_threshold
    )
    effective_iou = (
        iou_threshold if iou_threshold is not None else settings.iou_threshold
    )
    return effective_box, effective_iou


def _run_ocr(image: Any) -> tuple[Any, Any]:
    from util.utils import check_ocr_box  # type: ignore[import-not-found]

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
) -> tuple[str, list[RawElementDict]]:
    from util.utils import get_som_labeled_img

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
        batch_size=settings.caption_batch_size,
    )
    return annotated_img, parsed_content_list


class OmniParserService:
    _instance: OmniParserService | None = None
    _parser: Any = None

    def __new__(cls) -> OmniParserService:
        if cls._instance is None:
            with _lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def models_loaded(self) -> bool:
        return self._parser is not None

    def load_models(self) -> None:
        if self._parser is not None:
            return
        with _lock:
            if self._parser is not None:
                return
            _ensure_omniparser_on_path()
            from util.omniparser import Omniparser  # type: ignore[import-not-found]

            weights_dir = settings.weights_dir
            config = {
                "som_model_path": str(Path(weights_dir) / "icon_detect" / "model.pt"),
                "caption_model_name": "florence2",
                "caption_model_path": str(Path(weights_dir) / "icon_caption_florence"),
                "BOX_TRESHOLD": settings.box_threshold,
            }
            self._parser = Omniparser(config)
            logger.info("OmniParser models loaded from %s", weights_dir)

    def parse(
        self,
        image_base64: str,
        box_threshold: float | None = None,
        iou_threshold: float | None = None,
    ) -> ParseResult:
        self.load_models()

        try:
            image, width, height = _decode_image(image_base64)
            draw_config = _build_draw_config(image.size)
            effective_box, effective_iou = _resolve_thresholds(
                box_threshold, iou_threshold
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
                _build_element(i, raw) for i, raw in enumerate(parsed_content_list)
            )

            return ParseResult(
                annotated_image=annotated_img,
                elements=elements,
                image_width=width,
                image_height=height,
            )
        finally:
            gc.collect()
            torch.cuda.empty_cache()

    def parse_pixels(
        self,
        image_base64: str,
        box_threshold: float | None = None,
        iou_threshold: float | None = None,
    ) -> PixelParseResult:
        result = self.parse(image_base64, box_threshold, iou_threshold)
        pixel_elements = tuple(
            _to_pixel_element(el, result.image_width, result.image_height)
            for el in result.elements
        )
        return PixelParseResult(
            annotated_image=result.annotated_image,
            elements=pixel_elements,
            image_width=result.image_width,
            image_height=result.image_height,
        )
