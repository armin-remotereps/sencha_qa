# feat: Runner window UX (dark theme, section rail, chat bubbles with Markdown)

Status: Done
Created: 2026-11-05
Request: Three UX changes to the Test Runner's native person window (Tk). (1) A
black/dark theme, always on. (2) Long pages are hard to scroll: give each scrollable
page a column of buttons, one per section, that jumps straight to that section.
(3) The Discussion chat (help case and Manual run discuss) is hard to follow: show
it as chat bubbles and render the messages' Markdown. Follow-up from the user: the
Manual run tab is split into sub-tabs (Discuss, preconditions and steps, the run's
test cases, ...) instead of one long page. Controller client only.

## Context

- Window process: `controller_client/person_window_app.py` (`PersonWindowApp`,
  `_ReadOnlyBlock`, `_ScrollPage`, `_open_tk`, `_self_check`, `_apply_text_size`,
  `_on_wheel` / `_page_of`, `_refit`). Tabs Step, Test case, Discussion and Manual
  run are `_ScrollPage`s (canvas + inner frame, scrollbar shown only when the
  content does not fit); Log is a plain frame with one fixed-height block.
- Shared chat: `controller_client/person_window_chat.py` (`ChatPanel`,
  `chat_segments`, append-only `render_rows`), used by the Discussion tab and the
  Manual run tab's Discuss (`person_window_manual.ManualView._build_discuss`).
  Rows are `PersonDiscussionMessagePayload` (`kind` USER / ASSISTANT / NOTICE,
  server-built `author`, `text`, `created_at`) from `controller_client/protocol.py`.
- Manual run tab: `person_window_manual.py` builds into `self._manual_page.frame`.
  The mark sheet (`person_window_mark_sheet.py`) is a frame placed over the root
  with its own scroll area from `ManualHost.manual_scroll_area` (also a
  `_ScrollPage`, registered in `_pages` for the wheel).
- Palette is duplicated as light-theme constants (`_WARN_COLOR` #b45309,
  `_ERROR_COLOR`, `_OK_COLOR`, `_MUTED_COLOR`, `_COVERED_*`, `_CURRENT_*`) in
  `person_window_app.py`, `person_window_chat.py`, `person_window_manual.py`,
  `person_window_mark_sheet.py`, `person_window_mark_session.py`; font names
  `PunkHazardHeading` / `PunkHazardBold` are created in `PersonWindowApp.__init__`
  and scaled by `_apply_text_size` (A−/A+).
- No ttk theme is set today: macOS uses aqua (ignores most colours), Windows vista.
  `tk.Text` boxes use `relief="solid"`, which draws a near-black border.
- `--self-check` renders the commands `person_window_diagnostics` pipes in
  (`_sample_messages`, `_sample_manual_discussion`, ...), visits every tab and both
  text sizes, opens the mark sheet, prints `rendered: ...`; the diagnostics parse
  only exit code and that line.
- Dark tokens: `static/css/punk-hazard.css` (page #0b1118, surface #131d28,
  raised #1a2733, hover #223240, border #2d3d4b, border-strong #3d5060, text
  #f5f8fa, muted #9baab6, placeholder #6d7d8a, success #2fbf71, warning #f2b84b,
  danger #ef5b62, info #4da3ff, cyan #2ecae4, code bg #0a0f15, code text #c8d6e0;
  the `*-soft` tints are rgba and must be pre-blended for Tk).
- `CLIENT_VERSION` is 0.18.0 (`controller_client/protocol.py`). The server offers a
  self-update only when the runner is older (`projects/runner_update_offer.runner_is_older`),
  and the runner installs into `test-runner-<version>/`.

## Goals
- Dark theme on every widget of the window, the mark sheet and the combobox popdown,
  from one palette module; no light flash at first show.
- A per-page section rail that jumps a page to a section (Step, Test case,
  Discussion, mark sheet); generic API on the scroll page.
- The Manual run tab as a fixed case bar (Pass… / Fail… / Hand in…, step nav,
  Pause / Release, statuses) over sub-tabs Cases, Preconditions & steps, Current
  step, Discuss, Result.
- Chat shown as bubbles (agent left, people right, notices centred) with safe
  Markdown rendering, in both the Discussion tab and the Manual run Discuss
  sub-tab (one shared `ChatPanel`); append-only rendering, Copy and A−/A+ keep working.

