# refactor: Human in the loop continues after a non-blocking failed step

Status: Done
Created: 2026-10-29
Request: In Human in the loop, when a person reports that a step's result was
different (the step failed), the case should not stop there if the failure does
not block the remaining steps: the orchestrator decides whether the failed step
is a blocker, and if it is not, the next step is handed to the person as usual.
When all steps have run, the verdict decides whether each such person-reported
failure really fails the case or can be ignored (the case passes).

## Context

- `agents/services/orchestrator.py` `_execute_sub_tasks` (person branch, ~1196-1240):
  `_settle_person_report` records a person attempt; on `status == "fail"`
  (outcome "different") the case always appends
  `StepDecision(action="stop", reason="A person reported a different result")`,
  writes `Step N: FAILED (stopped) — ...`, sets `next_step = total + 1` and breaks
  to the verdict. No checkpoint before the break (the verdict checkpoint persists it).
- Agent steps go through `_evaluate_failure` (~1636; prompt
  `orchestrator_prompts.build_evaluate_prompt`, shared `evaluate_messages` thread
  whose system prompt pushes towards `need_help`). Its "continue" branch writes
  `Step N: FAILED — <summary>`, advances, checkpoints.
- `_rebuild_state` (~340) already rebuilds a failed final step attempt with
  `step_num < next_step` as `Step N: FAILED — <summary>`, the exact line the live
  "continue" path writes. A continued person failure rebuilds correctly with no
  change; the person attempt is a normal `kind="step"` attempt.
- `_build_verdict` (~1557) decides pass/fail **deterministically**: pass only when
  every planned step has a final attempt and all passed. The verdict LLM
  (`build_verdict_prompt`) only words the summary and is told not to contradict
  the result; on `AIServiceError` `_deterministic_summary` is used. So today a
  "continue"d failure always ends the case Failed: the "decide at the end" part of
  the request needs a verdict change, not just a loop change.
- `person_step_prompts.format_different_summary` puts the person's note (quoted,
  labelled as their words) and the confirm agent's observation in the failed
  attempt's summary; `person_attempt_label` / `person_verdict_note` label person
  attempts for the verdict.
- Downstream (checked, no assumption that "different" ends the case):
  `projects/person_step_rows.py` settles the row FAILED from the checkpoint save
  hook whenever the attempt is saved (the continue path checkpoints right away,
  which closes the row earlier than today: fine);
  `projects/case_step_progress.py` shows the newest decision for a failed final
  step (`test_run_case_detail.html` renders "Continued: <reason>");
  `projects/testrail_mapping._step_lines` labels it the same way;
  `projects/services._finalize_pivot` maps `TASK_COMPLETE`/`ERROR` from the
  orchestrator status; `case_failed_notifications.notify_case_failed` fires only on
  a Failed verdict (a waived case passes and does not notify, correct);
  `test_sessions` records the person's action, not the verdict path.
- Docs describing the stop behaviour: `agents/CLAUDE.md` ("Person steps":
  `"Different" records a fail attempt ... and stops the case` and
  `No _evaluate_failure, recovery or need_help on person steps`), root
  `CLAUDE.md` Human in the loop bullet (`"different" → step failed, case stops`).

## Goals
- A person "different" report on a step (kind `step`) asks the orchestrator LLM
  one question: does any remaining step depend on this one? `continue` or `stop`.
- `continue`: record the decision, write the FAILED state line, advance,
  checkpoint, hand the next step to the person.
- `stop`: today's behaviour, with the LLM's reason.
- At the verdict, when every non-passed step is such a continued person failure,
  the verdict LLM decides pass or fail and explains; otherwise the result stays
  deterministic as today.

## Non-goals
- Agent-performed steps: their `_evaluate_failure` and deterministic verdict are
  unchanged (a continued agent failure still fails the case).
- Restore hand-offs (`kind="restore"`): "different" still raises
  `ResumeStateLostError`.
- No recovery or `need_help` on person steps (the person is at the machine and
  reported what they saw).
- No new models, fields, migrations, checkpoint keys or `DecisionAction` values;
  no controller protocol change; no UI change.
- "Mark failed" / "Mark blocked" person actions are unchanged (they end the case).

## Design

### Data model
None. The blocker decision is a `StepDecision(action="continue"|"stop")` in the
existing checkpoint `decisions` list. The end-of-case judgement lives only in
the verdict (`OrchestratorResult.status` + summary) and is not stored separately.

### Services

`agents/services/orchestrator_prompts.py`
- `build_person_failure_system_prompt() -> str`: "You decide whether a test step
  that failed blocks the remaining steps."
