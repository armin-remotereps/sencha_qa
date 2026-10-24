# fix: Verified desktop actions stop retrying once the intended element acted but the outcome is unconfirmed

Status: Done
Created: 2026-10-25
Request: When `click`, `double_click`, `hover` or `drag` has acted on the intended
element and the screen changed, but the vision outcome check does not confirm the
`expected_result`, the tool must stop trying other OmniParser candidates and return
the vision model's verdict to the sub-agent, saying the action was performed and the
expected result was not confirmed, so the agent re-observes and decides. Retrying
another candidate stays correct only when nothing happened or the wrong element was
acted on.

## Context
- `agents/services/tools_controller.py`: `_run_verified_action` loops
  `attempt(budget)` until it returns a `ToolResult`; `_confirm_or_finish` returns
  `None` whenever `confirm_action` rejects, so the loop re-parses the (already
  changed) screen and acts on a new candidate. `_verified_click` (click,
  double_click), `hover` and `drag` all go through it.
- `agents/services/verified_element_actions.py`: `confirm_action` settles,
  screenshots, calls `verify_action_outcome` and on rejection records every
  acted-on element as an `outcome` rejection in `VerificationBudget`.
- `agents/services/action_verifier.py`: `_OUTCOME_SYSTEM_PROMPT` asks one combined
  question, "did the action hit the intended element **and** produce the expected
  result", answered YES/NO + reason; `_parse_verdict` maps it to
  `agents.types.Verdict(accepted, reason)`. The same `Verdict` serves
  `verify_candidate`.
- Prompts: `agents/services/prompt_parts.py::build_desktop_tool_examples` and the
  `click`/`double_click`/`hover`/`drag` entries in
  `agents/services/tool_definitions.py` say the tools "retry other candidates when
  it does not match" and "when ... returns an error, the element could not be
  confirmed: re-describe it".
- `ToolResult.is_error` is not read by the agent loop or the LLM serializer (only
  `machine_setup/services/activity_recorder.py` reads it); the model sees the
  content only.

## Root cause
Confirmed (code read + run log). The outcome verdict is binary and conflates two
failures: "the action missed / nothing happened" (retry is right) and "the intended
element acted, the screen changed, but not exactly as expected" (retry is wrong).
`_confirm_or_finish` treats both as "try another candidate", so after a correct
click that opened the detail page, the tool clicked "Seafood", "Sencha", "762ms" on
the new screen until the step's 150 s budget ran out. The verdict has no field that
distinguishes the two, so the fix needs one.

## Goals
- Outcome check reports three states: confirmed, unexpected (intended element acted,
  screen changed, expected result not shown), missed (nothing changed, or a
  different element acted).
- On `unexpected`, the tool stops at once and returns a non-error result naming the
  element acted on and the vision reason.
- Same behaviour for click, double_click, hover (with expected_result) and drag (with
  expected_result).

## Non-goals
- Candidate verification (`verify_candidate`), `MAX_CANDIDATE_REJECTIONS_PER_PARSE`,
  budgets, browser tools: unchanged.
- No pixel-diff of screenshots (see Alternatives).
- No change to the orchestrator, evaluate prompt or case verdict logic.

## Design
### Data model
None (no DB). New frozen dataclass in `agents/types.py`:
`OutcomeVerdict(status: Literal["confirmed", "unexpected", "missed"], reason: str)` A separate type rather than a
field on `Verdict`, because `verify_candidate` has no "acted" notion and a shared
optional field would be meaningless there.

