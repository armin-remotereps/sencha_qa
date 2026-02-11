from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services.element_finder import (
    _CHUNK_SIZE,
    _COLLECT_ELEMENTS_JS,
    _ELEMENT_LIST_CHAR_BUDGET,
    AmbiguousElementError,
    ElementNotFoundError,
    _build_element_list,
    _find_element_chunked,
    _max_idx_in_chunk,
    _split_into_chunks,
    find_element_by_description,
)
from agents.types import ChatMessage, DMRConfig, DMRResponse


def _make_config() -> DMRConfig:
    return DMRConfig(
        host="localhost",
        port="12434",
        model="ai/mistral",
        temperature=0.1,
        max_tokens=4096,
    )


def _make_elements(count: int = 6) -> list[dict[str, object]]:
    """Create a list of mock element dicts."""
    elements: list[dict[str, object]] = []
    for i in range(count):
        elements.append(
            {
                "idx": i,
                "tag": "button",
                "text": f"Button {i}",
                "role": "button",
                "ariaLabel": f"Button {i} label",
                "placeholder": "",
                "type": "button",
                "name": f"btn{i}",
                "id": f"button-{i}",
                "href": "",
                "value": "",
            }
        )
    return elements


def _make_dmr_response(content: str) -> DMRResponse:
    return DMRResponse(
        message=ChatMessage(role="assistant", content=content),
        finish_reason="stop",
        usage_prompt_tokens=50,
        usage_completion_tokens=10,
    )


# ============================================================================
# find_element_by_description tests
# ============================================================================


@patch("agents.services.element_finder.send_chat_completion")
def test_find_element_success(mock_send: MagicMock) -> None:
    """AI returns '5', result is [data-at-idx='5']."""
    mock_page = MagicMock()
    mock_page.evaluate.return_value = _make_elements(count=6)
    mock_send.return_value = _make_dmr_response("5")

    result = find_element_by_description(
        page=mock_page,
        description="the fifth button",
        dmr_config=_make_config(),
    )

    assert result == '[data-at-idx="5"]'


@patch("agents.services.element_finder.send_chat_completion")
def test_find_element_ambiguous(mock_send: MagicMock) -> None:
    """AI returns 'AMBIGUOUS: ...', raises AmbiguousElementError."""
    mock_page = MagicMock()
    mock_page.evaluate.return_value = _make_elements(count=3)
    mock_send.return_value = _make_dmr_response(
        "AMBIGUOUS: Multiple buttons match the description"
    )

    with pytest.raises(AmbiguousElementError, match="AMBIGUOUS"):
        find_element_by_description(
            page=mock_page,
            description="a button",
            dmr_config=_make_config(),
        )


@patch("agents.services.element_finder.send_chat_completion")
def test_find_element_not_found(mock_send: MagicMock) -> None:
    """AI returns 'NOT_FOUND', raises ElementNotFoundError."""
    mock_page = MagicMock()
    mock_page.evaluate.return_value = _make_elements(count=3)
    mock_send.return_value = _make_dmr_response("NOT_FOUND")

    with pytest.raises(ElementNotFoundError, match="No element found matching"):
        find_element_by_description(
            page=mock_page,
            description="a nonexistent slider",
            dmr_config=_make_config(),
        )


def test_find_element_no_elements_on_page() -> None:
    """Empty elements list raises ElementNotFoundError."""
    mock_page = MagicMock()
    mock_page.evaluate.return_value = []

    with pytest.raises(ElementNotFoundError, match="No interactive elements"):
        find_element_by_description(
            page=mock_page,
            description="any element",
            dmr_config=_make_config(),
        )


@patch("agents.services.element_finder.send_chat_completion")
def test_find_element_index_out_of_range(mock_send: MagicMock) -> None:
    """Index beyond list raises ElementNotFoundError."""
    mock_page = MagicMock()
    mock_page.evaluate.return_value = _make_elements(count=3)
    mock_send.return_value = _make_dmr_response("99")

    with pytest.raises(ElementNotFoundError, match="out of range"):
        find_element_by_description(
            page=mock_page,
            description="element 99",
            dmr_config=_make_config(),
        )


