# feat: Show TestRail HTML / Markdown case text as readable text

Status: Done
Created: 2026-11-01
Request: Test case text imported from TestRail (preconditions, steps, expected
result, the per-step texts of "Test Case (Steps)" cases) is often TestRail
Markdown (`**bold**`, `- item`, `![](index.php?/attachments/get/12)`, `|||`
tables, `[link](url)`) or HTML from the TestRail 9.7+ rich-text editor (`<p>`,
`<br>`, `<ul><li>`, `<strong>`, `&nbsp;`). Today the raw markup is shown. Parse
it and show readable text without tags in the Test Runner window (Test case tab
and Manual run tab) and on the web run case page, plus the other read-only web
places that show the same case text. Editing, agent/LLM prompts and TestRail
push keep the raw text.

## Context
- Storage: `projects/models.py` `TestCase.preconditions/steps/expected` hold the
  raw TestRail fields (`projects/testrail_client.py` `_case_from_fields`;
  steps-template cases are flattened by `projects/testrail_mapping.flatten_steps`
  into "`<n>. <text>`" blocks separated by a blank line, split back by
  `projects/case_step_refs.numbered_case_steps`).
- Runner Test case tab: `projects/runner_case_services._build_view` fills
  `PersonCaseStatePayload.preconditions/steps_text/expected_text/numbered_steps`
  (`controller_client/protocol.py`), rendered in
  `controller_client/person_window_app.py` (~L1925).
- Runner "Your turn" covered steps: `projects/runner_person_step_services._case_step_payloads`
  (from `CasePersonStep.case_steps`, raw texts stored at hand-off).
- Runner Manual run tab: `projects/runner_manual_state_services._active_case`
  (preconditions + `ManualStepPayload` texts from the manual checkpoint, which
  holds verbatim case text, `projects/manual_case_steps.py`), rendered in
  `controller_client/person_window_manual.py`.
- Web: `templates/projects/test_run_case_detail.html` "Test case" card
  (L576-607, `pre-wrap` raw fields); `templates/projects/includes/_person_step_card.html`
  "Covers ..." list (`case_step.text/expected`);
  `templates/projects/includes/_manual_case_card.html` steps (`step.description`,
  `step.expected_result`, from `manual_refinement_services.get_manual_case_panel`).
- Not affected (raw on purpose): edit dialogs and their `data-*` attributes
  (`test_run_detail.html`, `_test_case_table.html`, `_test_case_edit_dialog.html`,
  case page edit dialog), LLM prompts / Ask AI context, TestRail push, manual
  refinement, test case list (shows no case text).
- Precedent: `docs/plan/feat-chat-markdown.md` renders agent Markdown with
  marked + DOMPurify client-side; not reusable as-is (TestRail tables, Tk window).

## Goals
- One server-side converter turns TestRail HTML or Markdown into readable plain
  text; plain text passes through essentially unchanged.
- Runner windows (all versions, no protocol change) and the listed web places
  show the converted text.
- Raw text stays everywhere it is edited, sent to an LLM, or pushed.

## Non-goals
- Rich rendering (bold, real lists, tables) on the web or in Tk.
- Showing TestRail attachment images (they need TestRail auth).
- Changing stored data, imports, or what agents see.

## Design
### Data model
None. No migration.

### Services
- New `projects/case_markup.py` (pure stdlib: `re`, `html`, `html.parser`; no
  Django, no models): `markup_to_plain(text: str) -> str`.
  - Input capped first (`_MAX_INPUT_CHARS`, e.g. 64 000) to bound work; callers
    still cap the output with their existing protocol / model limits.
  - Detection, conservative: **HTML** only when a known tag matches
    `</?(p|br|div|span|ul|ol|li|strong|b|em|i|u|s|a|img|h[1-6]|table|thead|tbody|tr|td|th|pre|code|blockquote|hr)\b[^<>]{0,500}>`
    (case-insensitive); so `a < b`, `<username>`, `x<5` stay plain. Otherwise
    the Markdown pass runs (its rules are harmless on plain text).
  - HTML pass: an `HTMLParser` subclass (`convert_charrefs=True`) emitting text;
    block tags (`p, div, h*, blockquote, pre, table, tr, ul, ol, hr`) end the
    line, `br` = newline, `li` = "• " or "n. " (per-`ol` counter, 2-space indent
    per nesting level), `td/th` joined with " | ", `a` = "text (href)" when href
    is http(s) and differs from the text, `img` = "[image: alt]" / "[image]",
    `script/style/head` content dropped, unknown tags dropped (text kept),
    whitespace collapsed outside `pre`, `&nbsp;` to space. Any parser exception
    returns the raw text.
  - Markdown pass, line-based, ReDoS-safe (`[ \t]` not `\s`, no nested
    quantifiers): headings `#..######` stripped; `*`/`+`/`-` bullets to "• ";
    `**x**` / `__x__` / `*x*` (paired on one line, not word-adjacent) unwrapped,
    `_x_` left alone (snake_case); `` `x` `` unwrapped, fence lines removed with
    content kept; `![alt](u)` to "[image: alt]"; `[t](u)` to "t (u)"; TestRail
    tables (`|||:h|h`, `|| a | b`) to "a | b"; `> ` quote prefix stripped;
    backslash escapes removed.
  - Both: trailing spaces trimmed, 3+ newlines collapsed to one blank line,
    outer whitespace stripped.
