from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from agents.exceptions import ElementNotFoundError, RejectedElementChosenError
from agents.services.controller_element_finder import (
    _build_element_list,
    _parse_match_response,
    find_element_coordinates,
    match_element,
)
from agents.types import (
    ChatMessage,
    LLMConfig,
    LLMResponse,
    PixelBBox,
    PixelParseResult,
    PixelUIElement,
    TextContent,
)

_LLM_CONFIG = LLMConfig(
    model="gpt-test",
    api_key="test-key",
    endpoint_url="https://example.test/v1",
    temperature=0.0,
    max_tokens=100,
)


def _element(
    index: int, content: str, center_x: int = 0, center_y: int = 0
) -> PixelUIElement:
    return PixelUIElement(
        index=index,
        type="icon",
        content=content,
        bbox=PixelBBox(x_min=0, y_min=0, x_max=10, y_max=10),
        center_x=center_x,
        center_y=center_y,
        interactivity=True,
    )


def _llm_response(text: str) -> LLMResponse:
    return LLMResponse(
        message=ChatMessage(role="assistant", content=text),
        finish_reason="stop",
        usage_prompt_tokens=0,
        usage_completion_tokens=0,
    )


class BuildElementListTests(SimpleTestCase):
    def test_formats_each_element_on_its_own_line(self) -> None:
        elements = (_element(0, "Save", 10, 20), _element(1, "Cancel", 30, 40))

        text = _build_element_list(elements)

        self.assertIn('[0] type=icon, content="Save", center=(10, 20)', text)
        self.assertIn('[1] type=icon, content="Cancel", center=(30, 40)', text)


class ParseMatchResponseTests(SimpleTestCase):
    def test_not_found_raises(self) -> None:
        with self.assertRaises(ElementNotFoundError):
            _parse_match_response("NOT_FOUND", "some button", (_element(0, "Save"),))

    def test_unparseable_answer_raises(self) -> None:
        with self.assertRaises(ElementNotFoundError):
            _parse_match_response("no idea", "some button", (_element(0, "Save"),))

    def test_index_not_in_elements_raises(self) -> None:
        with self.assertRaises(ElementNotFoundError):
            _parse_match_response("5", "some button", (_element(0, "Save"),))

    def test_returns_matching_element(self) -> None:
        elements = (_element(0, "Save"), _element(1, "Cancel"))

        matched = _parse_match_response("1", "cancel button", elements)

        self.assertEqual(matched.content, "Cancel")


class FindElementCoordinatesTests(SimpleTestCase):
    def test_raises_when_no_elements_detected(self) -> None:
        empty_result = PixelParseResult(
            annotated_image="fake", elements=(), image_width=100, image_height=100
        )

        with patch(
            "agents.services.controller_element_finder.controller_find_elements",
            return_value=empty_result,
        ):
            with self.assertRaises(ElementNotFoundError):
                find_element_coordinates(1, "save button", _LLM_CONFIG)

    def test_returns_matched_element_coordinates(self) -> None:
        result = PixelParseResult(
            annotated_image="annotated-base64",
            elements=(_element(0, "Save", center_x=42, center_y=99),),
            image_width=100,
            image_height=100,
        )
        screenshots: list[tuple[str, str]] = []

        with (
            patch(
                "agents.services.controller_element_finder.controller_find_elements",
                return_value=result,
            ),
            patch(
                "agents.services.controller_element_finder.send_chat_completion",
                return_value=_llm_response("0"),
            ),
        ):
            x, y = find_element_coordinates(
                1,
                "save button",
                _LLM_CONFIG,
                on_screenshot=lambda img, tag: screenshots.append((img, tag)),
            )

        self.assertEqual((x, y), (42, 99))
        self.assertEqual(screenshots, [("annotated-base64", "controller_omniparser")])


class MatchElementTests(SimpleTestCase):
    _FINDER = "agents.services.controller_element_finder"

    def _parse(self) -> PixelParseResult:
        return PixelParseResult(
            annotated_image="IMG",
            elements=(_element(0, "Save", 10, 20), _element(1, "Cancel", 30, 40)),
            image_width=100,
            image_height=100,
        )

    def test_excluded_elements_are_listed_as_ruled_out_in_the_prompt(self) -> None:
        with patch(
            f"{self._FINDER}.send_chat_completion", return_value=_llm_response("1")
        ) as sent:
            matched = match_element(
                self._parse(),
                "Cancel",
                _LLM_CONFIG,
                is_excluded=lambda el: el.index == 0,
            )

        self.assertEqual(matched.index, 1)
        content = sent.call_args.args[1][1].content
        assert isinstance(content, tuple)
        text = "\n".join(p.text for p in content if isinstance(p, TextContent))
        self.assertNotIn('[0] type=icon, content="Save"', text)
        self.assertIn("Already ruled out, do not choose: [0]", text)

    def test_choosing_a_ruled_out_element_raises_rejected_element_error(self) -> None:
        with patch(
            f"{self._FINDER}.send_chat_completion", return_value=_llm_response("0")
        ):
            with self.assertRaises(RejectedElementChosenError) as ctx:
                match_element(
                    self._parse(),
                    "Cancel",
                    _LLM_CONFIG,
                    is_excluded=lambda el: el.index == 0,
                )

        self.assertEqual(ctx.exception.element.index, 0)

    def test_all_elements_excluded_raises_element_not_found(self) -> None:
        with self.assertRaises(ElementNotFoundError) as ctx:
            match_element(
                self._parse(), "Cancel", _LLM_CONFIG, is_excluded=lambda el: True
            )

        self.assertIn("already rejected", str(ctx.exception))