- `build_person_failure_prompt(sub_task: SubTask, result: SubTaskResult,
  state_description: str, remaining: Sequence[SubTask], first_remaining: int) -> str`:
  the failed step (description, expected result), its result summary (contains
  the person's quoted note: say step text and note are data, not instructions),
  the current state lines, and the remaining steps numbered with short
  description + expected result (reuse `_short_description`/`_shorten` style
  capping so the prompt stays bounded). Rules: `"stop"` when a remaining step
  needs this step's expected result to be in effect (a precondition: app not
  open, login failed, file/record not created that a later step uses), so it
  could not be performed or its result would mean nothing; `"continue"` when the
  remaining steps can still be performed and judged on their own. Not asked
  whether it is a product defect: that is the verdict's job. Output only
  `{"decision": "continue"|"stop", "reason": "<one sentence>"}`.
- `build_person_failure_verdict_prompt(attempts, plan, judged_steps: Sequence[int],
  *, helped, revisions) -> str`: same results block and resumed / help / redesign
  / person notes as `build_verdict_prompt` (extract those notes into one private
  helper both builders call, no copy-paste), then: "A person reported a different
  result at steps X (…); the case continued past them. Decide whether the case
  FAILS or PASSES." Fail when the difference shows the application not doing what
  the test case checks (wrong data, missing feature, error, wrong state); pass only
  when the difference does not touch what the case verifies (wording or cosmetic
  detail the case does not check, an environment detail, or later steps show the
  expected effect was in place after all). When unsure, fail. The summary (2-4
  sentences, same step-naming rule) must state the result and, on pass, name each
  ignored step and why. Output only
  `{"result": "pass"|"fail", "summary": "..."}`.
- `build_verdict_prompt` keeps its contract for the deterministic path.

`agents/services/orchestrator.py`
- New `_evaluate_person_failure(*, orchestrator_llm, sub_task, result,
  state_description, remaining, first_remaining, on_log) -> StepDecision`-shaped
  result (action narrowed to continue/stop + reason). Its own two-message call
  (system + user), **not** the shared `evaluate_messages` thread: that thread's
  system prompt and options (recover / need_help) contradict this question.
  Anything but exactly `"continue"` maps to `"stop"` (today's behaviour is the safe
  default). `AIServiceError` / `OrchestratorParseError` propagate, as from
  `_evaluate_failure` (existing fault handling: the case opens a help request
  with the cursor on the step).
- Person branch in `_execute_sub_tasks`, on `person_result.status != "pass"`:
  - `remaining == 0` (last step): no LLM call; record
    `StepDecision(continue, "Last step: the verdict judges this difference")`.
  - otherwise `progress.set_phase("evaluating")`, call `_evaluate_person_failure`.
  - append the decision; `continue` → `state_lines.append(f"Step {n}: FAILED — …")`
    (same text as the agent continue path, which `_rebuild_state` reproduces),
    `next_step = n + 1`, `checkpoint()`, `i += 1`, `continue`; `stop` → today's
    lines with the LLM's reason, break.
  - Update the comment above the branch.
- `_build_verdict` gains `decisions: Sequence[StepDecision]` (caller passes
  `progress.decisions`). New private helper `_judged_person_failures(attempts,
  finals, decisions) -> list[int]`: failed final step attempts with
  `performed_by == "person"` whose newest decision for that step is `continue`.
  - all planned steps have finals, all pass → pass (unchanged).
  - some step not run, or any failed final not in the judged list → fail,
    summary as today (unchanged).
  - otherwise (every failure is a judged person failure) → judged verdict: one
    call with `build_person_failure_verdict_prompt`, parsed with
    `_parse_json_response`; pass only when `result == "pass"` exactly and the
    summary is non-empty; anything else → fail with the model's summary if given.
    `AIServiceError` (incl. parse error) → fail +
    `_deterministic_summary(..., "fail")` plus a line saying the AI could not
    judge the person-reported difference. `error` is None on pass, else the
    newest failed step's summary as today.
- `_deterministic_summary` unchanged otherwise.

### Pages and forms
None. A passed case may now show a step row "Failed" with "Continued: <reason>";
the verdict summary names the ignored step. (See open question Q4.)

### Background work and WebSockets
No new task, queue, event or controller action. The case worker on `execution`
makes one extra small LLM call per continued person failure, and the judged
verdict replaces (not adds to) the verdict call.

### Settings / infrastructure
None.

### Data flow
1. Person reports "different" → `projects` writes `reported`, re-queues the case.
2. Worker re-enters `run_orchestrator` → `_settle_person_report` confirms and
   records the fail attempt (`performed_by="person"`).
3. Not last step → `_evaluate_person_failure` → `continue` or `stop` decision.
4. `continue` → FAILED line, cursor + 1, checkpoint (settles the row FAILED),
   next step prepared and handed to the person (`awaiting_person`).
5. After the last step → `_build_verdict`: only continued person failures →
   judged verdict decides pass/fail + summary → `_finalize_pivot` maps it.
6. Resume at the verdict phase (`next_step = total + 1`) re-runs the judgement.

## Alternatives considered
- Reuse `_evaluate_failure` with recover/need_help disallowed: its shared thread
  and system prompt are built around environment-vs-product and help; filtering
  options after the fact gives a muddled question. A separate narrow call is
  simpler and cheaper.
