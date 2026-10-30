# Person steps show the TestRail steps they cover

Date: 2026-10-30
Plan: docs/plan/feat-testrail-step-refs.md

## What changed
- `agents/case_step_refs.py` (new): `normalize_case_step_refs` / `lenient_case_step_refs` (ints in 1..N, sorted, unique, ≤ 50; bad values dropped).
- `agents/types.py`, `agents/checkpoint.py`: `SubTask.case_steps`, `PersonStepHandOff.case_steps` (never for restores), `OrchestratorCheckpoint.case_step_count` / `case_steps_testrail`; optional keys (plan item and `person` key `case_steps`, root `case_step_count`, `case_steps_testrail` only when False), version unchanged.
- `agents/services/orchestrator_prompts.py`, `orchestrator.py`: with `run_orchestrator(case_step_count>0)` the planner tags each sub-task with `case_steps`; prompt unchanged at 0. `_Progress` carries the count and flag; a resume takes them from the checkpoint. Recoveries get no refs. `person_steps.prepare_person_step` copies the plan step's refs onto the hand-off.
- `agents/plan_revision.py`, `machine_setup/services/plan_step_args.py`, `setup_prompt.py`, briefs: `propose_plan_change` takes optional `case_steps`; a carried step sent without them keeps the earlier step's; `plan_fingerprint` excludes them. Help/result briefs append the label to plan lines.
- `projects/case_step_refs.py` (new): `numbered_case_steps` (steps-template cases only; "<n>. " markers at the start or after a blank line, in sequence; CRLF normalised), `case_step_rows` (texts stored on the row; none when the case's step count differs from the plan's), `case_steps_label`, `stored_case_steps`. `projects/case_step_labels.py` (new leaf): `format_case_steps_label` ("TestRail steps 3–5", "Case step 2" without `testrail_id`). `case_task_text.numbered_case_step_count`.
- `projects/models.py`: `CasePersonStep.case_steps` (`[{num, text, expected}]`, ≤ 50) and `case_steps_label`; migration `0043_casepersonstep_case_steps`. Filled in `services._pause_run_for_person` on create; a not-confirmed reopen keeps them.
- `projects/runner_person_step_services.py`, `controller_consumer.py`: runners with `person_step_case_steps_v1` get `case_steps_label` and `case_steps` (≤ 15); older ones get " · <label>" appended to `step_label`. Still one query.
- `controller_client/protocol.py` (`CLIENT_VERSION` 0.10.2, capability, `PersonCaseStepPayload`), `person_window_app.py` ("Covers …" block with step texts and expected results between "What to do" and "How to do it"; picker label), `person_window_ipc.py` (pipe cap 2 → 4 MiB).
- Web: "Your turn" card "Covers …" list (`person_step_services.PersonStepPanel.case_steps*`, `_person_step_card.html`, CSS); Steps card label per row (`case_step_progress.CaseStepRow.case_steps_label`); Ask AI `step_label` includes the label.

## Why
The person performing a Human in the loop step had to work out which TestRail steps the Punk Hazard step stood for; showing the numbers and text cuts the back-and-forth between tester and Punk Hazard.

## Behaviour and contract changes
- Migration `projects.0043_casepersonstep_case_steps` (additive, defaults).
- Controller protocol 0.10.2: capability `person_step_case_steps_v1`, snapshot fields `case_steps_label`, `case_steps`. Older runners keep working (label in `step_label`).
- Steps card JSON rows gain `case_steps_label`. `propose_plan_change` step items accept `case_steps`.
- Only plans made after the deploy carry refs; free-text cases never do.
- Deploy: `migrate`, rebuild the server image, restart Daphne/web and every Celery worker; update runners from the Environment page.

## Notes for future changes
- Refs are numbered against the case text when planned (`case_step_count`); the stale guard compares only the count, so a same-count reword mid-run shows the new text.
- The window lists at most 15 steps; the label names all.
- Verified with pre-commit (strict mypy server + controller incl. win32), `makemigrations --check`, plain-python smokes (parsing, labels, checkpoint round trip, snapshot payloads with fake rows), a real Tk smoke of the window (no screenshot: no Screen Recording permission), and three reviewer/verifier rounds. Not run with a live runner, DB or the real model; the web card was not checked in a browser.
