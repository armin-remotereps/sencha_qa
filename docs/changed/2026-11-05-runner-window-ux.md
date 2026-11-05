# Runner window UX: dark theme, section rail, Manual sub-tabs, chat bubbles

Date: 2026-11-05
Plan: docs/plan/feat-runner-window-ux.md

## What changed
- `controller_client/person_window_theme.py` (new): one dark palette (web design-system tokens), named fonts and `apply_dark_theme(root)` (ttk "clam" + option database), called in `_open_tk` before any widget. The five window modules lost their own colour constants.
- `controller_client/person_window_scroll.py` (new): `ScrollPage` (moved from the app) with a right-hand section rail (`add_section`, `scroll_to`, `refit_height`) and the `SectionPage` protocol. Sections on Step, Test case, Discussion and the mark sheet. One app-level debounced refresh (`PersonWindowApp._schedule_section_refresh`).
- `controller_client/person_window_manual.py`: the Manual run tab is a fixed case bar (upload line, case title, Pause / Release, ◀ step ▶, Pass…, Fail…, Hand in…, statuses) over sub-tabs Cases, Preconditions & steps, Current step, Discuss, Result. The ▸/▾ Discuss toggle is gone; the Discuss title shows the message count. The claim, capture, pause and hand-in logic is unchanged.
- `controller_client/chat_markdown.py` (new): pure, stdlib-only, linear-time Markdown subset → segments (closed enums, never Tk tags).
- `controller_client/person_window_chat.py`: `ChatPanel` shows bubbles (agent left, people right, notices centred) with Markdown, for both the Discussion tab and Manual Discuss. Links are clickable only for strict http(s) URLs (`_web_url`); a link whose text differs from its URL shows " (host)"; other links show as plain "text (url)".
- `person_window_diagnostics.py`: Markdown sample messages; `--self-check` also visits the Manual sub-tabs and jumps to every rail section.
- `controller_client/protocol.py`: `CLIENT_VERSION` 0.18.1.

## Why
Testers found the light window hard on the eyes, long pages hard to scroll, the Manual run tab too long and the agent chat hard to follow.

## Behaviour and contract changes
- Test Runner 0.18.1. No protocol, IPC, capability or server change. The server image must still be redeployed so it offers the self-update: it offers only to older runners.
- Hand in… in the case bar is enabled without a verdict; the validation message selects the Result sub-tab.
- Chat links open the default browser only for ASCII http(s) URLs with a host, without backslash, user info (`@` in the host part) or invisible / control characters.

## Notes for future changes
- Not yet looked at on a real screen. The checks were mypy, the self-check, and scripts that read back colours and geometry. Check it on Windows (clam padding vs the rail-width estimate) and Linux.
- Rail: the frame has `pack_propagate(False)` and the column `minsize` reserves its width. Letting the rail size the page reintroduces a show/hide relayout loop.
- A person's bubble indents nested lists and quotes with spaces, not margins: Tk paints the whole left margin in one colour.
- `chat_markdown` treats `\` before punctuation as an escape, so Windows paths outside code spans can lose a backslash.
- `person_window_app._web_url` (manual-run links from the server) still checks only the scheme. The chat uses its own stricter check.
