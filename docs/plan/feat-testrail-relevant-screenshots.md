# feat: Send only the relevant screenshots to TestRail

Status: Done
Created: 2026-10-28
Request: "Send checked to TestRail" attaches every screenshot a case took, which
is mostly noise. A Passed case should attach no screenshots. A Failed case should
attach only the screenshots that show what went wrong. To get there, each
screenshot has to be linked to the part of the case log it belongs to (the step
attempt that took it). When a case is sent, the server works out which part of
the log caused the failure and attaches only that part's screenshots.

## Context

- **Send flow** (`projects/testrail_checked_push_services.py`, task
  `projects.tasks.push_checked_results_to_testrail` on the `upload` queue):
  - A person checks cases (`projects/run_case_check_services.py::check_run_cases`,
    one UPDATE, called from `projects/views.py` `test_run_cases_check`), then
    presses Send (`testrail_send_checked`).
  - `plan_checked_push` prefetches `_evidence_screenshots()`: every
    `TestRunScreenshot` of the pivot except `controller_omniparser` overlays,
    ordered `(created_at, id)`.
  - `_attach_screenshots` uploads each file with `add_attachment_to_result` and
    stores `TestRunTestCase.testrail_source_attachment_count` after every file.
    That counter is a resume index into the ordered list, so the list must not
    change between a send and its resume (`docs/changed/2026-10-22-testrail-checked-results.md`,
    "Notes for future changes").
  - The panel (`get_checked_push_panel`), `_has_pending` and `_RESUME_Q` count
    screenshots in SQL with the `_with_planned_shot_count` subquery.
  - The comment (`testrail_mapping.build_checked_result_comment`) says
    "N screenshots attached". Attachment names come from
    `screenshot_attachment_filename` (`C123_02_click.png`).
  - There is no auto-send. The whole-run 018 "Push to TestRail"
    (`testrail_results_services.py`) attaches no screenshots and is out of scope.
- **Screenshots** (`projects/services.py`):
  - `_build_screenshot_callback(pivot, generation)` becomes the
    `ScreenshotCallback(base64_data, tool_name)` (`agents/types.py`).
  - `_persist_screenshot` creates `TestRunScreenshot(test_run_test_case, image, tool_name, created_at)`.
  - The emitters are `tools_controller` (`take_screenshot`,
    `browser_take_screenshot`), `verified_element_actions` (the after-action
    image under the tool name) and `controller_element_finder` (overlays).
  - Screenshots carry no link to a step, an attempt or a log line.
- **Log**: `TestRunTestCase.logs` is one text blob, appended line by line by
  `_append_pivot_log`. The orchestrator writes these markers:
  - `[Orchestrator] Sub-task N/M: ...` and `Result: PASS|FAIL — ...`;
  - `Recovery ...` and `Restoring state before step N`.
  - Tool calls in between are written by `agent_loop`.
  - The structured form of the same information is the checkpoint
    (`execution_checkpoint`, `agents/checkpoint.py`). It lists the attempts
    (`SubTaskAttempt`: `step_num`, `kind` step/recovery/resume, `result.status`)
    and the decisions. Pass/fail is already decided without the LLM:
    `orchestrator._build_verdict` uses each step's final `step` attempt
    (`final_step_indices`). The LLM only words the summary.
- **Result chat** (`machine_setup/services/result_chat_tools.py`):
  - `read_case_log` pages through the raw `logs` text by character offset;
  - `ask_case_screenshot` takes a `TestRunScreenshot` id;
  - neither has a step link.
- **Lifecycle that affects which screenshots exist**:
  - Re-run from scratch (`run_rerun_services`) and Reset (`reset_test_run`)
    delete screenshots and logs.
  - Resume, Continue after help, and Re-run with redesigned plan keep the log,
    the screenshots and the checkpoint. A redesign moves the earlier plan's
    attempts into `plan_revisions`. Resume from an earlier step
    (`truncate_checkpoint`) drops later attempts.
  - A step interrupted by a fault leaves screenshots but no recorded attempt.
- **Writers of the final status**:
  - `_finalize_pivot` (agent verdict Passed/Failed);
  - `case_help_services.mark_blocked_after_help` (Blocked);
  - `case_result_change_services` Apply (any to any).
  - Apply and every restart clear Checked/Sent (`run_reopen_services.clear_checked_and_sent`).

## Goals

- Passed: no screenshots attached; the comment does not mention screenshots.
- Failed: only the screenshots of the step attempt(s) that caused the failure,
  capped.