@patch("agents.services.element_finder.send_chat_completion")
def test_find_element_extracts_number_from_text(mock_send: MagicMock) -> None:
    """AI returns 'The element is 3', extracts 3."""
    mock_page = MagicMock()
    mock_page.evaluate.return_value = _make_elements(count=6)
    mock_send.return_value = _make_dmr_response("The element is 3")

    result = find_element_by_description(
        page=mock_page,
        description="the third button",
        dmr_config=_make_config(),
    )

    assert result == '[data-at-idx="3"]'


# ============================================================================
# _build_element_list tests
# ============================================================================


def test_build_element_list_formats_correctly() -> None:
    """_build_element_list produces correct format."""
    elements: list[object] = [
        {
            "idx": 0,
            "tag": "a",
            "text": "Home",
            "role": "link",
            "ariaLabel": "Go home",
            "placeholder": "",
            "type": "",
            "name": "",
            "id": "home-link",
            "href": "/home",
            "value": "",
        },
        {
            "idx": 1,
            "tag": "input",
            "text": "",
            "role": "",
            "ariaLabel": "",
            "placeholder": "Enter name",
            "type": "text",
            "name": "username",
            "id": "name-field",
            "href": "",
            "value": "",
        },
    ]

    result = _build_element_list(elements)
    lines = result.split("\n")

    assert len(lines) == 2

    # First element: link with text, role, aria-label, id, href
    assert lines[0].startswith("[0] <a>")
    assert 'text="Home"' in lines[0]
    assert 'role="link"' in lines[0]
    assert 'aria-label="Go home"' in lines[0]
    assert 'id="home-link"' in lines[0]
    assert 'href="/home"' in lines[0]

    # Second element: input with placeholder, type, name, id
    assert lines[1].startswith("[1] <input>")
    assert 'placeholder="Enter name"' in lines[1]
    assert 'type="text"' in lines[1]
    assert 'name="username"' in lines[1]
    assert 'id="name-field"' in lines[1]
    # Empty fields should not appear
    assert "text=" not in lines[1]
    assert "role=" not in lines[1]
    assert "href=" not in lines[1]


# ============================================================================
# JS evaluation test
# ============================================================================


@patch("agents.services.element_finder.send_chat_completion")
def test_collect_elements_js_called(mock_send: MagicMock) -> None:
    """page.evaluate called with the JS."""
    mock_page = MagicMock()
    mock_page.evaluate.return_value = _make_elements(count=2)
    mock_send.return_value = _make_dmr_response("0")

    find_element_by_description(
        page=mock_page,
        description="first button",
        dmr_config=_make_config(),
    )

    mock_page.evaluate.assert_called_once_with(_COLLECT_ELEMENTS_JS)


# ============================================================================
# Chunked element finder tests
# ============================================================================


def test_split_into_chunks_even() -> None:
    """Chunks split evenly when count is a multiple of chunk_size."""
    items: list[object] = list(range(10))
    chunks = _split_into_chunks(items, 5)
    assert len(chunks) == 2
    assert chunks[0] == [0, 1, 2, 3, 4]
    assert chunks[1] == [5, 6, 7, 8, 9]


def test_split_into_chunks_uneven() -> None:
    """Last chunk is smaller when count is not a multiple of chunk_size."""
    items: list[object] = list(range(7))
    chunks = _split_into_chunks(items, 3)
    assert len(chunks) == 3
    assert chunks[0] == [0, 1, 2]
    assert chunks[1] == [3, 4, 5]
    assert chunks[2] == [6]


def test_split_into_chunks_empty() -> None:
    """Empty list returns empty chunks."""
    chunks = _split_into_chunks([], 5)
    assert chunks == []


def test_max_idx_in_chunk() -> None:
    """_max_idx_in_chunk returns max idx value."""
    chunk: list[object] = [{"idx": 10}, {"idx": 25}, {"idx": 15}]
    assert _max_idx_in_chunk(chunk) == 25


def test_max_idx_in_chunk_empty() -> None:
    """_max_idx_in_chunk returns 0 for empty chunk."""
    assert _max_idx_in_chunk([]) == 0


