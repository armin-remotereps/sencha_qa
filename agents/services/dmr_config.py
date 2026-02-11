from __future__ import annotations

from django.conf import settings

from agents.types import DMRConfig


def build_dmr_config(*, model: str | None = None) -> DMRConfig:
    return DMRConfig(
        host=settings.DMR_HOST,
        port=settings.DMR_PORT,
        model=model or settings.DMR_MODEL,
        temperature=settings.DMR_TEMPERATURE,
        max_tokens=settings.DMR_MAX_TOKENS,
    )


def build_summarizer_config(*, model: str | None = None) -> DMRConfig:
    return DMRConfig(
        host=settings.DMR_HOST,
        port=settings.DMR_PORT,
        model=model or settings.DMR_SUMMARIZER_MODEL,
        temperature=0.0,
        max_tokens=512,
    )


def build_vision_dmr_config(*, model: str | None = None) -> DMRConfig:
    return DMRConfig(
        host=settings.DMR_HOST,
        port=settings.DMR_PORT,
        model=model or settings.DMR_VISION_MODEL,
        temperature=settings.DMR_TEMPERATURE,
        max_tokens=settings.DMR_MAX_TOKENS,
    )


def build_openai_vision_config(*, model: str | None = None) -> DMRConfig:
    return DMRConfig(
        host="",
        port="",
        model=model or settings.OPENAI_VISION_MODEL,
        temperature=settings.OPENAI_TEMPERATURE,
        max_tokens=settings.OPENAI_MAX_TOKENS,
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_BASE_URL,
    )


def build_vision_config(*, model: str | None = None) -> DMRConfig:
    if settings.VISION_BACKEND == "openai":
        return build_openai_vision_config(model=model)
    return build_vision_dmr_config(model=model)