- Blocked: the screenshots of the attempt the case stopped on (same idea as Failed).
- Each new screenshot records which step attempt took it (attempt id + step number).
- Stale screenshots never count:
  - from earlier plans (redesign);
  - from truncated or interrupted attempts;
  - from earlier executions.
- Send resume stays duplicate-free; the panel counts stay SQL-only (no N+1).
- Cases executed before this change still send something sensible.

## Non-goals

- Changing the 018 whole-run push, the result chat tools or the case page
  gallery (see Q5).
- An LLM choosing the screenshots (see Alternatives).
- Moving the log to a structured table. The log stays a text blob.
- Re-selecting screenshots for results already sent.

## Design

### The link: screenshot → step attempt (the "log line" unit)

What the user calls "log lines" maps onto the **step attempt**. Each attempt is
one block of the log: its `Sub-task N/M` line, its tool calls and its
`Result:` line. It is also the unit the orchestrator already uses to decide
pass/fail. Tagging a screenshot with its attempt gives a reliable cause → evidence
link without parsing the log text.

- `SubTaskAttempt` gains `attempt_id: str = ""`, a random `uuid4().hex`
  generated by the orchestrator before each `run_sub_agent` call (step,
  recovery, resume restore).
  - It is serialized as an optional `attempt_id` key by `agents/checkpoint.py`,
    with `CHECKPOINT_VERSION` unchanged, as the module's rule requires. Old
    checkpoints parse with `""`.
  - `PlanRevisionRecord.earlier_attempts` may drop it.
  - Synthetic carried attempts (`carried_from`) have none.
- The orchestrator wraps the case's `on_screenshot` per attempt, so every
  screenshot taken inside that sub-agent is reported with
  `(attempt_id, step_num)`. Tools keep calling `on_screenshot(data, tool_name)`.
  `ScreenshotCallback` gains keyword-only `attempt_id: str = ""` and
  `step_num: int | None = None`. Only the orchestrator's wrapper fills them, so
  no tool signature changes.
- Why a random id and not `(plan_version, attempt index)`: an attempt
  interrupted by a fault is never recorded. Its re-run would get the same index
  and inherit the interrupted attempt's screenshots. A random id that only
  appears in the checkpoint once the attempt is recorded makes orphans and
  stale attempts drop out automatically, and the same holds for truncation and
  redesign.

### Data model (migration `projects.0033_screenshot_attempt_evidence`)

`TestRunScreenshot` gains:
- `attempt_id = CharField(max_length=32, blank=True, default="")`: empty for
  pre-change rows, teardown and other untagged captures.
- `step_num = PositiveSmallIntegerField(null=True, blank=True)`: used for the
  attachment name and display.
- `testrail_evidence = BooleanField(default=False)`: the frozen selection for
  the next send. It is meaningful only while the pivot is Checked.

Data step (RunPython, one UPDATE): set `testrail_evidence=True` on every
non-overlay screenshot of pivots that are **currently checked**. This preserves
the current behaviour for checked-but-unsent cases and, more importantly, for
partially sent cases. Their `testrail_source_attachment_count` indexes the full
ordered list. Reverse is a no-op. No data loss. Adding the field with a constant
default is a metadata-only change on Postgres 11+. The UPDATE touches only
checked cases' rows.

### Services