## Non-goals
- Light theme or a theme setting; following the OS appearance.
- The native title bar colour (Windows DWM dark title bar, macOS appearance).
- Highlighting the section currently in view, keyboard shortcuts for the rail.
- Markdown anywhere but the chat (step texts, case texts and log stay plain).
- Any server, wire-protocol or IPC change.

## Design

### Data model
None.

### Modules (controller_client only)

New:
- `controller_client/person_window_theme.py` (tkinter import guarded like the
  other window modules). The palette as `Final[str]` constants with semantic
  aliases (`WARN`, `ERROR`, `OK`, `MUTED`, `TEXT`, `SURFACE`, `RAISED`, `PAGE`,
  `BORDER`, `CODE_BG`, `CODE_TEXT`, `CYAN`, `INFO`, ...), pre-blended tints
  (`HIGHLIGHT_BG` for the covered / current step: warning ~30 % over surface,
  text colour on top; `USER_BUBBLE_BG`: cyan ~20 % over the text background;
  `AGENT_BUBBLE_BG` = raised), the font-name constants (`HEADING_FONT`,
  `BOLD_FONT`, plus new `ITALIC_FONT`, `BOLD_ITALIC_FONT`, `SUBHEADING_FONT`) and
  one function `apply_dark_theme(root: tk.Tk) -> None`.
- `controller_client/person_window_scroll.py`: `_ScrollPage` moved out of
  `person_window_app.py` as `ScrollPage` (the app file is 2 600+ lines) plus the
  section rail. Exposes a small Protocol `SectionPage` (`outer`, `frame`,
  `add_section(label, *widgets)`) that `person_window_manual` and
  `person_window_mark_sheet` type against.
- `controller_client/chat_markdown.py`: stdlib only, no tkinter, pure functions.
  `render_markdown(text: str) -> tuple[MdSegment, ...]` where a segment is
  `(text, inline styles, line style, indent level)` from a closed vocabulary
  (enums), never Tk tag names. No regex with nested quantifiers (linear scan),
  nothing loaded, nothing executed.

Changed: `person_window_app.py`, `person_window_chat.py`,
`person_window_manual.py` (case bar + sub-tabs, section 2b), `person_window_mark_sheet.py`,
`person_window_mark_session.py`, `person_window_diagnostics.py` (samples),
`protocol.py` (`CLIENT_VERSION` only).

### 1. Dark theme

- `apply_dark_theme` is called from `_open_tk()` right after `tk.Tk(...)`, before
  `withdraw`/any widget, so the option database and styles are in place for every
  widget (all callers: run, `--probe`, `--self-check`, `--probe-shortcuts`), and the
  first map is already dark.
- `ttk.Style().theme_use("clam")` on every platform (aqua and vista ignore
  colours). `style.configure` / `style.map` for `.` (defaults), TFrame, TLabel,
  TButton (active / pressed / disabled, focus colour cyan), TNotebook and
  TNotebook.Tab (selected vs not), TCombobox (incl. `readonly` fieldbackground,
  selection colours, arrow), TEntry (incl. `readonly`, insert colour), TScrollbar
  (trough, thumb, arrow, border/light/dark colours), TCheckbutton, TRadiobutton
  (indicator colours), and a `Rail.TButton` style for the rail (flat, muted text,
  anchored west, smaller padding).
- `root.configure(background=SURFACE)`; `root.option_add` for `*Text`, `*Canvas`,
  `*Listbox` and `*TCombobox*Listbox` (background, foreground, insertbackground,
  selectbackground/foreground, inactiveselectbackground, highlightbackground /
  highlightcolor).
- Text boxes: `_ReadOnlyBlock` and `_entry_text` switch from `relief="solid"` to
  `relief="flat"` with a 1 px highlight border (BORDER, CYAN when focused); read-only
  blocks on PAGE, editable boxes on PAGE too (the web inputs), pages on SURFACE.
- All five files drop their own colour/font constants and import from the theme
  module; `covered` (Test case tab) and `current` (Manual overview) use
  `HIGHLIGHT_BG` with `TEXT`. Every explicit `foreground=` stays as is but points at
  the new constants (they are already semantic: warn/error/ok/muted).
