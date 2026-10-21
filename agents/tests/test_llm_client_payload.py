from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from agents.services.llm_client import _build_payload
from agents.services.llm_config import MAX_OUTPUT_TOKENS, build_llm_config
from agents.types import ChatMessage, LLMConfig

_MESSAGES = (ChatMessage(role="user", content="hi"),)


def _config() -> LLMConfig:
    return LLMConfig(
        model="gpt-test",
        api_key="test-key",
        endpoint_url="https://example.test/v1",
        max_tokens=100,
    )


class BuildPayloadTests(SimpleTestCase):
    def test_temperature_is_never_sent(self) -> None:
        payload = _build_payload(_config(), _MESSAGES, ())
        self.assertNotIn("temperature", payload)

    def test_budget_uses_the_reasoning_model_token_field(self) -> None:
        payload = _build_payload(_config(), _MESSAGES, ())
        self.assertEqual(payload["max_completion_tokens"], 100)
        self.assertNotIn("max_tokens", payload)


class LLMConfigTests(SimpleTestCase):
    @override_settings(OPENAI_MODEL="gpt-5.6-sol")
    def test_model_comes_from_the_one_setting(self) -> None:
        self.assertEqual(build_llm_config().model, "gpt-5.6-sol")

    def test_every_role_asks_for_the_full_output_ceiling(self) -> None:
        self.assertEqual(build_llm_config().max_tokens, MAX_OUTPUT_TOKENS)
        self.assertEqual(MAX_OUTPUT_TOKENS, 128_000)
