# Verified actions stop once the right element acted but the result differs

Date: 2026-10-25
Plan: docs/plan/fix-verified-action-unconfirmed-outcome.md

## What changed
- `agents/types.py`: new `OutcomeVerdict(status: confirmed | unexpected | missed, reason)`; `Verdict` (candidate check) unchanged.
- `agents/services/action_verifier.py`: the outcome prompt answers YES / CHANGED / NO; `_parse_outcome_verdict` reads the reply's leading letters, so anything unreadable counts as `missed`.
- `agents/services/verified_element_actions.py`: `confirm_action` returns `OutcomeVerdict` and records an outcome rejection only for `missed`. New `format_unconfirmed_message`; both formatters share `_with_skipped`.
- `agents/services/tools_controller.py`: `_confirm_or_finish` returns on `unexpected` with a non-error result ("… The expected result was NOT confirmed: <reason> …") instead of trying another candidate. This covers click, double_click, hover and drag.
- `agents/services/prompt_parts.py`, `tool_definitions.py`, `agents/CLAUDE.md`: describe the new result and tell the agent to take a screenshot and decide.

## Why
In one run a click hit the right element and the page opened. The outcome check then rejected it, because the agent's `expected_result` named a detail that was not there. The tool went on clicking unrelated candidates on the changed screen until the step timed out, and the case was marked FAIL although the app was fine.

## Behaviour and contract changes
- Tool result text only: an action that acted on the intended element but did not reach the expected result is now reported once, as performed but not confirmed. A real miss still retries. There is no migration, setting or controller protocol change. Deploy: restart the execution workers.

## Notes for future changes
- If the vision model answers CHANGED for a wrong-element click, the cost is one extra agent turn. If it keeps answering NO for a partial result, behaviour stays as it was before this change.
- Not yet run against a live runner; the plan's Verification lists the manual checks.
