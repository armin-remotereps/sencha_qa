# fix: Human in the loop failures always carry AI-recorded evidence

Status: Done
Created: 2026-10-31
Request: The user says that in Human in the loop the AI is responsible for recording the result. So even when a command-based step fails, the AI should take a screenshot and record the failure logs. Today a person step can end Failed with no screenshot, and the command output the person pasted never reaches the attempt or the case log. Required at minimum:
- every confirm attempt takes a screenshot;
- a failure records the command output and relevant log excerpts, bounded in size, on the attempt and in the case log;
- that evidence is what TestRail evidence selection (`projects/testrail_evidence.py`) sends.

This plan is independent of `docs/plan/feat-tester-console.md`.

## Context
- The confirm sub-agent is `agents/services/person_steps.confirm_person_step`. It may use only `take_screenshot`, `execute_command` and `wait_for_command` (`CONFIRM_TOOL_NAMES`). Its prompt is `agents/services/person_step_prompts.build_confirm_sub_task`.
- The person attempt is recorded in `agents/services/orchestrator.py` (the function containing `confirm_person_step(`, around lines 912–1000):
  - It mints an `attempt_id`, and the screenshots taken by the sub-agent are tagged with it (`_attempt_screenshot_callback`).
  - It builds a `SubTaskAttempt(performed_by="person", ...)`.
  - For "different" it builds `SubTaskResult(status="fail", summary=format_different_summary(...))`, with no `error` and no output.
- The case log receives the sub-agent's tool calls through `agent_loop` (`[Tool Call]`, `[Tool Result]` preview). `tools_controller` logs the output of any command the sub-agent runs in full.
- `projects/person_step_services.mark_failed_person_step` and `mark_blocked_person_step` write the case verdict directly in the request: status, `result` text, `finished_at`. Then the rest of the run is queued, so the runner moves straight on to the next case.
- `projects/testrail_evidence.select_evidence`:
  - Failed: it sends the screenshots of the final failed `step` attempts.
  - Blocked: it sends the attempt the case stopped on.
  - Otherwise it falls back to the case's last N screenshots (`TESTRAIL_EVIDENCE_SHOTS_PER_STEP`=3, `TESTRAIL_EVIDENCE_MAX_SHOTS`=10).

## Root cause
Confirmed by reading the code (no live run yet):
1. **Command steps often take no screenshot.** The confirm prompt says: "Command step: judge the pasted output first … take a screenshot only when the step has a visible effect on screen." A command step with pasted output can pass or fail with zero tool calls, so the attempt has no tagged screenshot.
2. **"Different" reports need no tool call at all.** The prompt says: "record what you see as evidence and answer RESULT: FAIL". Nothing forces a screenshot, so the model may answer FAIL at once.
3. **The person's pasted output never reaches the attempt or the case log.**
   - It goes into the confirm prompt and is kept on `CasePersonStep.output` and in the checkpoint `person` key.
   - The `person` key is dropped once the attempt is recorded (`progress.person = None`).
   - `format_different_summary` includes the note but not the output, and nothing logs the output.
   - So the verdict LLM, the case log and TestRail never see it.
4. **Mark failed / Mark blocked by a person bypass the AI entirely.** They skip the worker, so there is no attempt, no screenshot and no log line. The decision views also drop any output in the form. TestRail evidence then uses the fallback, the case's last screenshots, which may belong to earlier steps or even show an earlier, passing state.

Suspected, verify during implementation: when the confirm sub-agent runs a command, its output is logged in full (`tools_controller` line ~131) and could be very large. That is the existing behaviour, but the bounded excerpt below should be the evidence, not the raw log.

## Goals
- Every confirm attempt (report "as expected" or "different") has at least one screenshot tagged with its `attempt_id`.
- A failed person attempt records the following, bounded and fenced as data:
  - the pasted output (head and tail, cap `PERSON_EVIDENCE_OUTPUT_MAX_CHARS` = 4000);
  - the output of any check command the sub-agent ran (head and tail, the same cap in total);
  - the result goes on `SubTaskResult.error` and as one `[Evidence]` block in the case log.
- Mark failed / Mark blocked by a person also produce an AI-recorded evidence attempt (screenshot + output) **before** the run moves on.
- TestRail evidence picks these attempts' screenshots through the existing rules (no fallback needed), and the TestRail comment carries the bounded output excerpt.

## Non-goals
- Screenshots taken from the browser. Tester uploads (console S9) and on-demand capture (console S10) are follow-ups that can reuse the evidence step below.
- Changing who decides the verdict: the person's Different, Mark failed and Mark blocked stay final.
- The overrule path. An overrule is a person's explicit pass without a check, and it stays without evidence (open question Q3).

## Design

