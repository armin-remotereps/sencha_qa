"""JavaScript evaluate of the web browser layer (WEB_BROWSER_V1).

The script runs as a function body (``arguments`` = the decoded
``args_json`` list) through WebDriver ``execute_script``, bounded by
``set_script_timeout`` (at most WEB_TIMEOUT_MS_MAX, below the answer
budget; a page whose main thread never yields is bounded by the session's
HTTP timeout instead). A wrapper around it serialises the result to JSON in
the page, on the runner's browser: DOM nodes become ``{tag, id, text≤100}``;
cycles, functions, symbols and undefined become null; a returned promise is
awaited. The JSON is then capped at WEB_EVAL_RESULT_MAX_BYTES (UTF-8).

A thrown error or a timeout raises WebScriptFailedError with the JavaScript
error class name only. The script and its result may hold test credentials:
never log them above debug.
"""

import json
import logging
import re
from typing import Final

from selenium.common.exceptions import (
    JavascriptException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.remote.webdriver import WebDriver

from controller_client.exceptions import WebScriptFailedError
from controller_client.protocol import (
    WEB_ELEMENT_TEXT_MAX_CHARS,
    WEB_EVAL_RESULT_MAX_BYTES,
    WEB_TIMEOUT_MS_MAX,
)
from controller_client.web_browser_backend import WebEvalResult

logger = logging.getLogger(__name__)

# The script timeout of a page session between evaluate calls (desktop
# sessions start with it; evaluate puts it back after its own timeout).
WEB_SCRIPT_TIMEOUT_SECONDS: Final[float] = 15.0
_TIMEOUT_CLASS: Final[str] = "ScriptTimeout"
_FALLBACK_CLASS: Final[str] = "Error"
_ERROR_CLASS_RE: Final[re.Pattern[str]] = re.compile(r"\b([A-Z][A-Za-z]*Error)\b")
_SAFE_CLASS_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_$.]{1,64}")

_WRAPPER_HEAD: Final[str] = """
const __phTextMax = %d;
const __phSerialize = (value) => {
    const ancestors = [];
    return JSON.stringify(value, function (key, item) {
        if (item === undefined || typeof item === 'function' || typeof item === 'symbol') {
            return null;
        }
        if (typeof item === 'bigint') {
            return item.toString();
        }
        if (typeof window !== 'undefined' && item === window) {
            return null;
        }
        if (typeof Node !== 'undefined' && item instanceof Node) {
            return {
                tag: String(item.nodeName || '').toLowerCase(),
                id: item.id ? String(item.id) : '',
                text: String(item.textContent || '').replace(/\\s+/g, ' ').trim()
                    .substring(0, __phTextMax),
            };
        }
        if (typeof item !== 'object' || item === null) {
            return item;
        }
        while (ancestors.length > 0 && ancestors[ancestors.length - 1] !== this) {
            ancestors.pop();
        }
        if (ancestors.includes(item)) {
            return null;
        }
        ancestors.push(item);
        return item;
    });
};
const __phErrorName = (error) => {
    if (error && typeof error === 'object' && error.name) {
        return String(error.name);
    }
    return 'Error';
};
const __phDone = (value) => {
    const text = __phSerialize(value);
    return {ph_ok: text === undefined ? 'null' : text};
};
const __phUser = function () {
""" % (WEB_ELEMENT_TEXT_MAX_CHARS,)
_WRAPPER_TAIL: Final[str] = """
};
let __phValue;
try {
    __phValue = __phUser.apply(null, arguments);
} catch (error) {
    return {ph_error: __phErrorName(error)};
}
if (__phValue && typeof __phValue.then === 'function') {
    return Promise.resolve(__phValue).then(
        (value) => __phDone(value),
        (error) => ({ph_error: __phErrorName(error)}));
}
return __phDone(__phValue);
"""


def _safe_class(name: str) -> str:
    return name if _SAFE_CLASS_RE.fullmatch(name) else _FALLBACK_CLASS


def _class_from_message(message: str) -> str:
    match = _ERROR_CLASS_RE.search(message)
    return match.group(1) if match else _FALLBACK_CLASS


def _cap(json_text: str) -> WebEvalResult:
    data = json_text.encode("utf-8")
    if len(data) <= WEB_EVAL_RESULT_MAX_BYTES:
        return WebEvalResult(json_text=json_text, truncated=False)
    cut = data[:WEB_EVAL_RESULT_MAX_BYTES].decode("utf-8", errors="ignore")
    return WebEvalResult(json_text=cut, truncated=True)


def _fallback_json(value: object) -> str:
    """A script that left the wrapper (returned past its own closing brace):
    serialise what the driver gave back, dropping what JSON cannot hold."""
    return json.dumps(value, ensure_ascii=False, default=lambda _item: None)


def _restore_script_timeout(driver: WebDriver) -> None:
    try:
        driver.set_script_timeout(WEB_SCRIPT_TIMEOUT_SECONDS)
    except WebDriverException:
        logger.debug("Could not restore the script timeout", exc_info=True)


def evaluate_script(
    driver: WebDriver, script: str, args_json: str, timeout_ms: int
) -> WebEvalResult:
    """Runs ``script`` in the current page; WebScriptFailedError on a JS
    error or timeout."""
    decoded = json.loads(args_json) if args_json.strip() else []
    args: list[object] = decoded if isinstance(decoded, list) else []
    # Capped again here: the hub's answer budget must cover the script.
    driver.set_script_timeout(min(max(timeout_ms, 1), WEB_TIMEOUT_MS_MAX) / 1000)
    logger.debug("Running a page script (%d chars)", len(script))
    # The session's own script timeout is put back whenever the driver
    # answered; after a transport error (no answer) the session is dropped.
    try:
        raw = driver.execute_script(_WRAPPER_HEAD + script + _WRAPPER_TAIL, *args)
    except TimeoutException as e:
        _restore_script_timeout(driver)
        raise WebScriptFailedError(_TIMEOUT_CLASS) from e
    except JavascriptException as e:
        _restore_script_timeout(driver)
        # A syntax error stops the whole wrapper before it can catch.
        raise WebScriptFailedError(_class_from_message(e.msg or "")) from e
    except WebDriverException:
        _restore_script_timeout(driver)
        raise
    _restore_script_timeout(driver)
    if isinstance(raw, dict):
        error = raw.get("ph_error")
        if isinstance(error, str):
            raise WebScriptFailedError(_safe_class(error))
        result = raw.get("ph_ok")
        if isinstance(result, str) and len(raw) == 1:
            return _cap(result)
    return _cap(_fallback_json(raw))