- **`projects/testrail_evidence.py`** (new, pure, no ORM). It holds
  `select_evidence(status, checkpoint, shots) -> list[int]`. `shots` are light
  rows `(id, attempt_id, step_num, tool_name)` in `(created_at, id)` order,
  overlays already excluded. Rules:
  1. **Passed** → `[]`.
  2. **Failed**:
     - The causing attempts are the final `step` attempt of every step whose
       final attempt failed (`final_step_indices` on `checkpoint.attempts`;
       usually one step, more when the orchestrator chose `continue`).
     - From each, the last `TESTRAIL_EVIDENCE_SHOTS_PER_STEP` screenshots.
       Those are the end of the attempt, where the failure is visible.
     - Overall cap `TESTRAIL_EVIDENCE_MAX_SHOTS`, keeping the latest steps.
  3. **Blocked** (always a person's decision): the causing attempt is the last
     failed attempt of any kind at the checkpoint cursor (`next_step`, the help
     step), with the same per-step cap. A Blocked set by Apply on a case whose
     checkpoint shows a failed step uses rule 2's attempts.
  4. **Fallback**: the last `TESTRAIL_EVIDENCE_SHOTS_PER_STEP` screenshots of
     the pivot, regardless of tag. It applies when:
     - the case is Failed or Blocked with no causing attempt, or the causing
       attempts have no tagged screenshots;
     - legacy untagged data;
     - a person changed Passed → Failed (all steps passed);
     - a fault interrupted the step before an attempt was recorded;
     - the checkpoint is missing or unparseable.

     Re-run from scratch deletes old screenshots, so "the pivot's latest" is
     always this execution or its resumed segments.
- **`projects/run_case_check_services.py::check_run_cases`** becomes the single
  writer of the selection. Checking is already the gate for sending, and every
  path that changes a result afterwards clears Checked (Apply, Resume, Re-run,
  redesign, Reset), so a re-check always reselects. In one transaction:
  1. Lock and load the selectable pivots (`select_for_update`,
     `.only("id", "status", "execution_checkpoint")`, no logs).
  2. Load their screenshot rows with one `values_list` query (overlays excluded,
     ordered).
  3. Compute in Python with `select_evidence`.
  4. One UPDATE clears `testrail_evidence` for those pivots' screenshots; one
     UPDATE sets it on the selected ids.
  5. Run the existing pivot UPDATE.

  That is a constant 5 queries for any selection size. The selection logic sits
  in a small helper module `projects/testrail_evidence_services.py`
  (`freeze_testrail_evidence(pivot_ids)`), which the check service calls, so
  check stays focused.
- **`projects/testrail_checked_push_services.py`**:
  - `_evidence_screenshots()` and `_with_planned_shot_count` add
    `testrail_evidence=True`; the overlay exclusion stays as a belt.
  - The panel, `_has_pending`, `_RESUME_Q`, `plan_checked_push` and
    `_attach_screenshots` are otherwise unchanged. The prefetch adds `step_num`
    to `.only()`.
  - Resume safety holds: the flags are frozen at check, uncheck is refused
    once sent, and Apply, Reset and Re-run clear Sent.
- **`projects/testrail_mapping.py`**:
  - `screenshot_attachment_filename` gains an optional `step_num` →
    `C123_s03_01_click.png` (`_01` stays the index within the case).
  - `build_checked_result_comment` already omits the screenshot line at 0. For
    Failed it may add "Screenshots from step 3" (wording only; see Q4).
- **`projects/services.py`**: `_build_screenshot_callback` / `_persist_screenshot`
  accept and store `attempt_id` / `step_num`. No change to `_broadcast_screenshot`
  (Q5).
- **`agents/services/orchestrator.py`**: a per-attempt id and config. A helper
  replaces `_config_for_sub_task` at the three `run_sub_agent` call sites
  (`_execute_sub_tasks`, `_attempt_recovery`, `_restore_resumed_state`) and
  returns a config whose `on_screenshot` is tagged. The id is stored on the
  appended `SubTaskAttempt`. The orchestrator still knows nothing about the ORM
  or TestRail.

### Pages and forms

No new URLs, forms or templates.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_cases_check` (existing) | POST | `@project_membership_required` | `RunCaseSelectionForm` (unchanged) | redirect + flash (unchanged) | `RunCaseCheckError` (unchanged) |
| `projects:testrail_send_checked` (existing) | POST | `@project_membership_required` | none | run page; the panel's `ready_screenshot_count` / confirm body now count only the selected shots | unchanged |

The run page confirm dialog ("... with N screenshots") becomes accurate on its
own, because it reads the same SQL count.

### Background work and WebSockets

- The send task, its queue (`upload`) and its limits are unchanged; it attaches
  fewer files, so it finishes sooner.
- No new events. `test_run_case.screenshot` is unchanged (Q5).
- No controller or vision worker protocol change. Tagging happens server-side
  in the orchestrator wrapper, so old controllers are unaffected.
  `CLIENT_VERSION` and capabilities are unchanged.

### Settings / infrastructure

These are read in `auto_tester/settings.py` with `config(..., cast=int)` and
added to `example.env`:
- `TESTRAIL_EVIDENCE_SHOTS_PER_STEP` (default 3).
- `TESTRAIL_EVIDENCE_MAX_SHOTS` (default 10).

Deploy: run `migrate`, then restart Daphne/web, the `execution` workers (new
tagging) and the `upload` worker.

### Data flow

1. A case runs. Before each sub-agent, the orchestrator mints `attempt_id` and
   passes a tagged screenshot callback. `_persist_screenshot` stores
   `attempt_id` and `step_num`, and the checkpoint records the attempt with the
   same id.
2. The case finishes, or is Blocked or Applied by a person. Nothing about
   evidence happens yet.
3. A person checks cases. `check_run_cases` locks them, loads checkpoints and
   screenshot rows in two queries, and runs `select_evidence`. It writes the
   flags with two UPDATEs and the checked mark with one.
4. The panel counts flagged shots in SQL. Send → task → `plan_checked_push`
   prefetches only flagged shots → `add_results_for_cases` → attach the
   flagged files in order, with a resumable counter.

## Alternatives considered

- **LLM picks the causing log lines at verdict time** (the verdict returns
  attempt ids). Rejected as the primary rule:
  - Pass/fail is already decided deterministically from step attempts, so
    the cause is known without a model.
  - An LLM pick adds cost, can return wrong ids, and is lost when the verdict
    falls back to `_deterministic_summary`.
  - It also does not cover person-made results (Apply, Mark blocked).

  It could later refine which screenshots *within* the failing attempt to send
  (see Q2).
- **Tag with `(plan_version, attempt index)` or `execution_generation`
  instead of a random id**: an interrupted attempt and its re-run collide; the
  generation is not in the checkpoint. Rejected.
- **Tag with a log line number/offset** (store `Length(logs)` at capture): this
  gives a literal log-line link for display. It does not help choose the
  evidence, though, and costs an extra query per screenshot. Deferred (Q5).
- **Select at send time in Python instead of freezing flags at check**: the
  panel, `_has_pending` and `_RESUME_Q` would have to parse every checkpoint of
  the run on each page render, and the resume index would depend on recomputation.
  Rejected.
- **Recompute flags at every status writer** (`_finalize_pivot`, Mark blocked,
  Apply): three writers instead of one, and none of them covers legacy cases.
  Rejected. Check time is the one gate every send passes through.
- **A separate evidence table**: more joins for no gain over a boolean on the
  screenshot row. Rejected for simplicity.

## Risks and mitigations

- **Resume of a partially sent result**: the flags change the ordered list the
  counter indexes. Mitigation:
  - the migration flags all non-overlay screenshots of checked pivots;
  - after deploy, flags only change at check, which is refused once sent;
  - Apply, Reset and Re-run clear Sent and the counter.
- **Check of a large run** ("check all", a few hundred cases) loads their
  checkpoints, typically tens of KB each. Mitigation: `.only()` the three
  fields, never logs; the query count is constant. Iterate with `.iterator()`
  if memory measurements warrant it.
- **Concurrent Apply/Resume during a check**: the pivots are locked in the
  check transaction. Apply and Resume lock the pivot too, then clear Checked,
  so the last writer wins consistently.
- **Losing the failure evidence** when the cap is too small or the tagging misses
  (a fault before the attempt was recorded): the fallback sends the latest shots;
  the case page keeps every screenshot and the comment links to it.
- **Checkpoint compatibility**: the new key is optional. Older parsers ignore
  it (rollback-safe), so `CHECKPOINT_VERSION` is not bumped.
- **Populated table migration**: additive fields with constant defaults, plus
  one bounded UPDATE. No data loss.

## Implementation steps

1. Agent side tagging (files: `agents/types.py` (`SubTaskAttempt.attempt_id`,
   `ScreenshotCallback` keyword-only params), `agents/checkpoint.py` (optional
   `attempt_id` key), `agents/services/orchestrator.py` (per-attempt id + tagged
   config at the three `run_sub_agent` sites)). Check `agents/plan_revision.py`
   and `apply_plan_revision` still build `SubTaskAttempt` correctly (default `""`).
2. Model + migration (files: `projects/models.py`,
   `projects/migrations/0033_screenshot_attempt_evidence.py` with the RunPython
   backfill for checked pivots), `projects/admin.py` (read-only display of the
   new fields).
3. Persist tags (files: `projects/services.py`: `_build_screenshot_callback`,
   `_persist_screenshot`).
4. Selection (files: new `projects/testrail_evidence.py` (pure rules), new
   `projects/testrail_evidence_services.py` (`freeze_testrail_evidence`),
   `auto_tester/settings.py`, `example.env`).
5. Freeze at check (files: `projects/run_case_check_services.py`: transaction,
   lock, call the freeze, then the existing UPDATE).
6. Send uses the flags (files: `projects/testrail_checked_push_services.py`:
   `_evidence_screenshots`, `_with_planned_shot_count`, prefetch `.only`;
   `projects/testrail_mapping.py`: filename with step, optional comment wording).
7. `CLAUDE.md` / `agents/CLAUDE.md` one-line notes, and `docs/changed/` entry.

Steps 1 and 2 are independent. Step 3 needs both. Steps 4 and 5 need step 2.
Step 6 needs step 2.

## Verification

- `pre-commit run --all-files` (strict mypy), `python manage.py check`,
  `python manage.py makemigrations --check` after the migration is written.
- Migration on a copy of the dev DB: checked pivots' non-overlay screenshots
  are flagged; others are not; the panel shows the same counts as before for
  already-checked cases.
- Run a case that passes, check it: the panel shows 0 screenshots; the send adds
  the result with no attachments and no screenshot line in the comment.
- Run a case that fails at step 3 after a recovery: only the last ≤3 screenshots
  of step 3's final attempt are flagged; filenames read `C<id>_s03_..`.
- Fail → Need help → Continue → fails again: only the second attempt's shots.
- Need help → Mark blocked: the shots of the failed attempt at the help step.
  If a runner fault interrupted the step: the fallback picks the latest shots.
- Result chat Apply Passed → Failed, then check: the fallback picks the latest
  shots. Failed → Passed: none.
- Re-run with redesigned plan, then fail: no screenshot from the earlier plan is
  flagged.
- A legacy (pre-change) Failed case checked after deploy: the fallback picks
  the latest shots.
- Interrupt a send mid-attachments (kill the `upload` worker) and send again: no
  duplicates, the counter resumes.
- Query counts: check 1 vs 100 cases gives the same number of queries
  (`CaptureQueriesContext` in a shell smoke on a throwaway DB). The panel query
  count is unchanged (2).
- Run the `reviewer` and `verifier` agents.

## Open questions

- Q1 **Blocked**: should a Blocked result attach screenshots? Blocked means a
  person decided the environment cannot be fixed, not a product failure.
  (Assumption: yes, the shots of the attempt the case stopped on, same caps as
  Failed.)
- Q2 **How many per failing step / overall?** (Assumption: the last 3
  screenshots of each failing step's final attempt, at most 10 per case, both
  env-configurable.) Alternative: all screenshots of the failing attempt, or
  only the very last one.
- Q3 **Which attempts count as "causing"?** (Assumption: only the final
  attempt of each failed step. Not earlier failed attempts of the same step
  that a recovery then fixed, not failed recovery attempts, not steps that
  passed.)
- Q4 **Comment content**: keep the full verdict text and step breakdown as
  today, and add "Screenshots from step N"? (Assumption: yes, keep everything,
  add the step note; the raw log is not sent today and still is not.)
- Q5 **Literal log-line link**: do you also want the link visible in Punk
  Hazard? Options: a "[Screenshot #id]" marker written into the case log at
  capture, a step label on the case page gallery, or "will be sent to TestRail"
  badges. (Assumption: not in this change; the link is stored
  (`attempt_id`, `step_num`) and only used for TestRail.)
- Q6 **Person-changed results** (Apply Passed → Failed, where every step
  passed): send the latest screenshots as the fallback, or none? (Assumption:
  the latest 3.)
- Q7 **Cases already checked before deploy but not yet sent**: keep sending all
  their screenshots (the migration preserves the old behaviour), or apply the
  new rule? (Assumption: keep the old behaviour; uncheck and re-check applies
  the new rule. Partially sent cases must keep the old list either way.)

## Changelog

- 2026-10-28: created
- 2026-10-28: user answered Q1, Q2, Q5 and Q6 with the assumptions above (Blocked
  attaches the stopping attempt; last 3 per step, at most 10; no visible link in
  Punk Hazard; person-made Failed sends the latest 3). Q3, Q4 and Q7 keep their
  assumptions. Status In progress.
- 2026-10-28: `ScreenshotCallback` stays unchanged (machine_setup's activity
  recorder implements it). Instead a new `AttemptScreenshotCallback(base64_data,
  tool_name, *, attempt_id, step_num)` is what `run_orchestrator` takes; the
  orchestrator wraps it per attempt into a plain `ScreenshotCallback`.
- 2026-10-28: review round 1 fixed: check loads checkpoints and screenshot rows
  only for Failed/Blocked cases (lock `.only("id", "status")`);
  `final_step_indices` moved to `agents/checkpoint.py`; Blocked first picks the
  attempt a fault interrupted at the cursor (newest tagged shot at `next_step`
  whose id was never recorded), so `EvidenceShot` carries `step_num`.
  Rejected: leaving `step_num` unset on resume-restore screenshots. They are
  only ever sent by the fallback, and "step N" (the step they prepare) is a
  fair label; making `step_num` optional on the callback would add a case for
  little gain.