- `PersonWindowApp.__init__` creates the three new named fonts beside the existing
  two (same family), and `_apply_text_size` scales them (subheading size + 1).
- `_ScrollPage` already reads the TFrame background for its canvas; with the style
  set first it gets SURFACE.

### 2. Section rail (Step, Test case, Discussion, mark sheet)

- `ScrollPage` gets a third grid column right of its scrollbar holding the rail (a
  `ttk.Frame` of `Rail.TButton`s, `takefocus=False`). A page that never registers a
  section has no rail column at all (the Manual run sub-tabs, see 2b).
- API: `add_section(label: str, *widgets: tk.Misc) -> None` registers a section in
  page order; several widgets mean "whichever of these is shown" (the Step tab's
  buttons / Different / Confirm frames share one row). `refresh_sections()` is
  scheduled (one `after` job, ~100 ms debounce) from the content `<Configure>` and
  from the end of `PersonWindowApp._render` (visibility can change without a size
  change, e.g. frames swapped in the same grid row); it rebuilds the buttons only
  when the tuple of shown labels changes.
- "Shown" is decided without relying on the window being mapped (it is withdrawn
  most of the time): a widget counts when it and every ancestor up to the page frame
  has a geometry manager (`winfo_manager() != ""`; `grid_remove` clears it) and a
  non-zero requested height. The jump target is the sum of `winfo_y()` from the
  widget up to the page frame (after `update_idletasks`), divided by the frame's
  height, passed to `canvas.yview_moveto` (Tk clamps the end).
- The rail's buttons are hidden while the page fits (same signal that hides the
  scrollbar in `_on_canvas_scroll`) or when fewer than two sections are shown, but
  the rail column of a page with sections keeps a `minsize` (widest label in the
  current font + padding, recomputed after A−/A+). Reason: collapsing the column
  would change the requested width, the notebook width, every wraplength and the
  content height, which can flip the rail on and off in a loop. `_TEXT_INSET_PX`
  (wrap estimate) grows by the rail width for those pages.
- Wheel: `_page_of` matches the page's `outer` path (so the wheel over the rail
  scrolls the page) with an exact-or-`path + "."` prefix test (today's bare
  `startswith` would match `.!frame2` for `.!frame20`).
- Sections (short labels; implementer may tune):
  - Step: Step (picker + step line), What to do, Case steps, Expected, Objection,
    Command, Output, Note, Actions (buttons / Different / Confirm), Link.
  - Test case: Preconditions, Steps, Expected.
  - Discussion: Reason, Chat, Message (`ChatPanel` exposes its message-box frame as
    `message_frame`), Proposal, Plan, Decide (resolve frame), Link.
  - Mark sheet (`ManualHost.manual_scroll_area` returns a `SectionPage` instead of
    a tuple): Step, Shots, Note, Hand in, Save.
  - Log tab: none (single fixed-height block, not a scroll page).
- Why not sub-tabs for Step / Test case / Discussion too: the Step tab is one
  top-to-bottom flow (read what to do and the expected result, then answer below
  it); splitting it would hide the expected result from the answer buttons. The
  Test case tab has three blocks. The Discussion tab is the closest candidate
  (chat vs decide), but the person decides from what the chat says, and its
  Continue panel is short; the rail keeps both on one page. Revisit if users ask.

### 2b. Manual run tab: fixed case bar + sub-tabs

Today the Manual run tab is one long `ScrollPage`: upload line, case list + Start,
then the claimed case (title, Pause / Release, paused banner, preconditions, steps
overview, ◀ picker ▶, step text, expected, mark status, thumbnails + Edit, Pass… /
Fail… + shortcut line, step note, capture status, collapsible Discuss, Case result
with verdict / note / case screenshot / Hand in, inline confirmation, submit
status). New layout (all in `person_window_manual.py`; the top-level Manual run
tab becomes a plain `ttk.Frame`, no longer a `ScrollPage`):

- **Case bar** (fixed, above the sub-notebook, never scrolls), so the actions the
  tester uses all the time are always reachable:
  - the upload line (as today);
  - while a case is claimed: case title (heading font) with Pause / Continue
    testing and Release case on the right; the paused banner and its one-line
    explanation, the pause blocker and pause status (each only when non-empty);
  - the action row: the existing nav (◀ Previous, step picker, Next ▶), Pass…,
    Fail… and **Hand in…**;
  - one status area: capture status, submit status and the inline confirmation
    frame (release / contradicting hand-in), so feedback for a bar button is shown
    next to it whatever sub-tab is selected.
