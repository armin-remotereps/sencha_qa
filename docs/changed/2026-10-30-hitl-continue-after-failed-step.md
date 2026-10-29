# Human in the loop continues after a non-blocking failed step

Date: 2026-10-30
Plan: docs/plan/refactor-hitl-continue-after-failed-step.md

## What changed
- `agents/services/orchestrator.py`: a person's "different" report no longer always stops the case. `_evaluate_person_failure` (own system+user call) asks whether a remaining step depends on the failed one: `continue` writes `Step N: FAILED — …`, advances, checkpoints and hands out the next step; `stop` (also for an unparseable answer) ends the case as before. The last step records `continue` without a call.
- `agents/services/orchestrator.py`: `_build_verdict(decisions=)`. When every failed final step is a continued person failure (`_judged_person_failures`) and all steps ran, `_judge_person_failures` lets the LLM decide pass or fail; pass only on an explicit `"pass"` with a summary, any AI or parse fault fails. Otherwise pass/fail stays deterministic.
- `agents/services/orchestrator_prompts.py`: `build_person_failure_system_prompt`, `build_person_failure_prompt`, `build_person_failure_verdict_system_prompt`, `build_person_failure_verdict_prompt`; `build_verdict_prompt` now shares `_verdict_results` / `_verdict_notes` / `_STEP_NAMING_RULE` (same text).
- `CLAUDE.md`, `agents/CLAUDE.md`: Human in the loop / Person steps text.

## Why
A step a person reports as different should not end the whole case when the rest can still run; whether that difference really fails the case is decided at the end, when later steps' results are known.

## Behaviour and contract changes
- A HITL case can now pass even though a person reported a different result on a step (only when the AI judges the difference does not touch what the case checks). The step row still shows Failed + "Continued: <reason>"; only the verdict text explains it.
- One extra small LLM call per continued person failure (not on the last step). No models, migrations, checkpoint keys, protocol or UI changes.

## Notes for future changes
- Agent-performed steps and restore hand-offs are unchanged (a continued agent failure still fails the case; "different" on a restore still raises `ResumeStateLostError`).
- A fault in the blocker call after the attempt is recorded opens Need help with the cursor on the step; Continue hands the step out again.
- A resume at the verdict phase re-runs the judgement and may word or decide it differently.
