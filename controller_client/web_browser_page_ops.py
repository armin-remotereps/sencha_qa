"""Page operations of the web browser layer (WEB_BROWSER_V1).

Plain blocking functions over any Selenium ``WebDriver`` in a page context:
desktop Chrome, Edge, Firefox and Safari sessions, and Appium web view
contexts on phones (Appium's WebDriver subclasses Selenium's). Selenium only;
never Playwright.

Element targets: ``index`` refers to the last element listing (its own
``data-ph-idx`` stamps, remembered per index together with the frame it was
found in); ``css`` / ``xpath`` search the top document first, then the
visible same-origin iframes one level deep, the same frames a listing
covers. Every function leaves the driver in the top document.

Messages are short and fixed: they never echo page content, selectors or
typed text.
"""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    InvalidElementStateException,
    InvalidSelectorException,
    JavascriptException,
    MoveTargetOutOfBoundsException,
    NoSuchElementException,
    NoSuchFrameException,
    StaleElementReferenceException,
    TimeoutException,
    UnexpectedTagNameException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.wait import WebDriverWait

from controller_client.exceptions import (
    BrowserElementNotFoundError,
    ExecutionError,
    WebBrowserUnsupportedError,
)
from controller_client.protocol import (
    WEB_ELEMENT_TEXT_MAX_CHARS,
    WebActKind,
    WebElementTarget,
    WebHistoryAction,
    WebReadWhat,
    WebTabsAction,
    WebWaitState,
)
from controller_client.web_browser_backend import WebActRequest, WebReadRequest
from controller_client.web_browser_page_script import (
    FIND_INDEXED_JS,
    FOCUS_JS,
    LIST_ELEMENTS_JS,
    PAGE_TEXT_JS,
    SCROLL_INTO_VIEW_JS,
    STAMP_FRAMES_JS,
)

logger = logging.getLogger(__name__)

ELEMENT_NOT_FOUND_TEXT: Final[str] = "Element not found"
_INDEX_NOT_FOUND_TEXT: Final[str] = "Element not found; list the elements again"
_NOT_INTERACTABLE_TEXT: Final[str] = "Element is not interactable"
_INVALID_SELECTOR_TEXT: Final[str] = "Invalid selector"
_UNKNOWN_KEY_TEXT: Final[str] = "Unknown key"
_NO_TAB_TEXT: Final[str] = "No tab at that index"
_NO_OPTION_TEXT: Final[str] = "No such option in the list"
_NOT_A_SELECT_TEXT: Final[str] = "Element is not a select list"
_WAIT_TIMEOUT_TEXT: Final[str] = "Timed out waiting for the element"
_HOVER_UNSUPPORTED_TEXT: Final[str] = "Hover is not available here"
_POLL_SECONDS: Final[float] = 0.2

# Key names (case-insensitive) -> Selenium key codes. Single characters are
# sent as they are.
_KEYS: Final[dict[str, str]] = {
    "enter": Keys.ENTER,
    "return": Keys.RETURN,
    "tab": Keys.TAB,
    "escape": Keys.ESCAPE,
    "esc": Keys.ESCAPE,
    "backspace": Keys.BACKSPACE,
    "delete": Keys.DELETE,
    "space": Keys.SPACE,
    "arrowdown": Keys.ARROW_DOWN,
    "arrowup": Keys.ARROW_UP,
    "arrowleft": Keys.ARROW_LEFT,
    "arrowright": Keys.ARROW_RIGHT,
    "down": Keys.ARROW_DOWN,
    "up": Keys.ARROW_UP,
    "left": Keys.ARROW_LEFT,
    "right": Keys.ARROW_RIGHT,
    "home": Keys.HOME,
    "end": Keys.END,
    "pageup": Keys.PAGE_UP,
    "pagedown": Keys.PAGE_DOWN,
    "insert": Keys.INSERT,
    "f1": Keys.F1,
    "f2": Keys.F2,
    "f3": Keys.F3,
    "f4": Keys.F4,
    "f5": Keys.F5,
    "f6": Keys.F6,
    "f7": Keys.F7,
    "f8": Keys.F8,
    "f9": Keys.F9,
    "f10": Keys.F10,
    "f11": Keys.F11,
    "f12": Keys.F12,
}
_MODIFIERS: Final[dict[str, str]] = {
    "control": Keys.CONTROL,
    "ctrl": Keys.CONTROL,
    "shift": Keys.SHIFT,
    "alt": Keys.ALT,
    "option": Keys.ALT,
    "meta": Keys.COMMAND,
    "command": Keys.COMMAND,
    "cmd": Keys.COMMAND,
}


