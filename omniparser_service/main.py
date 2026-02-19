from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from omniparser_service.config import settings
from omniparser_service.dependencies import get_parser_service, require_api_key
from omniparser_service.parser import OmniParserService

logger = logging.getLogger(__name__)


class ParseRequest(BaseModel):
    image_base64: str
    box_threshold: float | None = None
    iou_threshold: float | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.preload_models:
        logger.info("Preloading OmniParser models...")
        service = get_parser_service()
        service.load_models()
        logger.info("OmniParser models preloaded successfully")
    yield


app = FastAPI(title="OmniParser Service", lifespan=lifespan)


def _parse_error_response(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("%s during parse request", type(exc).__name__)
    return JSONResponse(status_code=500, content={"error": "Parse failed"})


for _exc_type in (ValueError, RuntimeError, OSError):
    app.add_exception_handler(_exc_type, _parse_error_response)


@app.get("/omniparser/health/")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/omniparser/ready/")
def ready(
    service: OmniParserService = Depends(get_parser_service),
) -> dict[str, bool]:
    return {"models_loaded": service.models_loaded}


@app.post("/omniparser/parse/")
def parse(
    body: ParseRequest,
    _api_key: None = Depends(require_api_key),
    service: OmniParserService = Depends(get_parser_service),
) -> dict[str, object]:
    result = service.parse(
        image_base64=body.image_base64,
        box_threshold=body.box_threshold,
        iou_threshold=body.iou_threshold,
    )
    return asdict(result)


@app.post("/omniparser/parse/pixels/")
def parse_pixels(
    body: ParseRequest,
    _api_key: None = Depends(require_api_key),
    service: OmniParserService = Depends(get_parser_service),
) -> dict[str, object]:
    result = service.parse_pixels(
        image_base64=body.image_base64,
        box_threshold=body.box_threshold,
        iou_threshold=body.iou_threshold,
    )
    return asdict(result)
