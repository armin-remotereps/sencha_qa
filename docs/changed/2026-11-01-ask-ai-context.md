# Ask AI reads the knowledge base and artifacts profile and answers about the case and step

Date: 2026-11-01
Plan: docs/plan/fix-ask-ai-context.md

## What changed
- `projects/advice_context.py` (new) `build_advice_context`: one context builder for Ask AI — OS/shell, full case text, Application context, machine readiness artifacts profile (≤ 8000), KB index and question-first KB excerpts.
- `projects/person_step_advice_mapping.py` (new): HITL mapper (`advice_step`, `earlier_steps`, `current_plan_start`). Earlier steps count only from the newest of case start, a from-scratch help Continue and an applied redesign; carried redesign steps get a fixed "carried, passed" line.
- `agents/types.py`: `AdviceStep` (`source` "plan" or "case"), `AdviceCaseStep`, `AdviceEarlierStep`; `PersonStepAdviceContext` gains `step`, `earlier_steps`, `artifacts_profile`, `knowledge_index`, `project_id`.
- `agents/services/person_step_advisor.py`: bounded KB tool loop (first round `required`, ≤ 3 look-ups per round, final `none` answer), one deadline that also covers context building.
- `agents/services/person_step_advice_prompts.py`: prompt rewritten — scope is the current case and plan step, the case's own steps win over the reworded instruction, direct answers unless "how to", search the KB before any project fact and name the item, never invent; new fences for case steps, earlier steps, profile and KB index.
- `projects/person_step_question_services.py`: every failure (context, AI, soft time limit) stores the question as failed.

## Why
Answers were wrong: the advisor never saw the artifacts profile or the case's own words for the step, confused plan and case step numbers, had no earlier steps, and read the KB only through a few excerpts matched on the question.

## Behaviour and contract changes
- Settings: `PERSON_STEP_QUESTION_TIMEOUT_SECONDS` default 120 → 180; new `PERSON_STEP_QUESTION_MAX_TOOL_ROUNDS` (default 3, 0 = old single call). Startup fails when rounds > 0 and the timeout is under 60.
- No migration, URL, page, socket event or controller protocol change.

## Notes for future changes
- Manual runs reuse `build_advice_context` with `AdviceStep(source="case")`; put that mapper beside `person_step_advice_mapping.py`, not in `projects.services` (`advice_context` imports `build_agent_project_context` from it).
- Accepted risk: a Connection-key holder asking from the Test Runner can now read any of the project's KB content through answers (project-scoped).
- Logs record the look-up tool, item id and query length only, never question or query text.