class PageListing:
    """The last element listing of one browser: index -> frame number (None:
    the top document). Reset on navigate, history and tab switches."""

    def __init__(self) -> None:
        self.frames: dict[int, int | None] = {}

    def reset(self) -> None:
        self.frames = {}


# --- frames and element lookup ----------------------------------------------


def _top(driver: WebDriver) -> None:
    try:
        driver.switch_to.default_content()
    except WebDriverException:
        logger.debug("Could not return to the top document", exc_info=True)


def _enter_frame(driver: WebDriver, frame: int) -> bool:
    """Switch from the top document into the stamped iframe ``frame``."""
    try:
        element = driver.find_element(By.CSS_SELECTOR, f'[data-ph-frame="{frame}"]')
        driver.switch_to.frame(element)
    except (NoSuchElementException, NoSuchFrameException):
        return False
    return True


def _script_object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _find_by_selector(driver: WebDriver, target: WebElementTarget) -> WebElement | None:
    by, selector = (
        (By.CSS_SELECTOR, target.css)
        if target.css is not None
        else (By.XPATH, target.xpath)
    )
    try:
        found = driver.find_elements(by, selector or "")
    except (InvalidSelectorException, JavascriptException) as e:
        raise ExecutionError(_INVALID_SELECTOR_TEXT) from e
    return found[0] if found else None


def _find_in_top_or_frames(
    driver: WebDriver, target: WebElementTarget
) -> WebElement | None:
    """CSS / XPath: the top document, then each same-origin iframe. Leaves
    the driver in the frame where the element was found."""
    _top(driver)
    element = _find_by_selector(driver, target)
    if element is not None:
        return element
    frames = driver.execute_script(STAMP_FRAMES_JS)
    count = frames if isinstance(frames, int) else 0
    for frame in range(count):
        _top(driver)
        if not _enter_frame(driver, frame):
            continue
        element = _find_by_selector(driver, target)
        if element is not None:
            return element
    _top(driver)
    return None


def _find_indexed(
    driver: WebDriver, listing: PageListing, index: int
) -> WebElement | None:
    if index not in listing.frames:
        return None
    _top(driver)
    frame = listing.frames[index]
    if frame is not None and not _enter_frame(driver, frame):
        return None
    found = driver.execute_script(FIND_INDEXED_JS, index)
    return found if isinstance(found, WebElement) else None


def _find(
    driver: WebDriver, listing: PageListing, target: WebElementTarget
) -> WebElement | None:
    if target.index is not None:
        return _find_indexed(driver, listing, target.index)
    return _find_in_top_or_frames(driver, target)


@contextmanager
def _located(
    driver: WebDriver, listing: PageListing, target: WebElementTarget
) -> Iterator[WebElement]:
    """The target element, with the driver inside its frame; back to the
    top document afterwards. Raises BrowserElementNotFoundError."""
    missing = (
        _INDEX_NOT_FOUND_TEXT if target.index is not None else ELEMENT_NOT_FOUND_TEXT
    )
    try:
        element = _find(driver, listing, target)
        if element is None:
            raise BrowserElementNotFoundError(missing)
        try:
            yield element
        except StaleElementReferenceException as e:
            raise BrowserElementNotFoundError(missing) from e
        except (
            ElementNotInteractableException,
            ElementClickInterceptedException,
            MoveTargetOutOfBoundsException,
        ) as e:
            raise ExecutionError(_NOT_INTERACTABLE_TEXT) from e
    finally:
        _top(driver)


