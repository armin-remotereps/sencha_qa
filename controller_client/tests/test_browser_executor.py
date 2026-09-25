from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from controller_client.browser_executor import (
    _ACTION_TIMEOUT_MS,
    _COLLECT_ELEMENTS_JS,
    BrowserSession,
    execute_browser_click,
    execute_browser_get_elements,
    execute_browser_hover,
    execute_browser_type,
)
from controller_client.exceptions import BrowserElementNotFoundError, ExecutionError
from controller_client.protocol import (
    BrowserClickPayload,
    BrowserHoverPayload,
    BrowserTypePayload,
)


class _FakeLocator:
    def __init__(self, frame: _FakeFrame, selector: str) -> None:
        self._frame = frame
        self._selector = selector

    def count(self) -> int:
        return 0 if self._selector in self._frame.missing else 1

    def click(self, *, timeout: float) -> None:
        self._act("click", timeout)
        self._frame.clicks.append(self._selector)

    def fill(self, text: str, *, timeout: float) -> None:
        self._act("fill", timeout)

    def hover(self, *, timeout: float) -> None:
        self._act("hover", timeout)

    def _act(self, action: str, timeout: float) -> None:
        self._frame.timeouts.append(timeout)
        if self._selector in self._frame.not_actionable:
            raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded ({action})")


class _FakeFrame:
    """A frame that mimics the index stamping the collection script performs."""

    def __init__(
        self,
        url: str,
        elements: list[dict[str, object]] | None = None,
        *,
        unreachable: bool = False,
        detached: bool = False,
    ) -> None:
        self.url = url
        self._elements = elements or []
        self._unreachable = unreachable
        self._detached = detached
        self.start_indices: list[int] = []
        self.clicks: list[str] = []
        self.timeouts: list[float] = []
        self.missing: set[str] = set()
        self.not_actionable: set[str] = set()

    def evaluate(self, script: str, start_index: int) -> list[dict[str, object]]:
        self.start_indices.append(start_index)
        if self._unreachable:
            raise RuntimeError("Execution context was destroyed")
        return [
            dict(element, idx=start_index + offset)
            for offset, element in enumerate(self._elements)
        ]

    def is_detached(self) -> bool:
        return self._detached

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)


class _FakePage:
    def __init__(self, frames: list[_FakeFrame]) -> None:
        self.frames = frames
        self.main_frame = frames[0]

    def is_closed(self) -> bool:
        return False


def _session(frames: list[_FakeFrame]) -> BrowserSession:
    session = BrowserSession()
    session._page = cast(Page, _FakePage(frames))
    return session


def _element(tag: str, text: str) -> dict[str, object]:
    return {"tag": tag, "text": text}


def test_collects_elements_from_every_frame() -> None:
    main = _FakeFrame("https://shop.example.com/", [_element("a", "Products")])
    ad = _FakeFrame("https://ads.example.net/unit.html", [_element("button", "Close")])
    session = _session([main, ad])

    result = execute_browser_get_elements(session)

    assert "Products" in result.content
    assert "Close" in result.content


def test_indices_do_not_collide_across_frames() -> None:
    main = _FakeFrame(
        "https://shop.example.com/",
        [_element("a", "Products"), _element("a", "Cart")],
    )
    ad = _FakeFrame("https://ads.example.net/unit.html", [_element("button", "Close")])
    session = _session([main, ad])

    result = execute_browser_get_elements(session)

    assert main.start_indices == [0]
    assert ad.start_indices == [2]
    assert "[0] <a>" in result.content
    assert "[2] <button>" in result.content


def test_elements_outside_the_main_frame_are_labelled() -> None:
    main = _FakeFrame("https://shop.example.com/", [_element("a", "Products")])
    ad = _FakeFrame("https://ads.example.net/unit.html", [_element("button", "Close")])
    session = _session([main, ad])

    lines = execute_browser_get_elements(session).content.splitlines()

    assert 'frame="' not in lines[0]
    assert 'frame="iframe:ads.example.net"' in lines[1]


