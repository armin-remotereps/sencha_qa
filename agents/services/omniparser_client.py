from __future__ import annotations

import logging

import httpx
from django.conf import settings

from omniparser_wrapper.types import PixelBBox, PixelParseResult, PixelUIElement

logger = logging.getLogger(__name__)


class OmniParserConnectionError(Exception):
    pass


class OmniParserResponseError(Exception):
    pass


def is_omniparser_configured() -> bool:
    url: str = settings.OMNIPARSER_URL
    return bool(url.strip())


def parse_screenshot_remote(image_base64: str) -> PixelParseResult:
    base_url: str = settings.OMNIPARSER_URL.rstrip("/")
    url = f"{base_url}/omniparser/parse/pixels/"
    api_key: str = settings.OMNIPARSER_API_KEY
    timeout: int = settings.OMNIPARSER_REQUEST_TIMEOUT

    try:
        with httpx.Client(timeout=float(timeout)) as client:
            response = client.post(
                url,
                json={"image_base64": image_base64},
                headers={"X-API-Key": api_key},
            )
            if response.status_code >= 400:
                logger.error(
                    "OmniParser error %d: %s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        msg = f"OmniParser request failed: {exc}"
        raise OmniParserConnectionError(msg) from exc

    data = response.json()
    return _deserialize_pixel_parse_result(data)


def _deserialize_pixel_parse_result(
    data: dict[str, object],
) -> PixelParseResult:
    elements_data = data.get("elements", ())
    if not isinstance(elements_data, list):
        msg = f"Expected 'elements' to be a list, got {type(elements_data).__name__}"
        raise OmniParserResponseError(msg)

    elements = tuple(_deserialize_pixel_element(el) for el in elements_data)

    annotated_image = data.get("annotated_image", "")
    if not isinstance(annotated_image, str):
        msg = f"Expected 'annotated_image' to be str, got {type(annotated_image).__name__}"
        raise OmniParserResponseError(msg)

    image_width = data.get("image_width", 0)
    if not isinstance(image_width, int):
        msg = f"Expected 'image_width' to be int, got {type(image_width).__name__}"
        raise OmniParserResponseError(msg)

    image_height = data.get("image_height", 0)
    if not isinstance(image_height, int):
        msg = f"Expected 'image_height' to be int, got {type(image_height).__name__}"
        raise OmniParserResponseError(msg)

    return PixelParseResult(
        annotated_image=annotated_image,
        elements=elements,
        image_width=image_width,
        image_height=image_height,
    )


def _deserialize_pixel_element(data: object) -> PixelUIElement:
    if not isinstance(data, dict):
        msg = f"Expected element to be a dict, got {type(data).__name__}"
        raise OmniParserResponseError(msg)

    bbox = _deserialize_pixel_bbox(data.get("bbox"))

    return PixelUIElement(
        index=int(data["index"]),
        type=str(data["type"]),
        content=str(data["content"]),
        bbox=bbox,
        center_x=int(data["center_x"]),
        center_y=int(data["center_y"]),
        interactivity=bool(data["interactivity"]),
    )


def _deserialize_pixel_bbox(data: object) -> PixelBBox:
    if not isinstance(data, dict):
        msg = f"Expected bbox to be a dict, got {type(data).__name__}"
        raise OmniParserResponseError(msg)

    return PixelBBox(
        x_min=int(data["x_min"]),
        y_min=int(data["y_min"]),
        x_max=int(data["x_max"]),
        y_max=int(data["y_max"]),
    )
