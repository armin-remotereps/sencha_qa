from __future__ import annotations

from django.conf import settings

from agents.types import LLMConfig


def _build_config(*, model: str, temperature: float, max_tokens: int) -> LLMConfig:
    return LLMConfig(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        api_key=settings.OPENAI_API_KEY,
        endpoint_url=settings.OPENAI_BASE_URL,
    )


def build_llm_config(*, model: str | None = None) -> LLMConfig:
    return _build_config(
        model=model or settings.OPENAI_AGENT_MODEL,
        temperature=settings.OPENAI_TEMPERATURE,
        max_tokens=settings.OPENAI_MAX_TOKENS,
    )


def build_orchestrator_config(*, model: str | None = None) -> LLMConfig:
    return _build_config(
        model=model or settings.OPENAI_ORCHESTRATOR_MODEL,
        temperature=settings.ORCHESTRATOR_TEMPERATURE,
        max_tokens=settings.ORCHESTRATOR_MAX_TOKENS,
    )


def build_sub_agent_config(*, model: str | None = None) -> LLMConfig:
    return _build_config(
        model=model or settings.OPENAI_SUB_AGENT_MODEL,
        temperature=settings.SUB_AGENT_TEMPERATURE,
        max_tokens=settings.SUB_AGENT_MAX_TOKENS,
    )


def build_summarizer_config(*, model: str | None = None) -> LLMConfig:
    return _build_config(
        model=model or settings.OPENAI_SUMMARIZER_MODEL,
        temperature=0.0,
        max_tokens=1024,
    )


def build_refiner_config(*, model: str | None = None) -> LLMConfig:
    return _build_config(
        model=model or settings.OPENAI_REFINER_MODEL,
        temperature=0.3,
        max_tokens=2048,
    )


def build_vision_config(*, model: str | None = None) -> LLMConfig:
    return _build_config(
        model=model or settings.OPENAI_VISION_MODEL,
        temperature=settings.OPENAI_TEMPERATURE,
        max_tokens=settings.OPENAI_MAX_TOKENS,
    )
