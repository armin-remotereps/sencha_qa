from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, Callable, cast

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from omniparser.decorators import require_api_key
from omniparser.services.parser import OmniParserService
from omniparser.types import ParseResult, PixelParseResult

logger = logging.getLogger(__name__)


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
def ready(request: HttpRequest) -> JsonResponse:
    service = OmniParserService()
    return JsonResponse({"models_loaded": service.models_loaded})


def _decode_request_json(request: HttpRequest) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(request.body))


def _handle_parse_request(
    request: HttpRequest,
    parse_fn: Callable[..., ParseResult | PixelParseResult],
) -> JsonResponse:
    try:
        body = _decode_request_json(request)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    image_base64: str | None = body.get("image_base64")
    if not image_base64:
        return JsonResponse({"error": "image_base64 is required"}, status=400)

    try:
        result = parse_fn(
            image_base64=image_base64,
            box_threshold=body.get("box_threshold"),
            iou_threshold=body.get("iou_threshold"),
        )
    except (ValueError, RuntimeError, OSError):
        logger.exception("OmniParser parse failed")
        return JsonResponse({"error": "Parse failed"}, status=500)

    return JsonResponse(asdict(result))


@csrf_exempt
@require_POST
@require_api_key
def parse_screenshot(request: HttpRequest) -> JsonResponse:
    return _handle_parse_request(request, OmniParserService().parse)


@csrf_exempt
@require_POST
@require_api_key
def parse_screenshot_pixels(request: HttpRequest) -> JsonResponse:
    return _handle_parse_request(request, OmniParserService().parse_pixels)