### Confirm sub-agent (agents)
- `person_step_prompts.build_confirm_sub_task`:
  - Every confirm must call `take_screenshot` first, before judging, with a question about the expected result. Command steps then judge the output.
  - "Different" says: take the screenshot, run at most one read-only check that shows the failure (for command steps), then answer FAIL.
- `person_steps.confirm_person_step`: if the sub-agent finishes without having taken a screenshot, the module takes one through the same tool path. That is one deterministic `take_screenshot` call under the attempt's `on_screenshot`, with no LLM, so the guarantee does not depend on the model. How the sub-agent reports tool use (`SubTaskResult` / the agent loop's tool count) is checked during implementation. If it is not exposed, add a small `screenshots_taken: int` to the sub-agent result.
- Output capture: the sub-agent's command outputs are collected bounded (head + tail) through the existing `on_log` hook, or a returned field; the implementer chooses after reading `run_sub_agent`.

### Attempt recording (orchestrator)
- On a failed person attempt ("different", or confirmed FAIL that the person later marks failed through the path below), `SubTaskResult.error` = a fenced evidence block:
  - person's pasted output (bounded);
  - check command(s) and output (bounded);
  - the sub-agent's observation.
- `format_different_summary` stays short and gets one line, "Output attached", when output exists.
- One case log entry `[Evidence] step N: …` with the same bounded block. It is model-free, so no XSS beyond the existing log escaping.
- No checkpoint version change: `error` already exists on `SubTaskResult`.

### Person Mark failed / Mark blocked (projects)
Today these write the verdict in the request. New flow, recommended:
- The service keeps every current rule (locks, stale guards, test-session hooks). Instead of writing the final status, it writes the checkpoint `person` key with `state="marked_failed"` / `"marked_blocked"`, the note and the output. The output field is now accepted by the decision form.
- It re-queues **only this case first**, the same queue path the report uses (`queue_pending_cases`, reported case first).
- The worker runs an evidence-only confirm: the same restricted sub-agent with a "record evidence, do not judge" prompt, or only the deterministic screenshot when `PERSON_EVIDENCE_LLM` is off. It records a failed person attempt with the evidence, then finalises the case Failed (`failed_by=help`) or Blocked (`help_skipped`) with today's result text.
- If the runner is offline, the gates already refuse queueing ("Saved, but the run cannot continue yet"). The person's decision must still stand, so the service writes the verdict directly as today and logs "no evidence: Test Runner unavailable".
- The worker path must keep "only a person marks Blocked": the worker applies the person's recorded decision; it does not decide.

Alternative, simpler, considered: keep the direct verdict and fire a Celery `execution`-queue task that only takes a screenshot through `controller_screenshot`, tagged with a new attempt id appended to the checkpoint. Rejected as the default: it races the next case on the same machine, and appending to the checkpoint outside the worker risks concurrent writes. Decided in Q1: the worker pass.

### TestRail evidence (projects)
- `testrail_evidence.select_evidence` needs no rule change:
  - The evidence attempts are final failed `step` attempts, so Failed picks their screenshots.
  - For Blocked, the person attempt at the cursor is a failed attempt, so `_blocked_attempt_ids` picks it.
- The TestRail result comment (`testrail_results_services` / `case_result_text`) includes the bounded evidence block of the final failed person attempts. The implementer confirms where the attempt summaries are assembled today.

### Settings
- `PERSON_EVIDENCE_OUTPUT_MAX_CHARS` (default 4000), in `settings.py` through decouple and in `example.env`.
- Optional `PERSON_EVIDENCE_LLM` (default true; false = screenshot only for Mark failed / blocked).

## Risks and mitigations
- **Extra screenshots**: a few MiB per person step plus one runner round trip, which is small next to a person's minutes.
- **Mark failed / blocked latency**: the verdict is final only after the worker runs (seconds). The card shows "Recording evidence…" like "The agent is checking…". A fault during the evidence pass (runner lost, AI service) does not open Need help: the worker applies the person's verdict anyway and logs "no evidence: <fault>" (Q2, decided).
- **Output with secrets**: the person may paste environment output. It is already sent to the LLM and stored on the row. The change adds the case log and TestRail, so the cap and the existing redaction (if any) apply. Mention it in the hint text.
- **Prompt injection through output**: it is fenced as data, as today.

## Implementation steps
1. Confirm prompt + guaranteed screenshot + bounded output capture. Files: `agents/services/person_step_prompts.py`, `agents/services/person_steps.py`, possibly `agents/services/sub_agent.py` / `agents/types.py`.
2. Evidence on the attempt and in the log. File: `agents/services/orchestrator.py`.
3. Mark failed / blocked through the worker. Files: `projects/person_step_services.py`, `projects/forms.py` (output on the decision form), `projects/services.py` (worker finalisation), `agents/checkpoint.py` (new `person` states), `templates/projects/includes/_person_step_card.html` ("Recording evidence…"), `projects/runner_person_step_services.py` (runner answers use the same path).
4. TestRail comment evidence. Files: `projects/testrail_results_services.py` / `projects/case_result_text.py`.
5. Settings. Files: `auto_tester/settings.py`, `example.env`.

