"""Page scripts of the web browser layer (WEB_BROWSER_V1).

They run through WebDriver ``execute_script`` (any Selenium or Appium page
context) and stamp the layer's own attributes: ``data-ph-idx`` on listed
elements and ``data-ph-frame`` on the same-origin iframes a listing visits.
They never touch the Playwright layer's ``data-at-idx``.
"""

from typing import Final

# Lists the visible interactive elements of the current document, across open
# shadow roots, and stamps each with ``data-ph-idx``. Also stamps the visible
# same-origin iframes (one level, ``data-ph-frame``) so the caller can switch
# into them and list them with the same script.
#
# arguments[0]: first index; arguments[1]: how many elements at most;
# arguments[2]: scope CSS selector or null; arguments[3]: text cap.
# Returns {elements: [...], frames: <number of stamped iframes>}, or
# {error: "selector"} when the scope selector is invalid.
LIST_ELEMENTS_JS: Final[str] = """
const startIndex = arguments[0];
const maxCount = arguments[1];
const scopeCss = arguments[2];
const textMax = arguments[3];
const SELECTORS = 'a[href], button, input, select, textarea, summary, '
    + '[role], [onclick], [contenteditable=""], [contenteditable="true"], '
    + '[tabindex]:not([tabindex="-1"])';

const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
        && style.visibility !== 'hidden'
        && style.opacity !== '0'
        && rect.width > 0
        && rect.height > 0;
};

const clean = (value, max) => String(value || '')
    .replace(/\\s+/g, ' ').trim().substring(0, max);

let roots = [document];
if (scopeCss) {
    try {
        roots = Array.from(document.querySelectorAll(scopeCss));
    } catch (e) {
        return {error: 'selector'};
    }
}

// Drop stamps of an earlier listing, so a stale index never matches.
const unstamp = (root) => {
    root.querySelectorAll('[data-ph-idx]').forEach(el => el.removeAttribute('data-ph-idx'));
    root.querySelectorAll('*').forEach(el => {
        if (el.shadowRoot) {
            unstamp(el.shadowRoot);
        }
    });
};
unstamp(document);
document.querySelectorAll('[data-ph-frame]').forEach(
    el => el.removeAttribute('data-ph-frame'));

const seen = new Set();
const candidates = [];
const walk = (root) => {
    root.querySelectorAll('*').forEach(el => {
        if (el.matches(SELECTORS) && !seen.has(el)) {
            seen.add(el);
            candidates.push(el);
        }
        if (el.shadowRoot) {
            walk(el.shadowRoot);
        }
    });
};
roots.forEach(root => {
    if (root !== document && root.matches && root.matches(SELECTORS) && !seen.has(root)) {
        seen.add(root);
        candidates.push(root);
    }
    walk(root);
});

const elements = [];
for (const el of candidates) {
    if (elements.length >= maxCount) {
        break;
    }
    if (!isVisible(el)) {
        continue;
    }
    const idx = startIndex + elements.length;
    el.setAttribute('data-ph-idx', String(idx));
    const value = (el.value !== undefined && el.type !== 'password')
        ? clean(el.value, 50) : '';
    elements.push({
        idx: idx,
        tag: el.tagName.toLowerCase(),
        role: clean(el.getAttribute('role'), 40),
        text: clean(el.innerText || el.textContent, textMax),
        label: clean(el.getAttribute('aria-label'), textMax),
        placeholder: clean(el.getAttribute('placeholder'), textMax),
        type: clean(el.getAttribute('type'), 40),
        name: clean(el.getAttribute('name'), 80),
        id: clean(el.id, 80),
        href: clean(el.getAttribute('href'), 200),
        value: value,
    });
}

let frames = 0;
document.querySelectorAll('iframe, frame').forEach(frame => {
    let sameOrigin = false;
    try {
        sameOrigin = frame.contentDocument !== null;
    } catch (e) {
        sameOrigin = false;
    }
    if (sameOrigin && isVisible(frame)) {
        frame.setAttribute('data-ph-frame', String(frames));
        frames += 1;
    }
});
return {elements: elements, frames: frames};
"""

# Stamps the visible same-origin iframes of the current document (one level)
# with ``data-ph-frame`` and returns how many there are. Used by CSS / XPath
# lookups, which search the top document first and then these frames.
STAMP_FRAMES_JS: Final[str] = """
const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
        && rect.width > 0 && rect.height > 0;
};
document.querySelectorAll('[data-ph-frame]').forEach(
    el => el.removeAttribute('data-ph-frame'));
let frames = 0;
document.querySelectorAll('iframe, frame').forEach(frame => {
    let sameOrigin = false;
    try {
        sameOrigin = frame.contentDocument !== null;
    } catch (e) {
        sameOrigin = false;
    }
    if (sameOrigin && isVisible(frame)) {
        frame.setAttribute('data-ph-frame', String(frames));
        frames += 1;
    }
});
return frames;
"""

# Finds the element stamped ``data-ph-idx`` = arguments[0] in the current
# document, open shadow roots included; null when it is gone.
FIND_INDEXED_JS: Final[str] = """
const wanted = String(arguments[0]);
const search = (root) => {
    const hit = root.querySelector('[data-ph-idx="' + wanted + '"]');
    if (hit) {
        return hit;
    }
    for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) {
            const found = search(el.shadowRoot);
            if (found) {
                return found;
            }
        }
    }
    return null;
};
return search(document);
"""

# Scrolls arguments[0] into the middle of the viewport before a pointer action.
SCROLL_INTO_VIEW_JS: Final[str] = (
    "arguments[0].scrollIntoView({block: 'center', inline: 'center'});"
)

# Focuses arguments[0] before a key combination is sent to it.
FOCUS_JS: Final[str] = "arguments[0].focus();"

# The visible text of the page (body innerText), "" without a body.
PAGE_TEXT_JS: Final[str] = (
    "return document.body ? (document.body.innerText || '') : '';"
)