### Services
- `action_verifier.py`
  - `_OUTCOME_SYSTEM_PROMPT` / question: answer on the first line with exactly one of
    `YES` (expected result visible), `CHANGED` (the intended element was acted on and
    the screen changed, but the expected result is not, or only partly, shown),
    `NO` (nothing visibly changed, or a different element / wrong place was acted
    on); reason on the next line. Spell out that CHANGED is for "right element,
    different or incomplete result" and NO for "missed".
  - `verify_action_outcome` returns `OutcomeVerdict` via a new
    `_parse_outcome_verdict`. Check `CHANGED` before `NO`-prefix matching is not an
    issue (different first letters), but match on the first token, not
    `startswith`, so "NOT..." / "NOTHING..." cannot be misread. Unparseable or empty
    reply -> `missed` (keeps today's retry behaviour for garbage).
  - `_parse_verdict` / `verify_candidate` unchanged.
- `verified_element_actions.py`
  - `confirm_action` returns `OutcomeVerdict`; records `outcome` rejections in the
    budget only for `missed` (an `unexpected` element is not "wrong", and the loop
    ends anyway).
  - New `format_unconfirmed_message(base: str, reason: str, budget) -> str` next to
    `format_verified_message`, same "Skipped candidates" history tail.
- `tools_controller.py`
  - `_confirm_or_finish`: `confirmed` -> success as today; `missed` -> `None`
    (retry); `unexpected` -> return
    `ToolResult(is_error=False, content=format_unconfirmed_message(...))`.
    No signature change; click/double_click/hover/drag pick it up automatically.
- Tool result text (decision), built from the existing `success_message`:
  `"<success_message>. The expected result was NOT confirmed: <reason>. The action
  was performed once and the screen changed; no other element was tried. Check the
  current screen (take_screenshot) and decide: continue if the step's goal is met,
  otherwise act on what you see. Do not repeat the same action blindly."`
  e.g. "Clicked element at (412, 88): Rapid Ext JS extension. The expected result
  was NOT confirmed: version 1.3.0 appears in the Installation section, not the
  header. ..."
- `is_error=False`: the action did happen; an error would contradict that and the
  prompt maps tool errors to "element could not be confirmed, re-describe it", which
  is the wrong advice here. The "NOT confirmed" wording carries the signal (the LLM
  only sees content anyway).

### Pages and forms
None.

### Background work and WebSockets
None. No controller protocol change (server-side only; old runners unaffected).

### Settings / infrastructure
None.

### Data flow
tool call -> resolve_verified_element (candidate verified) -> before screenshot ->
press/hover/drag -> confirm_action -> verify_action_outcome:
YES -> "Clicked ... (verified on attempt n)"; NO -> reject + next candidate (as
today); CHANGED -> stop, return "Clicked ... expected result NOT confirmed: <reason>"
-> sub-agent re-observes and decides; the step's verdict stays with the sub-agent.

## Alternatives considered
- Pixel diff of before/after to define "screen changed": cannot tell a wrong-element
  click (which also changes the screen, as "Seafood" did) from a right one, needs
  image decoding and thresholds for clocks/cursors. The vision model already sees
  both shots and the intended element; asking it one more distinction is cheaper and
  more accurate.
- Stop after any acted attempt: would drop the legitimate "missed" retry that the
  verified flow exists for.
- JSON verdict reply: more robust parsing but a bigger prompt/parse change than a
  third first-line token; the YES/NO line format is the established convention here.

## Risks and mitigations
- Vision model answers CHANGED for a wrong-element click: the agent gets an honest
  "performed on <element at x,y>, not confirmed" and re-observes; worst case one
  agent turn instead of 150 s of wrong clicks. Prompt wording makes "different
  element" explicitly NO.
- Model keeps answering NO for right-element/partial results (old habit): behaviour
  equals today; the prompt must give CHANGED its own sentence and example.
- Default click expectation ("click landed and UI reacted"): CHANGED is unlikely
  there but harmless.
- The agent may now accept a near-miss as success: the sub-agent prompt asks it to
  check the step's goal on screen; the orchestrator's evaluation is unchanged.

## Implementation steps
1. Add `OutcomeVerdict` to `agents/types.py`. (files: agents/types.py)
2. Three-way outcome prompt and `_parse_outcome_verdict`; `verify_action_outcome`
   returns `OutcomeVerdict`. (files: agents/services/action_verifier.py)
3. `confirm_action` return type and missed-only rejection; add
   `format_unconfirmed_message`. (files: agents/services/verified_element_actions.py)
4. `_confirm_or_finish` handles the three states. (files:
   agents/services/tools_controller.py)
5. Prompt/tool docs: in `build_desktop_tool_examples` add a bullet "If the result
   says the expected result was NOT confirmed, the action was performed once on that
   element and no other candidate was tried: take_screenshot, then decide; do not
   repeat the action blindly"; narrow the existing error bullet to errors. In
   `tool_definitions.py` click/double_click/hover/drag: retries happen "when the
   action missed"; when the intended element acted but the result differs the tool
   stops and reports it. (files: agents/services/prompt_parts.py,
   agents/services/tool_definitions.py)
6. Update `agents/CLAUDE.md` "Verified desktop actions" paragraph. (files:
   agents/CLAUDE.md)

Steps 1-4 are one dependent chain (single implementer); 5-6 can go in parallel.

## Verification
- `mypy .` and `pre-commit run --all-files` clean.
- `python manage.py shell -c` against `_parse_outcome_verdict` with "YES\n..",
  "CHANGED\n..", "NO\n..", "NOTHING changed", "" -> confirmed/unexpected/missed/
  missed/missed.
- Manual run on a desktop case: click with an expected_result that is deliberately
  slightly wrong (e.g. "the version is in the header" for the Rapid Ext JS page):
  exactly one click, tool result "... NOT confirmed: <reason>", no further
  candidates in the log, agent takes a screenshot and continues.
- Manual: click with a description that OmniParser maps to a wrong neighbouring
  element first -> NO -> second candidate tried (retry still works).
- hover/drag with expected_result: same two checks once each; hover without
  expected_result unchanged.
- Run `reviewer` and `verifier`.

## Open questions
None blocking. Assumption: the `unexpected` result is `is_error=False` and the
sub-agent, not the tool, decides whether the step's goal was met.

## Changelog
- 2026-10-25: created
- 2026-10-25: implemented. Review fixes: dropped the unused `OutcomeVerdict.accepted`;
  the parser reads the leading letters with a regex (so "YES—shown" is confirmed);
  the unconfirmed message puts the reason on one line without a doubled full stop;
  both formatters share `_with_skipped`. Reviewer/verifier second pass clean.
  `projects/migrations/0022_*` was reformatted by black during pre-commit (formatting only).