- **Sub-notebook** (nested `ttk.Notebook`), each sub-tab its own `SectionPage`
  created through `host.manual_scroll_area` (so the window's wheel handling covers
  it); no rail sections registered:
  1. **Cases**: case list, Start this case, list status.
  2. **Preconditions & steps**: Preconditions block (hidden when empty) and the
     steps overview with the current-step highlight and Pass/Fail marks
     (`_OVERVIEW_LINES` can grow, e.g. 20, now that it has its own page).
  3. **Current step**: step text ("Step n of N"), Expected result, mark status,
     thumbnails + Edit…, the shortcut status line, the step note.
  4. **Discuss**: the explanatory line, the `ChatPanel` (bubbles + Markdown, as in
     the Discussion tab), Open on Punk Hazard. Replaces the ▸/▾ toggle
     (`_DISCUSS_OPEN/_CLOSED`, `_discuss_open`, `_toggle_discuss` go away); the
     message count and "answering…" move into the sub-tab title, e.g.
     "Discuss (3)" / "Discuss (3) …". Selecting it scrolls the chat to the end
     (what opening the toggle did).
  5. **Result**: verdict radios, case note, case screenshot, the "not marked steps
     are reported as not run" hint. Hand in itself is the bar's button.
- Visibility (hidden with `notebook.hide`, like the top-level tabs, rather than
  disabled): no claimed case → only Cases; claimed → Cases, Preconditions & steps,
  Current step, Result, and Discuss only when the server sends `discussion` (an
  older server leaves it hidden). The case bar's claimed-case rows hide with the
  claim.
- Selection rules (`ManualView` owns them; never steals focus, only selects):
  a new claim (claim token changes in `_load_claim`) selects Current step; the
  claim ending selects Cases; Hand in… whose `validation_error` names a step
  selects Current step (as today it `go_to`s the step and focuses its note),
  otherwise (verdict missing, case note required / too long) selects Result;
  `show_hand_in_error` follows the same rule. A selected sub-tab that becomes
  hidden falls back to Current step, else Cases. Otherwise the tester's choice
  persists across snapshots.
- Hand in… in the bar runs the existing `_ask_submit` path unchanged (validation,
  contradiction confirmation, `submit`), so there is still exactly one hand-in
  path; it is enabled under the same conditions as today's Hand in.
