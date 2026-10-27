# Keep general test cases short: plan only what the case asks

Date: 2026-10-27
Plan: inline (planner returned a prompt-only plan, no plan file)

## What changed
- `agents/services/orchestrator_prompts.py`: new shared `PLAN_SCOPE_RULES` (SCOPE + step granularity) used by `build_plan_system_prompt`; the planner persona now asks for the fewest sub-tasks; the time-budget rule gives combined sub-tasks more time.
- `agents/services/prompt_parts.py::build_qa_rules`: the executor does only what its task asks, does the named housekeeping (close clauses), then finishes; "Verify every check in the expected result".
- `machine_setup/services/setup_prompt.py`: help-chat and result-chat redesign rules reuse `PLAN_SCOPE_RULES` (replacing "One concrete action per step") and follow a person who asks for more coverage.

## Why
A general case ("check feature A works") was planned as a long scenario that exercised every item. It should be a short happy path that runs fast.

## Behaviour and contract changes
- General checks without listed items: reach the feature, use its main action once, confirm the visible result (typically 2–5 steps); no invented scenarios, per-item steps, or edge/negative tests the case does not ask for. "All X are shown" is one look; "each X works" still exercises each X.
- Explicit cases: listed steps/items only, plus needed preconditions and implicit steps; every stated expected result kept.
- A general-check step may combine repeat-safe actions (open, navigate, look, read); anything that creates, submits, sends or changes data stays its own step.
- No setting, model, protocol or code-level step cap change.

## Notes for future changes
- Combining is limited to repeat-safe actions because a step interrupted by a fault or help pause re-runs from its first action (checkpoints are per step).
- Rejected review finding: the help chat's pre-existing "add a step that verifies its result" vs "No verification-only steps" wording predates this change; separate plan if it matters.
- Verified with pre-commit on the changed files, full `mypy .`, rendered prompts, and three reviewer + verifier passes. Not yet checked against the real model on a live run.
