# Manual run steps get "How to do it" from the knowledge base and artifacts profile

Date: 2026-11-01
Plan: docs/plan/feat-manual-step-howto.md

## What changed
- `agents/services/person_step_prompts.py`, `person_steps.py`: a "case step" audience of the one HITL how-to rule set (`build_case_step_how_to_*_prompt`, `prepare_case_step_how_to`); HITL prompts byte-identical. `agents/checkpoint.py`: `person_guide_to_json` / `person_guide_from_json` made public.
- `projects/models.py` + migration 0054: `ManualStepGuide` (one per run case: fingerprint, generation, state, OS, research notes, per-step items) and `ManualGuideState`.
- `projects/manual_step_guide_services.py` (claim hook in `manual_case_claim_services`), `manual_step_guide_worker.py` + task `projects.tasks.prepare_manual_case_guide` (`assist`): KB research once per case, then steps in order in chunks, parallel HTTP-only calls within a wall-clock budget, generation-fenced writes, runner nudge per chunk.
- `projects/manual_step_guide_read.py`: `load_manual_guide(pivot)` (one query; fingerprint against the stored OS; stale in-flight guides read "none").
- Runner snapshot (`runner_manual_state_services.py`), case card (`manual_case_display_services.py`, `_manual_case_card.html`, new shared `_how_to_list.html`), Ask AI on a manual step (`manual_advice_mapping.py`, `person_step_question_services.py`). `projects/how_to_view.py` moved out of `person_step_services.py`.
- Test Runner: Manual tab "How to do it" block with Copy and sources (`person_window_manual.py`), rendering shared with the HITL tab (`person_window_how_to.py`), diagnose sample.

## Why
Manual steps had Ask AI but no "How to do it"; testers wanted the same KB- and profile-grounded guidance as Human in the loop.

## Behaviour and contract changes
- Migration `projects` 0054 (additive table).
- Settings: `MANUAL_GUIDE_MAX_STEPS` (default 30, 0 = off, max 30), `MANUAL_GUIDE_PARALLEL` (3, 1–8), `MANUAL_GUIDE_CHUNK_TIMEOUT_SECONDS` (120). Task route on `assist`.
- Controller protocol 0.15.0: capability `manual_step_guide_v1`; `manual_run_state` steps gain `how_to` / `how_to_state` (pending/ready/none), the active case `shell_label`. Older runners get the keys empty and ignore them. Update runners from the Environment page.
- Case page: collapsed "How to do it" per manual step, always (claimed or handed in).

## Notes for future changes
- The how-to never enters the checkpoint, hand-in, TestRail or the note rewrite; step texts stay verbatim.
- The guide uses the OS stored at claim; the live OS reads "Linux" after a disconnect.
- A re-claim with the same fingerprint writes only missing/failed steps; a new run regenerates. No beat sweep: a lost task reads "none" after the stale window, and the next claim retries.
- If the snapshot would exceed `MANUAL_RUN_STATE_SAFE_BYTES`, the how-to is dropped from it (warning log).