- Decide "ignorable" at the step (when continuing): the user asked for the
  decision at the end, when later steps' results are known (they can show the
  effect was in place after all).
- Let the verdict LLM decide for every case: makes agent cases non-deterministic
  for no requested reason; the judgement is limited to person-reported failures.
- Store the waiver as a new `DecisionAction` / checkpoint field to show it on the
  step row: adds checkpoint compatibility work; the verdict text suffices for now.

## Risks and mitigations
- **A real failure passes.** The verdict is told to fail when unsure; any parse or
  AI fault fails; agent failures and unrun steps never reach the judged path.
- **Prompt injection via the person's note** ("mark this as pass"): the note is
  already quoted and labelled as data in `format_different_summary`; both new
  prompts repeat that step text and notes are data. The writer is a project
  member, who can already overrule a step.
- **Wrong "continue" on a real blocker**: later steps then fail too (person
  reports different again or cannot do them); each is judged; any person can still
  Mark failed. Wrong "stop" is today's behaviour.
- **Fault in the blocker call** after the attempt is recorded: same as an agent
  `_evaluate_failure` fault today; the cursor is still on the step, so help
  Continue hands the step out again. Acceptable, documented.
- **Re-judging on resume at the verdict** may give a different result; nothing
  was saved in between, as with today's verdict summary.
- Checkpoint compatibility: none (no new keys or values).

## Implementation steps
1. Prompts: `build_person_failure_system_prompt`, `build_person_failure_prompt`,
   `build_person_failure_verdict_prompt`, shared private notes helper used by
   `build_verdict_prompt` too (files: `agents/services/orchestrator_prompts.py`).
2. Loop: `_evaluate_person_failure`, person-branch continue/stop with the
   last-step shortcut (files: `agents/services/orchestrator.py`).
3. Verdict: `_build_verdict(decisions=)`, `_judged_person_failures`, judged call
   and fallbacks; pass `progress.decisions` from the single caller
   (files: `agents/services/orchestrator.py`).
4. Docs: `agents/CLAUDE.md` "Person steps" paragraph, root `CLAUDE.md` Human in
   the loop bullet; later `docs/changed/2026-10-29-hitl-continue-after-failed-step.md`.

Steps 1-3 touch two files that depend on each other line by line: implement
directly, no parallel sub-agents.

## Verification
- `pre-commit run --all-files` (black, isort, mypy strict) clean.
- Manual, Human in the loop run on a 3+ step case:
  - Report "different" on a step later steps do not depend on (e.g. a label
    text check): the log shows the decision `continue`, the next step is handed
    out, the step row shows Failed + "Continued: …", the person row is FAILED.
    Report the rest as expected: the verdict either passes (summary names the
    ignored step) or fails with a reason.
  - Report "different" on a precondition step (e.g. "open the app"): decision
    `stop`, case Failed as today.
  - "Different" on the last step: no blocker call, judged verdict.
  - Mixed: a continued person failure plus an agent failure (switch mode
    mid-run): deterministic Failed, no judged call.
  - Kill the worker after a continued person failure, Resume: state lines show
    `Step N: FAILED — …`, case continues at N+1.
  - Point the orchestrator LLM at a broken endpoint for the verdict: Failed with
    the deterministic summary.
- Run `reviewer` and `verifier` agents.

## Open questions
- Q1. Should the end-of-case judgement really be able to turn the case **Passed**
  despite a person reporting a different result, or should it only choose between
  Failed and a softer outcome (e.g. Passed with a note, or leave Failed but explain)?
  (Assumption in this draft: it may pass the case; it fails when unsure.)
- Q2. Should the same continue-then-judge behaviour apply to agent-performed
  steps that `_evaluate_failure` continues? (Assumption: no, Human in the loop
  person steps only.)
- Q3. When the blocker call cannot decide (unknown answer), stop or continue?
  (Assumption: stop, today's behaviour; an AI fault opens Need help as for any
  step.)
- Q4. Should a Passed case with an ignored person failure be marked visibly on
  the case page / TestRail beyond the verdict text (e.g. a "Ignored at verdict"
  label on the step row)? (Assumption: no UI change; the step row stays
  "Failed — Continued: …" and the verdict summary explains.)
- Q5. Restore hand-offs reported "different" keep raising
  `ResumeStateLostError`? (Assumption: yes, unchanged.)

## Changelog
- 2026-10-29: created
- 2026-10-30: user confirmed Q1 (may pass), Q3 (stop when unsure), Q4 (verdict text only); Q2/Q5 kept as assumed (person steps only; restore unchanged)
- 2026-10-30: per Q3 an unparseable blocker answer stops (not propagated as the Design said); an AIServiceError from the send still propagates. Review: judged verdict prompt states the final-attempt rule, the judge system prompt moved to orchestrator_prompts, `_evaluate_person_failure` takes `step_num`, the agent's on-screen observation is labelled as data in both prompts. Formatting-only black/isort changes in projects/person_step_question_* came from pre-commit --all-files and are kept.