- Mark sheet: unchanged placement. It is `place`d over the whole root, so it covers
  the window regardless of the selected sub-tab; a shortcut or Pass… / Fail… /
  Edit… opens it from any sub-tab and no switch is needed. After Save the sub-tab
  stays as it was (the bar's step picker shows the advanced step); a Save that
  hands in and auto-claims the next case lands on Current step through the
  new-claim rule.
- Height: each sub-page's maximum height must leave room for the case bar.
  `ScrollPage` takes the extra allowance (an `int` or a callable returning the
  bar's requested height) instead of only `_HEADER_ALLOWANCE`, so a long sub-page
  still scrolls instead of pushing the window off screen.
- `ManualView.__init__` keeps `(host, root, frame)`; `frame` is now the plain
  top-level tab frame. New `ManualView.subtab_frames()` for the self-check.
  `_render_*` methods are reorganised by sub-tab but keep their logic; the draft,
  capture, claim, pause and chat state machines are untouched.

### 3. Chat bubbles and Markdown

- Applies to both users of `ChatPanel`: the top-level Discussion tab (help case)
  and the Manual run Discuss sub-tab. No per-caller code.
- Bubbles stay inside the one read-only `tk.Text` (selection across messages, Copy,
  and append-only rendering keep working). Side by kind: ASSISTANT left, USER right
  (every person, not only the one at this runner: the protocol has no "me" flag and
  the server-built `author` is shown in the header), NOTICE centred, muted, no
  bubble.
- Each bubble: a header line "author · 14:05" (bold; cyan for people, muted text
  colour for the agent), then the rendered body; the bubble's last newline is
  tagged so the background reaches the bubble's right edge; one untagged blank line
  between bubbles. Left bubble: small left margin, right margin ~18 % of the text
  width; right bubble mirrored. Inner vertical padding via `spacing1` on the header
  and `spacing3` on the last body line.
- Tk gotcha (confirmed in the Tk 8.6 text man page): `-lmargincolor` /
  `-rmargincolor` default to the tag's `-background`, so the margins must set
  them to the text background explicitly or the whole width is painted. Both need
  Tk ≥ 8.6.6; on a `TclError` the panel configures the tags without them (bubbles
  then span the full width, still readable).
- Tag layering in `ChatPanel.__init__`, created in this order so priority is
  deterministic (later wins): bubble background tags (`bubble_agent`,
  `bubble_user`), composite margin tags per side × line style × indent
  (`agent:list2`, `user:quote`, `user:pre`, ...: side margin + indent, `lmargin2`
  hang for list items), inline tags (bold, italic, bold-italic, subheading/heading
  fonts, link URL muted), then code tags (`code`: TkFixedFont, CODE_BG / CODE_TEXT;
  `pre` the same for fenced lines) last so code background wins over the bubble.
  Margins are pixels computed from the text width and `TkTextFont` measures;
  `ChatPanel.relayout()` reconfigures the margin tags (no re-insert) on the Text's
  `<Configure>` (fires when the width changes and when A−/A+ changes the fixed-line
  height).
- `chat_segments(message, first)` keeps its role but returns bubble-tagged
  segments built from `render_markdown(message.text)` for USER and ASSISTANT;
  NOTICE and the empty-state text stay plain. `render_rows` is unchanged in
  structure (append when only new rows came, else full re-render).
- Markdown subset (everything else plain text, characters kept):
  - `#`, `##`, `###` headings (heading / subheading / bold fonts), `#` without a
    following space is not a heading.
  - `**bold**`, `__bold__`, `*italic*`, `_italic_`, `***both***`; `_`/`*` only at
    word boundaries so `snake_case`, paths and `2*3*4` stay as typed; `\*` escapes.
  - `` `code` `` spans (no formatting inside); fenced ``` / ~~~ blocks (language
    word dropped, an unterminated fence runs to the end) rendered mono on CODE_BG;
    lines starting with `|` (tables) rendered mono without background so columns
    line up.
  - `-`, `*`, `+` bullets shown as "•", `1.` / `1)` kept; up to three nesting
    levels by leading spaces; `>` quotes indented with muted text.
  - `[text](url)` shown as "text" with a link tag; bare `http://` / `https://`
    URLs also get the link tag. Links are clickable only when the URL is
    http/https (`_web_url`): a click opens the default browser through the same
    path as the existing "Open" buttons (`ChatHost.chat_open_url`, backed by
    `PersonWindowApp._open_url`). Any other scheme is shown as plain text, never
    opened. Link tag: info colour, underline, hand cursor on enter/leave.
  - `---`, `***`, `___` alone on a line become a muted line of "─".
- Copy copies the rendered text (bullets, link text, code without fences):
  readable, no Markdown markers.

### Settings / infrastructure
- No new settings or env vars (theme always on).
- `CLIENT_VERSION` 0.18.0 → **0.18.1**. Not a protocol change, but required to
  ship it: the server offers the self-update only to older runners and installs
  into a per-version folder. No capability added; the server needs no code
  change (it reads the shared `protocol.py`). Mention in the changed note that the
  server image must be redeployed for the update to be offered.

### Data flow
Unchanged: runner → IPC `StateCommand` / `ManualStateCommand` → `PersonWindowApp.apply`
→ `_render` → tabs → `ChatPanel.render_rows` → `chat_segments` → `render_markdown`
→ Text inserts with bubble/markdown tags (same for the Discussion tab and the
Manual run Discuss sub-tab). `ManualView.render` fills the case bar and the
visible sub-tabs and applies the visibility / selection rules. `_render` ends by scheduling each page's
section refresh. A rail click → `ScrollPage.scroll_to(section)` → `canvas.yview_moveto`.

## Alternatives considered
- Bubbles as embedded `ttk.Frame`/`Label` windows in the Text (`window_create`) or a
  canvas of frames: real padding and rounded look, but loses cross-message
  selection and Copy, needs per-message wrap handling and is heavier per row. Tags
  in one Text are the simplest design that keeps every existing behaviour.
- Rail on the left, or tabs-within-tabs: the right side keeps the content's left
  edge where it is today and sits next to the scrollbar it replaces in practice.
- Manual run tab with only a section rail (the first draft): the user asked for
  sub-tabs; they also cut the page to what the tester needs at each moment. The
  fixed case bar is what keeps Pass / Fail / Hand in reachable from every sub-tab.
- Mark sheet as a sub-tab: rejected; it must cover the whole window (it is what a
  shortcut opens over whatever the tester was doing, and it hides with the window
  for screenshots), so it stays a placed overlay.
- Rail that fully collapses when the page fits: rejected for the relayout loop
  above; the reserved column is ~90 px.
- A Markdown library (markdown-it-py, mistune): new dependency in the runner venv
  and the update bundle for a small subset that must map to Tk tags anyway.
- Keeping colours per file: the duplication is exactly why "dark" would need five
  coordinated edits; one palette module is the minimal abstraction added.

## Risks and mitigations
- Tk version differences (margin colours ≥ 8.6.6; `clam` element options differ
  slightly between 8.6 and 9.0): guarded configure with fallback; check on macOS
  python.org Tk, Windows and a Linux distro Tk.
- Wrapped lines inside a bubble may leave a ragged right background edge on some Tk
  builds: accept, or verify visually and tune `rmargin`; not worth embedded
  windows.
- Readability of status colours on dark (amber/red/green on #131d28) and of the
  covered/current highlight: use the web tokens, which are designed for dark.
- Relayout loops from the rail: reserved column width (see Design).
- Markdown parser on long or hostile text: linear scan, no backtracking regex,
  bounded nesting; inserts only text with tags (no `eval`, images or widgets), so
  no injection surface. `PERSON_CHAT_TEXT_MAX_CHARS` already bounds input.
- Append-only chat after the change: bubble and margin tags are static, so
  appending a message never touches earlier ranges; full re-render path unchanged.
- Window becomes ~90 px wider by default (rail): within `_MIN_WIDTH`; check a
  1366×768 screen.
- Manual case bar height on small screens (title, paused banner, nav + actions,
  statuses): keep each row to one line where possible, the paused explanation
  muted and short; sub-pages scroll within what is left (allowance in
  `ScrollPage`). Check at 1366×768 with A+ twice.
- Manual run regressions: the tab's state machines (draft flush, capture, claim,
  auto-claim, pause, hand-in timeout) are moved, not changed; the verifier should
  diff behaviour, not only layout. `show_hand_in_error` / `go_to` must still land
  the tester on the failing step's note.
- Focus rules: rail buttons `takefocus=False`; clicking them must not change the
  "never takes focus" behaviour beyond what any button click does today.

## Implementation steps
1. Palette and theme: create `person_window_theme.py`; call `apply_dark_theme` in
   `_open_tk`; replace the colour/font constants in the five window files; new
   named fonts in `PersonWindowApp.__init__` / `_apply_text_size`; Text boxes to
   flat + highlight border. (files: `person_window_theme.py`, `person_window_app.py`,
   `person_window_chat.py`, `person_window_manual.py`, `person_window_mark_sheet.py`,
   `person_window_mark_session.py`)
2. Scroll page and rail: move `_ScrollPage` to `person_window_scroll.py` as
   `ScrollPage` with the rail, the height allowance parameter and the `SectionPage`
   protocol; `_page_of` path fix; `_TEXT_INSET_PX`; refresh scheduling from
   `_render`; `manual_scroll_area` returns a `SectionPage`; the top-level Manual run
   tab becomes a plain frame. (files: `person_window_scroll.py`,
   `person_window_app.py`)
3. Register sections on Step, Test case, Discussion; `ChatPanel.message_frame`;
   mark sheet sections. (files: `person_window_app.py`, `person_window_chat.py`,
   `person_window_mark_sheet.py`)
4. Manual run case bar + sub-tabs: rebuild `ManualView._build` into the bar and
   five sub-pages, visibility and selection rules, Discuss title, removal of the
   Discuss toggle, `subtab_frames()`. Depends on 2. (files: `person_window_manual.py`)
5. Markdown renderer: `chat_markdown.py` (pure, typed). Independent; can be
   written in parallel with 1–4. (files: `chat_markdown.py`)
6. Bubbles: `ChatPanel` tags, `relayout`, `chat_segments` on top of
   `render_markdown`. Depends on 1 and 5. (files: `person_window_chat.py`)
7. Self-check and samples: give the sample assistant messages Markdown (heading,
   bullets with nesting, a fenced block, a link, bold/italic, `snake_case`) and make
   at least one page long enough to scroll; in `_self_check` also select every
   visible Manual run sub-tab (`subtab_frames()`), call every page's section
   refresh and jump to each registered section (failures recorded like render
   errors). Keep the `rendered: ...` output format. (files:
   `person_window_diagnostics.py`, `person_window_app.py`)
8. `CLIENT_VERSION` 0.18.1. (files: `protocol.py`)

Ordering: 1 → 2 → (3, 4 in parallel: different files) → 6; 5 in parallel from the
start; 7 and 8 last. Steps 1, 2, 3 and 7 all edit `person_window_app.py`, so they
run in sequence or in one agent.

## Verification
- `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg controller_client/`
  and `pre-commit run --all-files` clean.
- `python -m controller_client.diagnose --window` (pipes the samples into
  `python -m controller_client.person_window_app --self-check`): exit 0 and the
  usual `rendered: ...` line including "mark sheet".
- Quick REPL smoke of `chat_markdown.render_markdown` on: nested list, fenced block
  without closing fence, `snake_case_name`, `a * b * c`, `**bold _and italic_**`,
  `[x](https://e.x)`, a 100 kB line of `*` characters (returns quickly). Not
  committed (no tests in this project).
- Manual, on macOS and Windows (Linux if available), with a live runner:
  - Window, mark sheet, combobox popdown, case listbox, entries, scrollbars,
    checkbuttons, radio buttons and disabled buttons are all dark; no white flash
    at first show; covered/current steps readable.
  - Each scroll page: rail lists only shown sections, buttons hide when the page
    fits, clicking jumps the section to the top, the wheel over the rail scrolls the
    page, A−/A+ keeps the rail aligned, no flicker or width oscillation while
    resizing.
  - Manual run: without a claim only Cases shows; Start → Current step selected;
    Pass… / Fail… / Hand in… and ◀ ▶ work from every sub-tab; the shortcut and
    Edit… open the mark sheet over any sub-tab; Save + hand-in + auto-claim lands
    on Current step; a hand-in missing the verdict selects Result, a failed step
    without a note selects Current step with its note focused; Pause shows the
    banner in the bar and disables the bar actions; Discuss hidden on an older
    server sample (no `discussion`) and titled with the count when present.
  - Discussion and Manual Discuss: agent bubbles left, people right, notices
    centred; Markdown renders; new messages append without jumping when scrolled up;
    Copy yields readable text; A−/A+ scales bubble text, headings and code.
  - Self-update from 0.18.0 is offered and lands on 0.18.1.
- Run `reviewer` and `verifier` agents.

## Open questions
- Answered 2026-11-05: Q1 links clickable (http/https only, default browser).
  Q2 every person's message on the right (no server change). Q3 Hand in… in the
  case bar submits directly through `_ask_submit`.

## Changelog
- 2026-11-05: created
- 2026-11-05: Manual run tab split into a fixed case bar + sub-tabs (Cases,
  Preconditions & steps, Current step, Discuss, Result) per the user's follow-up;
  the section rail stays for Step, Test case, Discussion and the mark sheet; chat
  bubbles apply to both Discussion and Manual Discuss.
- 2026-11-05: open questions answered (clickable http/https links; every person right; Hand in submits directly). Status In progress.
- 2026-11-05: implemented. Review fixes: Hand in… enabled without a verdict (selects Result), sub-page heights follow the case bar, one debounced rail refresh, relayout cache, paren-balanced link targets, non-http links as "text (url)", strict `_web_url` (ASCII, no invisible chars, no backslash or user info) with a " (host)" hint, rail no longer sizes the page (relayout loop). Deviation: a person's bubble indents lists/quotes with spaces, because Tk paints the whole left margin in one colour. Not fixed: `_web_url` in person_window_app (server manual links) stays scheme-only, which is out of scope. Status Done.
