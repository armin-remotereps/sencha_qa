# Remove "How to do it" guides and "Ask AI"

Date: 2026-11-05
Plan: docs/plan/refactor-remove-howto-and-ask-ai.md

## What changed
- Human in the loop: person steps no longer get an AI how-to list or knowledge base guide research; `prepare_person_step` is one LLM call again and the instruction must carry any navigation the case states. Confirm / mark evidence treat only a case-stated `command` as a command step.
- Manual: claiming a case queues no guide task; the case card, runner snapshot and manual Discuss brief carry the verbatim steps only (the brief lost its "TEST MACHINE (at claim)" block; the live TEST MACHINE prompt section covers it).
- Ask AI removed everywhere: web card, runner window, `person_step_questions` socket event, `projects:test_run_case_person_ask`, the advisor and its context builders.
- Deleted: guide/advice modules in `agents/` and `projects/`, `knowledge_base/services/related_knowledge.py`, `render_excerpts`, `KnowledgeExcerpt`, the Celery tasks `answer_person_step_question` and `prepare_manual_case_guide`, and the related forms, templates and CSS.
- Test Runner 0.17.0: How to do it and Ask sections removed from the Step and Manual run tabs; question forwarding and IPC messages gone.

## Why
The `assist` queue (981 guide tasks for 349 manual guides plus Ask AI) was about 40% of a ~$200 AI day. Discuss / Pause and discuss covers the need.

## Behaviour and contract changes
- Migration `projects/0056_remove_howto_and_ask_ai`: drops `CasePersonStep.how_to` and the `PersonStepQuestion` and `ManualStepGuide` tables. **Data loss**: all Ask AI Q&A, manual guides and stored how-to lists (none reach TestRail, sessions or verdicts). Rolling the code back needs `migrate projects 0055` first (recreates empty tables).
- Protocol `CLIENT_VERSION` 0.17.0. Capabilities `person_step_assist_v1`, `person_step_guide_v1`, `manual_step_guide_v1` and messages `person_step_question(_result)` retired (ignored if an old runner sends them; never reuse). The server stops sending `how_to`, `questions`, `questions_enabled`, `can_ask`, `how_to_state` and manual `active.shell_label`; runners 0.10–0.16 default them and simply hide the features.
- Checkpoints: `person.how_to` and root `person_guide` are ignored on read and dropped on the next save.
- Settings removed: `PERSON_STEP_QUESTION_TIMEOUT_SECONDS`, `PERSON_STEP_QUESTION_MAX_TOOL_ROUNDS`, `MANUAL_GUIDE_MAX_STEPS`, `MANUAL_GUIDE_PARALLEL`, `MANUAL_GUIDE_CHUNK_TIMEOUT_SECONDS`, `PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS`, `PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS` (leftovers in `.env` are harmless).
- `assist` queue kept for readiness Scan / Check machine only; `assist-celery` concurrency 8 → 4.

## Notes for future changes
- Deploy: recreate `assist-celery` with the web containers; queued messages under the removed task names are rejected as unregistered. Do not purge `assist` (it also holds Scan/Check).
- `MANUAL_RUN_STATE_SAFE_BYTES` is an assumed budget; nothing on the server measures the manual snapshot.