# --- navigation and tabs ----------------------------------------------------


def page_navigate(driver: WebDriver, listing: PageListing, url: str) -> str:
    """Load ``url``; returns the URL afterwards (a page still loading past the
    driver's page-load timeout is not an error)."""
    listing.reset()
    try:
        driver.get(url)
    except TimeoutException:
        logger.debug("Page load timed out; answering the current URL")
    return driver.current_url


def page_history(
    driver: WebDriver, listing: PageListing, action: WebHistoryAction
) -> str:
    listing.reset()
    try:
        if action is WebHistoryAction.BACK:
            driver.back()
        elif action is WebHistoryAction.FORWARD:
            driver.forward()
        else:
            driver.refresh()
    except TimeoutException:
        logger.debug("History load timed out; answering the current URL")
    return driver.current_url


def _tab_list(driver: WebDriver) -> str:
    """``[{index, title, url, current}]``; visits each tab and returns to the
    current one."""
    handles = list(driver.window_handles)
    try:
        current = driver.current_window_handle
    except WebDriverException:
        current = handles[-1] if handles else ""
    tabs: list[dict[str, object]] = []
    for index, handle in enumerate(handles):
        try:
            driver.switch_to.window(handle)
            title = driver.title
            url = driver.current_url
        except WebDriverException:
            title, url = "", ""
        tabs.append(
            {
                "index": index,
                "title": title[:WEB_ELEMENT_TEXT_MAX_CHARS],
                "url": url,
                "current": handle == current,
            }
        )
    if current in handles:
        driver.switch_to.window(current)
    return json.dumps(tabs, ensure_ascii=False)


def page_tabs(
    driver: WebDriver,
    listing: PageListing,
    action: WebTabsAction,
    index: int | None,
) -> str:
    """Runs ``action`` and answers the tab list afterwards (JSON)."""
    if action is WebTabsAction.LIST:
        return _tab_list(driver)
    handles = list(driver.window_handles)
    if action is WebTabsAction.NEW:
        listing.reset()
        driver.switch_to.new_window("tab")
        return _tab_list(driver)
    if action is WebTabsAction.SWITCH:
        if index is None or not 0 <= index < len(handles):
            raise ExecutionError(_NO_TAB_TEXT)
        listing.reset()
        driver.switch_to.window(handles[index])
        return _tab_list(driver)
    # CLOSE: the given tab, or the current one.
    current = driver.current_window_handle
    if index is not None:
        if not 0 <= index < len(handles):
            raise ExecutionError(_NO_TAB_TEXT)
        closing = handles[index]
    else:
        closing = current
    listing.reset()
    driver.switch_to.window(closing)
    driver.close()
    remaining = [handle for handle in handles if handle != closing]
    if not remaining:
        return "[]"
    driver.switch_to.window(current if current in remaining else remaining[-1])
    return _tab_list(driver)


# --- element listing ----------------------------------------------------------


