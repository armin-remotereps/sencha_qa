from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from agents.services.llm_client import _build_payload
from agents.services.llm_config import build_refiner_config, build_summarizer_config
from agents.types import ChatMessage, LLMConfig


def _config(temperature: float | None) -> LLMConfig:
    return LLMConfig(
        model="gpt-test",
        api_key="test-key",
        endpoint_url="https://example.test/v1",
        temperature=temperature,
        max_tokens=100,
    )


_MESSAGES = (ChatMessage(role="user", content="hi"),)


class BuildPayloadTemperatureTests(SimpleTestCase):
    def test_temperature_sent_when_configured(self) -> None:
        payload = _build_payload(_config(0.1), _MESSAGES, ())
        self.assertEqual(payload["temperature"], 0.1)

    def test_zero_temperature_is_still_sent(self) -> None:
        payload = _build_payload(_config(0.0), _MESSAGES, ())
        self.assertEqual(payload["temperature"], 0.0)

    def test_temperature_omitted_when_unset(self) -> None:
        payload = _build_payload(_config(None), _MESSAGES, ())
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["max_completion_tokens"], 100)


class RoleTemperatureSettingsTests(SimpleTestCase):
    @override_settings(SUMMARIZER_TEMPERATURE=None)
    def test_summarizer_reads_temperature_from_settings(self) -> None:
        self.assertIsNone(build_summarizer_config().temperature)

    @override_settings(REFINER_TEMPERATURE=None)
    def test_refiner_reads_temperature_from_settings(self) -> None:
        self.assertIsNone(build_refiner_config().temperature)
