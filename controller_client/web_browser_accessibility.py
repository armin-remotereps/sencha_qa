"""Accessibility mode of the web browser layer (phones only).

When a phone browser exposes no inspectable page (an iOS build whose
WKWebView is not inspectable, Web Inspector off, Android Edge without a
DevTools socket), the web content is still visible in the native
accessibility tree of the app's web view. This module reads that tree from
the native page source (one request), lists links, buttons, texts and text
fields as indexed elements, taps and types on them by their centre, and
reads the address bar. Selectors, scripts, tabs, select, wait and hover do
not exist here; the backends refuse them.

Every function that takes a driver runs inside the phone's ``web_call``
in the native context, so selenium errors are translated there; a lost
session is always re-raised so the session can recreate it.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

from appium.webdriver.webdriver import WebDriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchDriverException,
    WebDriverException,
)
from selenium.webdriver.remote.webelement import WebElement

from controller_client import mobile_gestures as gestures
from controller_client.exceptions import (
    BrowserElementNotFoundError,
    ExecutionError,
    WebBrowserUnsupportedError,
)
from controller_client.protocol import (
    WEB_ELEMENT_TEXT_MAX_CHARS,
    WebActKind,
    WebElementTarget,
    WebReadWhat,
)
from controller_client.web_browser_backend import WebActRequest, WebReadRequest

logger = logging.getLogger(__name__)

# The person's field gets focus after the tap before typing starts.
_FOCUS_SETTLE_SECONDS: Final[float] = 0.3
_UNREADABLE: Final[str] = "The page on the phone could not be read"
_NO_ADDRESS: Final[str] = "The address bar is not visible on the phone"
_NO_TITLE: Final[str] = "The page title is not available in accessibility mode"
_NOT_FIELD: Final[str] = "The element is not a text field"
_NO_FOCUS: Final[str] = "No text field has focus on the phone"
_NEEDS_INDEX: Final[str] = (
    "Accessibility mode: act on an element by its index from the element list"
)
_PAGE_ONLY: Final[str] = "Not available in accessibility mode"

# --- iOS (XCUITest page source) --------------------------------------------
_IOS_WEB_VIEW: Final[str] = "XCUIElementTypeWebView"
_IOS_ROLES: Final[dict[str, str]] = {
    "XCUIElementTypeLink": "link",
    "XCUIElementTypeButton": "button",
    "XCUIElementTypeStaticText": "text",
    "XCUIElementTypeTextField": "field",
    "XCUIElementTypeSecureTextField": "password field",
    "XCUIElementTypeTextView": "field",
    "XCUIElementTypeSearchField": "field",
    "XCUIElementTypeSwitch": "switch",
}
_IOS_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "XCUIElementTypeTextField",
        "XCUIElementTypeSecureTextField",
        "XCUIElementTypeTextView",
        "XCUIElementTypeSearchField",
    }
)
_IOS_SECRET: Final[str] = "XCUIElementTypeSecureTextField"
_IOS_CONTROLS: Final[frozenset[str]] = frozenset(
    {"XCUIElementTypeLink", "XCUIElementTypeButton"}
)
# Unverified on devices: accessibility identifiers / names of the address
# bar in Safari (iOS 15+ "TabBarItemTitle", older "URL"), Chrome and Firefox.
# The value is what the bar shows, often the host only.
_IOS_ADDRESS_NAMES: Final[frozenset[str]] = frozenset(
    {
        "url",
        "tabbaritemtitle",
        "address",
        "address and search bar",
        "addresstoolbar.address",
        "url_bar",
    }
)

# --- Android (UiAutomator2 page source) -------------------------------------
_ANDROID_WEB_VIEW: Final[str] = "android.webkit.WebView"
_ANDROID_EDIT: Final[str] = "android.widget.EditText"
_ANDROID_BUTTON: Final[str] = "android.widget.Button"
# Chromium's omnibox id (Chrome, Edge): "<package>:id/url_bar". Unverified
# for current Edge builds.
_ANDROID_ADDRESS_ID_SUFFIX: Final[str] = ":id/url_bar"
_BOUNDS_RE: Final[re.Pattern[str]] = re.compile(
    r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]"
)


@dataclass(frozen=True)
class A11yElement:
    role: str
    text: str
    # Centre in the phone's input coordinates (iOS points, Android pixels).
    x: int
    y: int
    field: bool


@dataclass(frozen=True)
class A11yPage:
    elements: tuple[A11yElement, ...]
    # Every visible text of the web content, in document order.
    texts: tuple[str, ...]
    address: str
    title: str
    found_web_view: bool


class A11yListing:
    """The last element listing; an index refers to it."""

    def __init__(self) -> None:
        self._elements: tuple[A11yElement, ...] = ()

    def remember(self, elements: tuple[A11yElement, ...]) -> None:
        self._elements = elements

    def reset(self) -> None:
        self._elements = ()

    def get(self, index: int) -> A11yElement:
        if 0 <= index < len(self._elements):
            return self._elements[index]
        raise BrowserElementNotFoundError(
            f"No element [{index}] in the last element list; list the elements again"
        )


PressKey = Callable[[WebDriver, str], None]


def _cut(text: str, limit: int = WEB_ELEMENT_TEXT_MAX_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _parse(source: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(source)
    except ElementTree.ParseError as exc:
        raise ExecutionError(_UNREADABLE) from exc


def _int(node: ElementTree.Element, name: str) -> int:
    try:
        return round(float(node.get(name, "0")))
    except ValueError:
        return 0


def _visible(node: ElementTree.Element, attribute: str) -> bool:
    return node.get(attribute, "true").lower() != "false"


# --- iOS -------------------------------------------------------------------


def _ios_type(node: ElementTree.Element) -> str:
    return node.get("type") or node.tag


def _ios_text(node: ElementTree.Element, kind: str) -> str:
    for name in ("label", "name", "value"):
        if name == "value" and kind == _IOS_SECRET:
            continue
        value = node.get(name, "")
        if value.strip():
            return value
    return ""


def _ios_centre(node: ElementTree.Element) -> tuple[int, int, bool]:
    x, y = _int(node, "x"), _int(node, "y")
    width, height = _int(node, "width"), _int(node, "height")
    return x + width // 2, y + height // 2, width > 0 and height > 0


def parse_ios_page(source: str) -> A11yPage:
    root = _parse(source)
    elements: list[A11yElement] = []
    texts: list[str] = []
    title = ""
    found = False
    for web_view in root.iter():
        if _ios_type(web_view) != _IOS_WEB_VIEW or not _visible(web_view, "visible"):
            continue
        found = True
        title = title or _ios_text(web_view, _IOS_WEB_VIEW)
        for node, inside_control in _walk(web_view, _ios_type, _IOS_CONTROLS):
            kind = _ios_type(node)
            role = _IOS_ROLES.get(kind)
            if role is None or not _visible(node, "visible"):
                continue
            text = _ios_text(node, kind)
            # A link's or button's label already holds its inner texts.
            if text and kind != _IOS_SECRET and not inside_control:
                texts.append(text)
            x, y, has_area = _ios_centre(node)
            if not has_area or (inside_control and kind not in _IOS_FIELDS):
                continue
            if role == "text" and not text:
                continue
            elements.append(A11yElement(role, _cut(text), x, y, kind in _IOS_FIELDS))
    return A11yPage(tuple(elements), tuple(texts), _ios_address(root), title, found)


def _ios_address(root: ElementTree.Element) -> str:
    for node in root.iter():
        name = (node.get("name") or "").strip().lower()
        if name in _IOS_ADDRESS_NAMES:
            value = node.get("value") or node.get("label") or ""
            if value.strip():
                return value.strip()
    return ""


# --- Android ---------------------------------------------------------------


def _android_class(node: ElementTree.Element) -> str:
    return node.get("class") or node.tag


def _android_text(node: ElementTree.Element) -> str:
    if node.get("password", "false").lower() == "true":
        return node.get("hint", "") or ""
    return node.get("text", "") or node.get("content-desc", "") or ""


def _android_centre(node: ElementTree.Element) -> tuple[int, int, bool]:
    match = _BOUNDS_RE.fullmatch(node.get("bounds", ""))
    if match is None:
        return 0, 0, False
    left, top, right, bottom = (int(value) for value in match.groups())
    return (left + right) // 2, (top + bottom) // 2, right > left and bottom > top


def parse_android_page(source: str, package: str) -> A11yPage:
    root = _parse(source)
    elements: list[A11yElement] = []
    texts: list[str] = []
    title = ""
    found = False
    controls = frozenset({_ANDROID_BUTTON})
    for web_view in root.iter():
        if _android_class(web_view) != _ANDROID_WEB_VIEW:
            continue
        if web_view.get("package", package) != package:
            continue
        if not _visible(web_view, "displayed"):
            continue
        found = True
        title = title or _android_text(web_view)
        for node, inside_control in _walk(web_view, _android_class, controls):
            if not _visible(node, "displayed"):
                continue
            kind = _android_class(node)
            text = _android_text(node)
            is_field = kind == _ANDROID_EDIT
            clickable = node.get("clickable", "false").lower() == "true"
            secret = node.get("password", "false").lower() == "true"
            if text and not secret and not inside_control:
                texts.append(text)
            if not (is_field or clickable or text):
                continue
            if inside_control and not is_field:
                continue
            x, y, has_area = _android_centre(node)
            if not has_area:
                continue
            if is_field:
                role = "field"
            elif kind == _ANDROID_BUTTON:
                role = "button"
            elif clickable:
                role = "link"
            else:
                role = "text"
            elements.append(A11yElement(role, _cut(text), x, y, is_field))
    return A11yPage(tuple(elements), tuple(texts), _android_address(root), title, found)


def _android_address(root: ElementTree.Element) -> str:
    for node in root.iter():
        if (node.get("resource-id") or "").endswith(_ANDROID_ADDRESS_ID_SUFFIX):
            return (node.get("text") or "").strip()
    return ""


def _walk(
    root: ElementTree.Element,
    kind_of: Callable[[ElementTree.Element], str],
    controls: frozenset[str],
) -> Iterator[tuple[ElementTree.Element, bool]]:
    """Descendants of ``root`` in document order, with whether one of their
    ancestors below ``root`` is a link or button (whose own label already
    names them)."""
    stack: list[tuple[ElementTree.Element, bool]] = [
        (child, False) for child in reversed(list(root))
    ]
    while stack:
        node, inside = stack.pop()
        yield node, inside
        child_inside = inside or kind_of(node) in controls
        stack.extend((child, child_inside) for child in reversed(list(node)))


# --- Operations ------------------------------------------------------------


def _require_index(target: WebElementTarget | None) -> int:
    if target is None or target.index is None:
        raise WebBrowserUnsupportedError(_NEEDS_INDEX)
    return target.index


def list_elements(page: A11yPage, listing: A11yListing, max_elements: int) -> str:
    elements = page.elements[:max_elements]
    listing.remember(elements)
    if not elements:
        return "No elements are visible on the page"
    lines = []
    for index, element in enumerate(elements):
        extra = " (field)" if element.field and element.role == "field" else ""
        lines.append(f'[{index}] {element.role} "{element.text}"{extra}')
    return "\n".join(lines)


def read(page: A11yPage, listing: A11yListing, request: WebReadRequest) -> str:
    if request.what is WebReadWhat.TEXT:
        if request.target is not None:
            raise WebBrowserUnsupportedError(_NEEDS_INDEX)
        return "\n".join(page.texts)[: request.max_chars]
    if request.what is WebReadWhat.URL:
        if not page.address:
            raise ExecutionError(_NO_ADDRESS)
        return page.address
    if request.what is WebReadWhat.TITLE:
        if not page.title:
            raise WebBrowserUnsupportedError(_NO_TITLE)
        return page.title
    if request.what is WebReadWhat.ELEMENT_TEXT:
        return listing.get(_require_index(request.target)).text[: request.max_chars]
    raise WebBrowserUnsupportedError(_PAGE_ONLY)


def act(
    driver: WebDriver,
    listing: A11yListing,
    request: WebActRequest,
    press_key: PressKey,
) -> str:
    if request.action is WebActKind.PRESS_KEY:
        press_key(driver, request.key or "")
        return "Pressed the key"
    if request.action in (WebActKind.HOVER, WebActKind.SELECT):
        raise WebBrowserUnsupportedError(_PAGE_ONLY)
    index = _require_index(request.target)
    element = listing.get(index)
    if request.action is WebActKind.CLICK:
        gestures.tap(driver, element.x, element.y)
        return f"Tapped element {index}"
    if request.action is WebActKind.DOUBLE_CLICK:
        gestures.double_tap(driver, element.x, element.y)
        return f"Double tapped element {index}"
    if not element.field:
        raise ExecutionError(_NOT_FIELD)
    gestures.tap(driver, element.x, element.y)
    time.sleep(_FOCUS_SETTLE_SECONDS)
    if request.action is WebActKind.CLEAR:
        _focused_field(driver).clear()
        return f"Cleared element {index}"
    _type(driver, request.text or "", request.clear)
    return f"Typed into element {index}"


def _focused_field(driver: WebDriver) -> WebElement:
    """The focused element, or ExecutionError. Unverified on devices: both
    XCUITest and UiAutomator2 document ``GET /element/active``."""
    try:
        return driver.switch_to.active_element
    except (InvalidSessionIdException, NoSuchDriverException):
        raise
    except WebDriverException as exc:
        raise ExecutionError(_NO_FOCUS) from exc


def _type(driver: WebDriver, text: str, clear: bool) -> None:
    try:
        element = driver.switch_to.active_element
        if clear:
            element.clear()
        element.send_keys(text)
        return
    except (InvalidSessionIdException, NoSuchDriverException):
        raise
    except WebDriverException as exc:
        if clear:
            raise ExecutionError(_NO_FOCUS) from exc
        logger.debug("Typing into the focused element failed; using key actions")
    # The field has focus from the tap: W3C key actions type at the cursor.
    gestures.send_keys(driver, list(text), 0.0)