def test_an_unreachable_frame_does_not_lose_the_rest_of_the_page() -> None:
    main = _FakeFrame("https://shop.example.com/", [_element("a", "Products")])
    dead = _FakeFrame("https://ads.example.net/gone", unreachable=True)
    ad = _FakeFrame("https://ads.example.net/unit.html", [_element("button", "Close")])
    session = _session([main, dead, ad])

    result = execute_browser_get_elements(session)

    assert "Products" in result.content
    assert "Close" in result.content
    assert ad.start_indices == [1]


def test_click_is_routed_to_the_frame_that_owns_the_index() -> None:
    main = _FakeFrame("https://shop.example.com/", [_element("a", "Products")])
    ad = _FakeFrame("https://ads.example.net/unit.html", [_element("button", "Close")])
    session = _session([main, ad])
    execute_browser_get_elements(session)

    execute_browser_click(session, BrowserClickPayload(element_index=1))

    assert ad.clicks == ['[data-at-idx="1"]']
    assert main.clicks == []


def test_click_on_an_index_from_before_the_last_collection_uses_the_main_frame() -> (
    None
):
    main = _FakeFrame("https://shop.example.com/", [_element("a", "Products")])
    session = _session([main])
    execute_browser_get_elements(session)

    execute_browser_click(session, BrowserClickPayload(element_index=99))

    assert main.clicks == ['[data-at-idx="99"]']


def test_click_falls_back_to_the_main_frame_when_the_owning_frame_detached() -> None:
    main = _FakeFrame("https://shop.example.com/", [_element("a", "Products")])
    ad = _FakeFrame(
        "https://ads.example.net/unit.html",
        [_element("button", "Close")],
        detached=True,
    )
    session = _session([main, ad])
    execute_browser_get_elements(session)

    execute_browser_click(session, BrowserClickPayload(element_index=1))

    assert ad.clicks == []
    assert main.clicks == ['[data-at-idx="1"]']


def test_collection_script_reaches_shadow_roots_and_clears_stale_indices() -> None:
    assert "shadowRoot" in _COLLECT_ELEMENTS_JS
    assert "removeAttribute('data-at-idx')" in _COLLECT_ELEMENTS_JS


def test_click_on_a_missing_element_fails_at_once_as_not_found() -> None:
    main = _FakeFrame("https://shop.example.com/", [_element("a", "Products")])
    main.missing.add('[data-at-idx="0"]')
    session = _session([main])

    with pytest.raises(
        BrowserElementNotFoundError, match=r"Element \[0\] was not found"
    ):
        execute_browser_click(session, BrowserClickPayload(element_index=0))

    assert main.clicks == []
    assert main.timeouts == []


def _click(session: BrowserSession) -> object:
    return execute_browser_click(session, BrowserClickPayload(element_index=0))


def _type(session: BrowserSession) -> object:
    return execute_browser_type(session, BrowserTypePayload(element_index=0, text="hi"))


def _hover(session: BrowserSession) -> object:
    return execute_browser_hover(session, BrowserHoverPayload(element_index=0))


_BrowserAction = Callable[[BrowserSession], object]


@pytest.mark.parametrize("run", [_click, _type, _hover])
def test_missing_element_is_not_found_for_every_browser_action(
    run: _BrowserAction,
) -> None:
    main = _FakeFrame("https://shop.example.com/")
    main.missing.add('[data-at-idx="0"]')

    with pytest.raises(BrowserElementNotFoundError):
        run(_session([main]))

    assert main.timeouts == []


@pytest.mark.parametrize(
    ("run", "verb"),
    [(_click, "clicked"), (_type, "typed into"), (_hover, "hovered")],
)
def test_unactionable_element_fails_within_the_action_timeout(
    run: _BrowserAction, verb: str
) -> None:
    main = _FakeFrame("https://shop.example.com/")
    main.not_actionable.add('[data-at-idx="0"]')

    with pytest.raises(ExecutionError) as exc_info:
        run(_session([main]))

    assert not isinstance(exc_info.value, BrowserElementNotFoundError)
    assert f"could not be {verb} within 10s" in str(exc_info.value)
    assert main.timeouts == [_ACTION_TIMEOUT_MS]