@patch("agents.services.element_finder.send_chat_completion")
def test_find_element_small_list_skips_chunking(mock_send: MagicMock) -> None:
    """Small element list uses fast path, not chunking."""
    mock_page = MagicMock()
    elements = _make_elements(count=3)
    mock_page.evaluate.return_value = elements
    mock_send.return_value = _make_dmr_response("1")

    result = find_element_by_description(
        page=mock_page,
        description="button 1",
        dmr_config=_make_config(),
    )

    assert result == '[data-at-idx="1"]'
    # Only one AI call (fast path)
    assert mock_send.call_count == 1


@patch("agents.services.element_finder.send_chat_completion")
def test_find_element_chunked_single_match(mock_send: MagicMock) -> None:
    """100 elements, match found in one chunk."""
    mock_page = MagicMock()
    elements = _make_elements(count=100)
    mock_page.evaluate.return_value = elements

    # First build element list to check it exceeds budget
    element_list = _build_element_list(elements)
    assert len(element_list) > _ELEMENT_LIST_CHAR_BUDGET

    # AI returns NOT_FOUND for all chunks except one that returns "42"
    def side_effect(
        config: DMRConfig, messages: tuple[ChatMessage, ...]
    ) -> DMRResponse:
        prompt = messages[-1].content
        assert isinstance(prompt, str)
        if "[42]" in prompt:
            return _make_dmr_response("42")
        return _make_dmr_response("NOT_FOUND")

    mock_send.side_effect = side_effect

    result = find_element_by_description(
        page=mock_page,
        description="button 42",
        dmr_config=_make_config(),
    )

    assert result == '[data-at-idx="42"]'


@patch("agents.services.element_finder.send_chat_completion")
def test_find_element_chunked_no_match(mock_send: MagicMock) -> None:
    """100 elements, no matches raises ElementNotFoundError."""
    mock_page = MagicMock()
    elements = _make_elements(count=100)
    mock_page.evaluate.return_value = elements

    mock_send.return_value = _make_dmr_response("NOT_FOUND")

    with pytest.raises(ElementNotFoundError, match="No element found matching"):
        find_element_by_description(
            page=mock_page,
            description="nonexistent element",
            dmr_config=_make_config(),
        )


@patch("agents.services.element_finder.send_chat_completion")
def test_find_element_chunked_multiple_candidates(mock_send: MagicMock) -> None:
    """Matches in 2 chunks triggers disambiguation pass."""
    mock_page = MagicMock()
    elements = _make_elements(count=100)
    mock_page.evaluate.return_value = elements

    call_count = 0

    def side_effect(
        config: DMRConfig, messages: tuple[ChatMessage, ...]
    ) -> DMRResponse:
        nonlocal call_count
        call_count += 1
        prompt = messages[-1].content
        assert isinstance(prompt, str)
        # Match in chunk containing idx 10
        if "[10]" in prompt and "[11]" in prompt:
            return _make_dmr_response("10")
        # Match in chunk containing idx 60
        if "[60]" in prompt and "[61]" in prompt:
            return _make_dmr_response("60")
        # Disambiguation pass: only 10 and 60 are candidates, pick 60
        if "[10]" in prompt and "[60]" in prompt and "[11]" not in prompt:
            return _make_dmr_response("60")
        return _make_dmr_response("NOT_FOUND")

    mock_send.side_effect = side_effect

    result = find_element_by_description(
        page=mock_page,
        description="a specific button",
        dmr_config=_make_config(),
    )

    assert result == '[data-at-idx="60"]'
    # 4 chunk calls + 1 disambiguation call = 5
    assert call_count == 5


@patch("agents.services.element_finder._ask_ai_for_element")
def test_find_element_chunked_skips_ambiguous_chunks(
    mock_ask: MagicMock,
) -> None:
    """Ambiguous chunks are skipped, not treated as errors."""
    elements = _make_elements(count=60)
    call_idx = 0

    def side_effect(description: str, element_list: str, dmr_config: DMRConfig) -> str:
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return "AMBIGUOUS: multiple match"
        if call_idx == 2:
            return "30"
        return "NOT_FOUND"

    mock_ask.side_effect = side_effect

    result = _find_element_chunked(
        elements,
        "some button",
        _make_config(),
    )

    assert result == '[data-at-idx="30"]'
