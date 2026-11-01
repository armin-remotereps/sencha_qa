# Human in the loop: "How to do it" grounded in the knowledge base and artifacts profile

Date: 2026-11-01
Plan: docs/plan/feat-hitl-howto-from-kb.md

## What changed
- `agents/services/person_guide_context.py`, `person_guide_research.py`, `person_guide_prompts.py`: once per HITL case, before the first hand-off, a bounded KB research loop (`PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS`, `PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS`) writes cited fact notes, stored in the checkpoint `person_guide` key (`PersonGuideNotes`, ≤ 2 attempts per case, considered once per execution).
- `agents/services/knowledge_tool_loop.py`: the planner's KB tool loop, extracted and shared (planner behaviour unchanged).
- `agents/services/person_step_prompts.py`, `person_steps.py`: one how-to rule set: instruction = what, how-to = how, every item adds a concrete fact, no restating, `[]` over filler, ≤ 12 items. With a guide, the fenced artifacts profile, research notes and per-step KB excerpts are added and each item may carry a `source` (KB item + page, or the profile) validated against resolved READY item ids; the label is server-built (`agents/how_to_source.py`). `_restates` drops restated items. Case text is marker-stripped.
- `agents/secret_redaction.py`: `redact_secrets` on instruction, expected result, how-to text and commands, research notes, the Ask AI profile and answers; never on a `kind="command"` command; a value the case states (whole token, ≥ 4 chars) is kept.
- `agents/types.py`, `agents/checkpoint.py`, `agents/json_values.py`, `agents/case_log.py`: `PersonHowToItem.source_*`, `PersonGuideSources/Context/Notes`, `person_guide` key; shared helpers.
- `projects/person_guide_sources.py`, `projects/services.py`: `build_person_guide_sources` passed to `run_orchestrator` on every execution (fresh or resumed); profile and index memoized and shared with the planner's fresh read. `machine_readiness/profile_text.py`: one function for the profile text and its cited ids.
- `projects/person_step_rows.py`, `person_step_services.py`, `_person_step_card.html`, `punk-hazard.css`: rows store sources; the card shows "Source: «name», page N" links (or the artifacts profile) and a "general guidance" hint when no item has one.
- `controller_client/protocol.py`, `person_window_app.py`, `person_window_diagnostics.py`, `projects/runner_person_step_services.py`, `runner_window_state.py`, `controller_consumer.py`: `PersonHowToPayload.source`, 12 items, capability `PERSON_STEP_GUIDE_V1`, CLIENT_VERSION 0.14.0; runners without it get ≤ 8 items, the 8th pointing to the case page.
- `agents/services/person_step_advice_prompts.py`, `person_step_advisor.py`, `projects/advice_context.py` (on top of `docs/changed/2026-11-01-ask-ai-context.md`): the Ask AI profile is redacted, the prompt has the secrets rule, and answers pass through `redact_secrets` (case-stated values kept); its KB lookup is unchanged.

## Why
Testers found the how-to just repeated the step. It now draws on the project's documents and machine profile to give exact paths, values, navigation, checks and what a wrong result looks like.

## Behaviour and contract changes
- New settings `PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS` (4; 0 disables research, excerpts and profile still used) and `PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS` (120); `example.env` updated.
- Checkpoint: optional `person_guide` key; how-to items gain optional `source_kind`, `source_item_id`, `source_page`, `source_label` (also in `CasePersonStep.how_to` JSON; no migration). `PERSON_STEP_HOW_TO_MAX_ITEMS` 8 → 12.
- Controller protocol 0.14.0: optional `source` per how-to item, `person_step_guide_v1`. Update runners from the Environment page.
- The prepare prompt changed for every HITL step, also in projects without KB or profile (no longer "write it whenever …").

## Notes for future changes
- `_Progress` must carry `person_guide` into every checkpoint, or research re-runs at every step.
- Notes are not refreshed when the KB changes during a case; sources are re-validated at every prepare. A link to a KB item deleted later 404s.
- Each grounded prepare sends up to ~22k characters of references (full profile every step); trim to the runner OS's line if cost matters.
- Redaction is a net for obvious shapes (`key=value`, Bearer, `user:pass@`, PEM, AKIA); prose secrets rely on the prompt rule.
- Verified with pre-commit, `manage.py check` and stubbed-LLM scripts; not yet run with the real model, a live runner or in a browser.
