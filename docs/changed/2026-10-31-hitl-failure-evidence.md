# Human in the loop failures carry AI-recorded evidence

Date: 2026-10-31
Plan: docs/plan/fix-hitl-failure-evidence.md

## What changed
- `agents/services/person_step_prompts.py`: the confirm prompt always starts with a screenshot; "different" allows at most one read-only check. New: `build_mark_evidence_sub_task`, `format_person_evidence` (pasted + check output, head and tail, fenced `COMMAND_OUTPUT`), `format_mark_summary`, "Output attached." on the summaries, and a mark label in `person_attempt_label` (headline only).
- `agents/services/person_steps.py`: `confirm_person_step` returns `PersonCheck(result, check_output)`. `_run_check` counts the sub-agent's screenshots, keeps its `$ ` command log lines, and takes one screenshot without the model (`tools_controller.capture_screenshot`) when the sub-agent took none. New: `record_mark_evidence` and `person_failure_result` (evidence on `SubTaskResult.error` + one `[Evidence] step N:` log block).
- `agents/services/orchestrator.py`: a "different" report records that evidence.
- `agents/services/orchestrator_prompts.format_attempt_lines`: shows a person attempt's `error` capped at `EVIDENCE_PROMPT_MAX_CHARS` (1500).
- `agents/services/person_mark.py` (new): `record_person_mark` takes a checkpoint and returns it with a failed person evidence attempt at the marked step and no `person` key.
- `agents/types.py` / `agents/checkpoint.py`: `person` states `marked_failed` / `marked_blocked`, `PersonMarkVerdict`, `person_mark_verdict()`.
- `projects/person_step_services.py`: Mark failed / Mark blocked (`_mark_person_step`, new `output` argument) leave the row `reported` with the mark's close reason, write the `marked_*` key, put the case back to Created with the verdict text in `result`, and queue it first.
  - If a gate refuses, the verdict is written now ("no evidence: <reason>") and the run is queued once more, which finishes a run with no open case left.
  - `PersonStepPanel.marked_verdict`; `PersonStepActionResult.no_evidence`.
- `projects/services.py`: `execute_test_run_test_case` sends a `marked_*` case to `_finish_person_mark`: wait for the runner, run the evidence pass, then `person_mark_verdict.apply_person_mark`. There is no orchestrator, teardown or cleanup on this path, and any fault logs "[Evidence] no evidence: …". `abort_test_run` first calls `settle_person_marks_on_abort`.
- `projects/person_mark_verdict.py` (new leaf): writes the person's verdict, closes the marked row (Failed → `failed`, Blocked → `cancelled`), logs no-evidence lines.
- `projects/testrail_mapping.py`: the step list in TestRail comments adds a failed or interrupted person step's evidence block, indented as code.
- `projects/forms.py`, `projects/person_step_views.py`, `projects/runner_person_step_services.py`: the decision form and the runner form keep `output` for Mark failed / blocked.
- `templates/projects/includes/_person_step_card.html`: "Recording evidence…" while a mark waits for its pass; the output hint warns about secrets.
- Settings: `PERSON_EVIDENCE_OUTPUT_MAX_CHARS` (4000), `PERSON_EVIDENCE_LLM` (True).

## Why
In Human in the loop the AI is responsible for recording the result. Before this change, command steps could pass or fail with no screenshot, the person's pasted output never reached the attempt, the case log or TestRail, and Mark failed / Mark blocked skipped the AI entirely. TestRail then fell back to the case's last screenshots, which could show earlier steps.

## Behaviour and contract changes
- Every confirm attempt has at least one screenshot tagged with its attempt, so there is one more runner round trip when the model took none.
- Mark failed / Mark blocked are no longer instant. The case runs again first (In progress, "Recording evidence…"), the next case starts after it, and the flash says so. The verdict is the person's, never the AI's. A fault, a gate refusal, an abort or an unusable checkpoint keep the verdict with "[Evidence] no evidence: …", never Need help.
- Checkpoint `person.state` has two new values (no version bump). TestRail result comments of failed person steps change, so already-pushed rows re-send once if their comment hash changes.
- An overrule still takes no screenshot.
- No migration, URL, WebSocket event or controller protocol change. The runner window may now send `output` with fail / block.

## Notes for future changes
- A Needs re-test mark during the evidence pass wins (the mark's verdict is not written). A worker that dies hard leaves the `marked_*` key until a Resume drops it.
- Pasted output is not redacted. It is capped and shown escaped, and the card warns against pasting secrets.
- The evidence keeps the check commands' output, not the command text; the command is in the case log's tool-call lines.
- A mark on a restore hand-off records a `resume` attempt. For Failed, the TestRail selection then falls back to the last shots, which are that attempt's.
