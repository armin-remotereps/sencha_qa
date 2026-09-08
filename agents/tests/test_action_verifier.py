from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from agents.services.action_verifier import (
    _parse_verdict,
    verify_action_outcome,
    verify_candidate,
)
from agents.types import (
    ChatMessage,
    ImageContent,
    LLMConfig,
    LLMResponse,
    PixelBBox,
    PixelParseResult,
    PixelUIElement,
    TextContent,
)

_VERIFIER = "agents.services.action_verifier"

_LLM_CONFIG = LLMConfig(
    model="gpt-test",
    api_key="test-key",
    endpoint_url="https://example.test/v1",
    temperature=0.0,
    max_tokens=100,
)


def _element(index: int, content: str) -> PixelUIElement:
    return PixelUIElement(
        index=index,
        type="icon",
        content=content,
        bbox=PixelBBox(x_min=0, y_min=0, x_max=10, y_max=10),
        center_x=5,
        center_y=5,
        interactivity=True,
    )


def _llm_response(text: str | None) -> LLMResponse:
    return LLMResponse(
        message=ChatMessage(role="assistant", content=text),
        finish_reason="stop",
        usage_prompt_tokens=0,
        usage_completion_tokens=0,
    )


def _user_text(messages: tuple[ChatMessage, ...]) -> str:
    content = messages[1].content
    assert isinstance(content, tuple)
    return "\n".join(p.text for p in content if isinstance(p, TextContent))


def _user_images(messages: tuple[ChatMessage, ...]) -> list[str]:
    content = messages[1].content
    assert isinstance(content, tuple)
    return [p.base64_data for p in content if isinstance(p, ImageContent)]


class ParseVerdictTests(SimpleTestCase):
    def test_yes_with_reason_is_accepted(self) -> None:
        verdict = _parse_verdict("YES\nThe box wraps the Save button.")

        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.reason, "The box wraps the Save button.")

    def test_lowercase_no_is_rejected(self) -> None:
        verdict = _parse_verdict("no\nIt is the Cancel button.")

        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, "It is the Cancel button.")

    def test_leading_blank_lines_are_ignored(self) -> None:
        verdict = _parse_verdict("\n\n  YES - matches")

        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.reason, "YES - matches")

    def test_unparseable_text_is_rejected_with_raw_text(self) -> None:
        verdict = _parse_verdict("Maybe, hard to tell.")

        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, "Maybe, hard to tell.")

    def test_missing_content_is_rejected(self) -> None:
        verdict = _parse_verdict(None)

        self.assertFalse(verdict.accepted)
        self.assertIn("no content", verdict.reason)


class VerifyCandidateTests(SimpleTestCase):
    def test_sends_annotated_image_and_candidate_details(self) -> None:
        parse_result = PixelParseResult(
            annotated_image="ANNOTATED",
            elements=(_element(3, "Save"),),
            image_width=100,
            image_height=100,
        )
        with patch(
            f"{_VERIFIER}.send_chat_completion", return_value=_llm_response("YES\nok")
        ) as sent:
            verdict = verify_candidate(
                _LLM_CONFIG, parse_result, _element(3, "Save"), "the Save button"
            )

        self.assertTrue(verdict.accepted)
        messages = sent.call_args.args[1]
        self.assertEqual(_user_images(messages), ["ANNOTATED"])
        text = _user_text(messages)
        self.assertIn("Box [3]", text)
        self.assertIn('content="Save"', text)
        self.assertIn("the Save button", text)


class VerifyActionOutcomeTests(SimpleTestCase):
    def test_sends_both_screenshots_and_expected_result(self) -> None:
        with patch(
            f"{_VERIFIER}.send_chat_completion",
            return_value=_llm_response("NO\nNothing changed."),
        ) as sent:
            verdict = verify_action_outcome(
                _LLM_CONFIG,
                "BEFORE_IMG",
                "AFTER_IMG",
                "clicked 'Save' at (5, 5)",
                "the document is saved",
            )

        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, "Nothing changed.")
        messages = sent.call_args.args[1]
        self.assertEqual(_user_images(messages), ["BEFORE_IMG", "AFTER_IMG"])
        text = _user_text(messages)
        self.assertIn("BEFORE:", text)
        self.assertIn("AFTER:", text)
        self.assertIn("clicked 'Save' at (5, 5)", text)
        self.assertIn("the document is saved", text)
