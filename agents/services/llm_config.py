from __future__ import annotations

from django.conf import settings

from agents.types import LLMConfig

# The full output ceiling of the configured model. A reasoning model draws its
# hidden thinking from the same budget as the visible answer, so every role asks
# for the whole thing: capability is worth more here than a smaller bill.
MAX_OUTPUT_TOKENS = 128_000


def build_llm_config() -> LLMConfig:
    return LLMConfig(
        model=settings.OPENAI_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        api_key=settings.OPENAI_API_KEY,
        endpoint_url=settings.OPENAI_BASE_URL,
        reasoning_summary=settings.OPENAI_REASONING_SUMMARY or None,
    )
