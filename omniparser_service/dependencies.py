from __future__ import annotations

from fastapi import Header, HTTPException

from omniparser_service.config import settings
from omniparser_service.parser import OmniParserService


def get_parser_service() -> OmniParserService:
    return OmniParserService()


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not settings.api_key or x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