- New `projects/case_text_display.py` (no queries, works on a loaded
  `TestCase`): `case_text_display(test_case: TestCase) -> CaseTextDisplay`
  (frozen dataclass: `preconditions`, `steps_text`, `expected_text`,
  `numbered_steps: tuple[CaseStepText, ...]`). For steps-template cases each
  numbered step's text and expected is converted *after* splitting (the HTML
  pass would otherwise collapse the "\n\n<n>. " separators), and
  `steps_text`/`expected_text` are rebuilt from the converted steps; otherwise
  each field is converted whole. Plus `case_steps_for_display(steps:
  Sequence[CaseStepText]) -> tuple[CaseStepText, ...]` for stored hand-off rows.
- Callers (convert, then apply the existing caps):
  - `runner_case_services._build_view`: use `case_text_display`.
  - `runner_person_step_services._case_step_payloads`: `markup_to_plain` per
    text/expected.
  - `runner_manual_state_services._active_case`: preconditions and each step
    text/expected.

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_case_detail` (unchanged) | GET | `@project_membership_required` | none | new context key `case_text` (`CaseTextDisplay`); Test case card renders `case_text.preconditions`, steps as a numbered list (`num. text` + muted "Expected: ...") when `numbered_steps`, else `case_text.steps_text`, and `case_text.expected_text` only for free-text cases; all autoescaped, `pre-wrap` | none |

- Template filter `case_text` in `projects/templatetags/project_extras.py`
  (calls `markup_to_plain`, returns `str`, never `mark_safe`), used for
  `case_step.text/expected` in `_person_step_card.html` and `step.description` /
  `step.expected_result` in `_manual_case_card.html`.
- Edit dialogs untouched.

### Background work and WebSockets
No new tasks or events. Snapshot payload shapes unchanged; only their text
content is converted. No `CLIENT_VERSION` bump, no new capability: every runner,
old or new, gets readable text. The runner gains no converter.

### Settings / infrastructure
None. No new dependency.

### Data flow
Raw DB text -> `case_text_display` / `markup_to_plain` at display time ->
capped -> protocol payload (runner) or template context (web, autoescaped).
LLM, push and edit paths keep reading the raw model fields.

## Alternatives considered
- **Sanitized HTML on the web** (nh3 server-side, or the chat's marked +
  DOMPurify client-side): keeps bold/lists but adds a dependency or a second
  client renderer, marked does not know TestRail tables, and Tk cannot render
  HTML, so web and runner would differ. Rejected for now; plain text meets
  "don't show the tags" with zero XSS surface. Can be layered on later from the
  same detection.
- **Converter in the runner** (shared stdlib module under `controller_client/`):
  only new runners would benefit, needs a capability/version bump and duplicates
  work per push. Server-side conversion is simpler.
- **Convert on import / store a second column**: migrates data, breaks the
  "edit keeps markup" rule, stale after edits. Rejected.

## Risks and mitigations
- False-positive detection mangling plain text: tag allowlist for HTML;
  Markdown rules chosen to be harmless on plain text; raw text still in edit
  dialogs.
- XSS: output is plain `str`, autoescaped; no `|safe`/`mark_safe`. Links become
  text, not anchors.
- ReDoS / cost on large fields: input cap, linear regexes, `html.parser` is
  linear; snapshots already coalesced to one push per second.
- Step splitting: convert per numbered step, never the flattened blob.
- Runner window change detection compares texts; converted text is stable, so no
  extra redraws.

## Implementation steps
1. Converter (files: `projects/case_markup.py`).
2. Display composition (files: `projects/case_text_display.py`).
3. Runner payloads (files: `projects/runner_case_services.py`,
   `projects/runner_person_step_services.py`,
   `projects/runner_manual_state_services.py`).
4. Web (files: `projects/templatetags/project_extras.py`, `projects/views.py`
   `test_run_case_detail` context, `templates/projects/test_run_case_detail.html`,
   `templates/projects/includes/_person_step_card.html`,
   `templates/projects/includes/_manual_case_card.html`; any small list style in
   `static/css/punk-hazard.css`).
5. `pre-commit run --all-files`.

## Verification
- `manage.py shell -c` against `markup_to_plain` with samples: TestRail HTML
  (`<p>`, nested `<ul>/<ol>`, `<table>`, `&nbsp;`, `<img>`), TestRail Markdown
  (bold, bullets, `|||` table, image, link, fence), plain text with `a < b`,
  `2 * 3 * 4`, `file_name_here`, `<username>` (unchanged).
- Steps-template case whose step texts are HTML: case page shows numbered
  steps with expected, runner Test case tab highlights the covered steps
  correctly.
- HITL "Your turn" card and runner window "Covers ..." list; Manual run tab and
  manual case card show plain text.
- Edit dialogs still show and save the raw markup; Ask AI / planner prompts and
  TestRail push unchanged (diff shows no change in those modules).
- Old runner (< 0.14.0) shows converted text too.
- Run `reviewer` and `verifier`.

## Open questions
- Q1 (non-blocking) Plain readable text everywhere, or sanitized rich rendering
  (bold, real lists) on the web later? (Assumption: plain text on both surfaces
  in this change.)

## Changelog
- 2026-11-01: created
- 2026-11-01: implemented. Review fixes: whole fields are always converted and
  the case page lists numbered steps only when they cover the whole field
  (`steps_listed`, `steps_cover_expected`), so no text is hidden; HTML needs a
  closed known element or a br/hr/img tag; `markup_to_plain` is cached
  (lru 512) with bounded inline regexes; manual Steps card rows are converted
  only in `case_step_progress._row_payload`, so the Machine setup run-state
  text stays raw. Accepted: Markdown that also contains HTML tags goes through
  the HTML pass only (its Markdown markers stay).