## Verification
- `pre-commit run --all-files`, `manage.py check`.
- With a stubbed LLM, run the orchestrator for the "as expected" and "different" command steps with no tool calls from the model: a screenshot is still tagged with the attempt, and `error` / the log hold the bounded output.
- A live HITL run:
  - a command step reported Different with pasted output → the attempt has a screenshot and output, the log has `[Evidence]`, and TestRail Send attaches that step's screenshots and output;
  - Mark failed → "Recording evidence…" → a Failed case with an evidence attempt, and the next case starts only afterwards;
  - Mark failed with the runner offline → the verdict is saved with "no evidence".
- Run the `reviewer` and `verifier` agents.

## Open questions
Resolved by the user on 2026-10-31:
- Q1. Mark failed / blocked: **worker evidence pass** (not a direct verdict with a background screenshot).
- Q2. If the evidence pass faults (runner lost mid-capture): **keep the person's verdict**, log "no evidence"; it does not reopen as Need help.
- Q4. The evidence pass for Mark failed uses **the AI** (one read-only check), behind the `PERSON_EVIDENCE_LLM` switch (default true).

Not asked; kept as planned:
- Q3. An overrule takes no screenshot; passes send no TestRail evidence.

## Changelog
- 2026-10-31: created (Draft), split from `feat-tester-console.md` at the user's request.
- 2026-10-31: the user answered Q1 (worker pass), Q2 (keep the verdict) and Q4 (AI on with a switch); Q3 stays "no". Status Ready.
- 2026-10-31: implemented (In progress). First review pass, fixed:
  - Reviewer: an abort (or a runner drop, which aborts the run) while a mark waits for its evidence pass cancelled the case and lost the person's verdict. `abort_test_run` now calls `person_mark_verdict.settle_person_marks_on_abort` first: the verdict is written, the row closed, "no evidence: the run was stopped".
  - Reviewer: `apply_person_mark` locked the case before the person step; it now locks the person step first.
  - Reviewer: the evidence block made every verdict prompt bigger. `format_attempt_lines` now shows a person attempt's `error` head and tail only, capped at `EVIDENCE_PROMPT_MAX_CHARS` (1500). The log and TestRail keep the whole block.
  - Reviewer/verifier: the mark label matched the phrase anywhere in the summary; it now matches the headline only. The card gets `PersonStepPanel.marked_verdict` instead of comparing `close_reason` in the template. `ControllerNotConnectedError` gets its own "no evidence" text.
  - Verifier: a gate refusal on the last case left the run Paused with nothing to do. After the direct verdict, the service queues once more, and with no open case left the run finishes.
  - Verifier: an unusable checkpoint left the row open and the `marked_*` key stored. The row id now comes from the `person` key itself, the key is dropped, and "no evidence" is logged. The evidence pass also waits for the runner first (`wait_for_controller`, the usual reconnect grace).
- Not changed, with reasons:
  - Verifier: the evidence keeps the check commands' output but not the command text. The command is already in the case log's tool-call lines next to it, and the TestRail step text names the step's command.
  - Verifier: pasted output is not redacted before it reaches the case log and TestRail. This was accepted in Risks; the card hint now warns against pasting secrets.
  - Verifier: a mark on a restore hand-off records a `resume` attempt. For Failed, the TestRail selection then falls back to the last shots, which are that attempt's evidence shots anyway.
- Second review pass, fixed: the abort settle broadcasts the settled case and locks only person step rows (`of=("self",)`); every no-evidence path drops the `person` key; `close_marked_row` keeps the row's summary when there is no evidence; the web flash says "without evidence: <reason>" when the verdict was written at once.
- Second pass, accepted as is (documented):
  - A Needs re-test mark that lands during the evidence pass wins: the row closes as `marked_retest` and the person's earlier Failed/Blocked is not written. That is the later person decision.
  - A worker that dies hard during the evidence pass (no abort) leaves the case In progress with the `marked_*` key, like any crashed worker; a Resume then drops the key and the mark is lost.
- Third pass, fixed: the warning flash shown when the run cannot continue now also says the verdict was saved without evidence; `close_marked_row` builds its update fields first instead of unpacking them inline.
- Fourth pass: reviewer and verifier clean. Accepted as is: when the run stays gated after a no-evidence mark, the warning flash may repeat the same gate sentence (once as the no-evidence reason, once as the blocker). The message is still correct, and both agents marked it optional.
- 2026-10-31: Done. Change note: docs/changed/2026-10-31-hitl-failure-evidence.md.