def _element_line(raw: dict[str, object], frame: int | None) -> str:
    def text(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    tag = text("tag")
    role = text("role")
    kind = f"{tag}/{role}" if role and role != tag else tag
    shown = text("text") or text("label") or text("placeholder")
    extras = [
        f"{name}={text(key)}"
        for name, key in (
            ("type", "type"),
            ("name", "name"),
            ("id", "id"),
            ("label", "label"),
            ("placeholder", "placeholder"),
            ("href", "href"),
            ("value", "value"),
        )
        if text(key) and text(key) != shown
    ]
    if frame is not None:
        extras.append(f"frame={frame}")
    extra = f" ({', '.join(extras)})" if extras else ""
    return f'[{raw.get("idx")}] {kind} "{shown[:WEB_ELEMENT_TEXT_MAX_CHARS]}"{extra}'


def _list_in_context(
    driver: WebDriver,
    listing: PageListing,
    frame: int | None,
    start: int,
    remaining: int,
    scope_css: str | None,
    lines: list[str],
) -> int:
    """Lists the current document; returns its stamped iframe count."""
    result = _script_object(
        driver.execute_script(
            LIST_ELEMENTS_JS, start, remaining, scope_css, WEB_ELEMENT_TEXT_MAX_CHARS
        )
    )
    if result.get("error") == "selector":
        raise ExecutionError(_INVALID_SELECTOR_TEXT)
    raw_elements = result.get("elements")
    for raw in raw_elements if isinstance(raw_elements, list) else []:
        if not isinstance(raw, dict):
            continue
        index = raw.get("idx")
        if not isinstance(index, int):
            continue
        listing.frames[index] = frame
        lines.append(_element_line(raw, frame))
    frames = result.get("frames")
    return frames if isinstance(frames, int) else 0


def page_elements(
    driver: WebDriver,
    listing: PageListing,
    scope_css: str | None,
    max_elements: int,
) -> str:
    """One line per visible interactive element:
    ``[<index>] <tag/role> "<text>" (<extra>)``; remembers the listing."""
    listing.reset()
    lines: list[str] = []
    _top(driver)
    try:
        frames = _list_in_context(
            driver, listing, None, 0, max_elements, scope_css, lines
        )
        for frame in range(frames):
            if len(lines) >= max_elements:
                break
            _top(driver)
            if not _enter_frame(driver, frame):
                continue
            try:
                _list_in_context(
                    driver,
                    listing,
                    frame,
                    len(lines),
                    max_elements - len(lines),
                    scope_css,
                    lines,
                )
            except JavascriptException:
                # A frame that navigated or died mid-listing must not cost
                # the rest of the page.
                logger.debug("Skipped the listing of frame %d", frame, exc_info=True)
    finally:
        _top(driver)
    if not lines:
        return "No interactive elements found"
    return "\n".join(lines)


# --- actions ------------------------------------------------------------------


def _key_sequence(key: str) -> tuple[list[str], str]:
    """``"Control+A"`` -> ([CONTROL], "a"); ExecutionError for an unknown
    name."""
    parts = key.split("+") if len(key) > 1 else [key]
    if any(not part for part in parts):
        raise ExecutionError(_UNKNOWN_KEY_TEXT)
    modifiers: list[str] = []
    for part in parts[:-1]:
        modifier = _MODIFIERS.get(part.strip().lower())
        if modifier is None:
            raise ExecutionError(_UNKNOWN_KEY_TEXT)
        modifiers.append(modifier)
    last = parts[-1]
    if len(last) == 1:
        return modifiers, last.lower() if modifiers else last
    mapped = _KEYS.get(last.strip().lower()) or _MODIFIERS.get(last.strip().lower())
    if mapped is None:
        raise ExecutionError(_UNKNOWN_KEY_TEXT)
    return modifiers, mapped


def _press_key(driver: WebDriver, element: WebElement | None, key: str) -> None:
    modifiers, main = _key_sequence(key)
    if element is not None and not modifiers:
        element.send_keys(main)
        return
    if element is not None:
        driver.execute_script(FOCUS_JS, element)
    actions = ActionChains(driver)
    for modifier in modifiers:
        actions.key_down(modifier)
    actions.send_keys(main)
    for modifier in reversed(modifiers):
        actions.key_up(modifier)
    actions.perform()


def _click(driver: WebDriver, element: WebElement, *, double: bool) -> None:
    driver.execute_script(SCROLL_INTO_VIEW_JS, element)
    actions = ActionChains(driver).move_to_element(element)
    if double:
        actions.double_click(element).perform()
        return
    try:
        actions.click().perform()
    except (
        StaleElementReferenceException,
        ElementNotInteractableException,
        ElementClickInterceptedException,
    ):
        raise
    except WebDriverException:
        # Some page contexts (phone web views) refuse pointer actions; the
        # element click is the W3C fallback.
        logger.debug("Pointer click refused; using the element click", exc_info=True)
        element.click()


def _select(element: WebElement, value: str | None, label: str | None) -> None:
    try:
        select = Select(element)
    except UnexpectedTagNameException as e:
        raise ExecutionError(_NOT_A_SELECT_TEXT) from e
    try:
        if value is not None:
            select.select_by_value(value)
        else:
            select.select_by_visible_text(label or "")
    except NoSuchElementException as e:
        raise ExecutionError(_NO_OPTION_TEXT) from e


def page_act(
    driver: WebDriver,
    listing: PageListing,
    request: WebActRequest,
    *,
    allow_hover: bool,
) -> str:
    """Runs one action; returns a short fixed confirmation."""
    action = request.action
    if action is WebActKind.HOVER and not allow_hover:
        raise WebBrowserUnsupportedError(_HOVER_UNSUPPORTED_TEXT)
    if action is WebActKind.PRESS_KEY:
        key = request.key or ""
        if request.target is None:
            _key_sequence(key)  # unknown keys fail before anything is sent
            _press_key(driver, None, key)
            return "Pressed the key"
        with _located(driver, listing, request.target) as element:
            _press_key(driver, element, key)
        return "Pressed the key"
    if request.target is None:
        raise BrowserElementNotFoundError(ELEMENT_NOT_FOUND_TEXT)
    with _located(driver, listing, request.target) as element:
        try:
            if action is WebActKind.CLICK:
                _click(driver, element, double=False)
                return "Clicked"
            if action is WebActKind.DOUBLE_CLICK:
                _click(driver, element, double=True)
                return "Double-clicked"
            if action is WebActKind.HOVER:
                driver.execute_script(SCROLL_INTO_VIEW_JS, element)
                ActionChains(driver).move_to_element(element).perform()
                return "Hovered"
            if action is WebActKind.TYPE:
                if request.clear:
                    element.clear()
                element.send_keys(request.text or "")
                return "Typed the text"
            if action is WebActKind.CLEAR:
                element.clear()
                return "Cleared"
            _select(element, request.value, request.label)
            return "Selected"
        except InvalidElementStateException as e:
            if isinstance(e, ElementNotInteractableException):
                raise
            raise ExecutionError(_NOT_INTERACTABLE_TEXT) from e


# --- read and wait ------------------------------------------------------------


def page_read(driver: WebDriver, listing: PageListing, request: WebReadRequest) -> str:
    what = request.what
    if what is WebReadWhat.URL:
        return driver.current_url
    if what is WebReadWhat.TITLE:
        return driver.title[: request.max_chars]
    if what is WebReadWhat.TEXT:
        _top(driver)
        text = driver.execute_script(PAGE_TEXT_JS)
        return (text if isinstance(text, str) else "")[: request.max_chars]
    if request.target is None:
        raise BrowserElementNotFoundError(ELEMENT_NOT_FOUND_TEXT)
    with _located(driver, listing, request.target) as element:
        if what is WebReadWhat.ELEMENT_TEXT:
            shown: str = element.text or (element.get_attribute("textContent") or "")
            return shown[: request.max_chars]
        value = element.get_attribute(request.name or "")
        return (value or "")[: request.max_chars]


def _wait_satisfied(
    driver: WebDriver,
    listing: PageListing,
    target: WebElementTarget,
    state: WebWaitState,
) -> bool:
    try:
        element = _find(driver, listing, target)
        if state is WebWaitState.PRESENT:
            return element is not None
        if state is WebWaitState.VISIBLE:
            return element is not None and element.is_displayed()
        return element is None or not element.is_displayed()
    except StaleElementReferenceException:
        return state is WebWaitState.GONE
    finally:
        _top(driver)


def page_wait(
    driver: WebDriver,
    listing: PageListing,
    target: WebElementTarget,
    state: WebWaitState,
    timeout_ms: int,
) -> str:
    """Waits until ``target`` is ``state`` or ``timeout_ms`` passed."""
    wait = WebDriverWait(
        driver,
        max(timeout_ms, 0) / 1000,
        poll_frequency=_POLL_SECONDS,
        ignored_exceptions=(NoSuchElementException, NoSuchFrameException),
    )
    try:
        wait.until(lambda d: _wait_satisfied(d, listing, target, state))
    except TimeoutException as e:
        raise ExecutionError(_WAIT_TIMEOUT_TEXT) from e
    return f"Element is {state.value}"
