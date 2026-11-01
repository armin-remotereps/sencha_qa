# feat: Manual run mode

Status: Done
Created: 2026-11-01
Request: Add a fourth run mode, **Manual**, next to Attended, Night and Human in
the loop. In Manual a person performs every case end to end and decides every
result. The AI does not plan, run, check or judge anything. The Test Runner
window lists the case's original steps, taken verbatim from the test case with
no AI rewording. Each step has a "Pass" button, which takes a screenshot, and a
"Fail" button, which takes a screenshot and asks for a note. Below the steps,
the tester marks the whole case Passed, Failed or Blocked. Screenshots are kept locally
and uploaded only after the tester finishes the case, so the tester can go
straight to the next case. When results go to TestRail, the AI rewrites the
tester's notes so the wording is clean and every case reads the same way. It
never changes a status.

## Context

- Run modes: `projects/models.py` `RunHelpMode` (`attended`, `night`, `hitl`;
  `TestRun.help_mode` `max_length=16`), `Project.default_help_mode`. A
  superuser chooses a mode through `projects/run_help_mode_policy.resolve_help_mode`.
  Mid-run switches go through `projects/run_help_mode_services.change_run_help_mode`
  and the project card through `projects/project_run_mode_services.change_project_run_mode`
  (scopes `new_only/drafts/open/all`; `all` also rewrites finished runs).
- Start: `projects/services.start_test_run` sets STARTED, bumps
  `execution_generation`, records the session activity and credit, and queues a
  Celery chain of `execute_test_run_case` (`queue_test_run_cases`). The run
  finishes through `_update_test_run_status_if_needed` / `finish_test_run`
  (conditional on generation). Readiness: `get_run_readiness` (runner
  connected, visual engine, no active run, no setup turn, no runner update,
  application context, machine prepared).
- A runner disconnect does not cancel a STARTED run any more
  (`controller_consumer._cleanup_connection`; workers wait in
  `controller_reconnect_services`). A self-update is refused while a run is
  STARTED (`runner_update_offer.py`).
- Per-step results today live in `TestRunTestCase.execution_checkpoint`
  (`agents/types.OrchestratorCheckpoint`: `plan` of `SubTask`, `attempts` of
  `SubTaskAttempt` with `performed_by="person"`, `attempt_id`, `duration_seconds`).
  Everything downstream reads that checkpoint: the case page Steps card
  (`projects/case_step_progress.build_case_step_progress`), the TestRail step
  list (`testrail_results_services.pivot_step_breakdown` →
  `testrail_mapping.format_step_breakdown`), and the screenshot evidence choice
  (`testrail_evidence.select_evidence`, keyed on `attempt_id` / `step_num` of
  `TestRunScreenshot`).
- Verbatim steps: `projects/case_step_refs.numbered_case_steps` splits
  "Test Case (Steps)" cases into `(num, text, expected)` and returns `()` for
  free-text cases (`template="Test Case"`, one `steps` and one `expected` blob).
- Screenshots: `TestRunScreenshot` (`image` under `MEDIA_ROOT/screenshots/trtc_<id>/`,
  `tool_name`, `attempt_id`, `step_num`, `testrail_evidence`), written by
  `services._persist_screenshot` and broadcast to the case page.
- TestRail: whole-suite Push (`testrail_results_services`, no attachments),
  "Send checked" and the early case send (`testrail_checked_push_services`,
  `testrail_case_send_services`; check → `freeze_testrail_evidence` → send with
  attachments). Comments come from `testrail_mapping.build_result_comment` /
  `build_checked_result_comment` with `pivot_result_text` + `pivot_step_breakdown`.
  Passed cases attach no screenshots today.
- Test Runner: `controller_client/protocol.py` (CLIENT_VERSION 0.12.0,
  `ClientCapability`, `_PERSON_WINDOW_CAPABILITIES`, `MAX_MESSAGE_SIZE` 10 MiB,
  update archive 7 MiB as the precedent for one big message). The Tk window
  runs in its own process (`person_window.py` manager, `person_window_app.py`,
  JSON-lines IPC `person_window_ipc.py`). `client._dispatch` hides it before
  agent actions (`hidden_for_action`). The consumer pushes `person_step_state`
  on nudges (`controller_person_steps_changed`, coalesced, push lock), accepts
  person messages only from the owner connection, and throttles them
  (`runner_person_step_services.hit_runner_throttle`). The actor is
  `projects/person_step_actor.PersonStepActor` (member resolved from
  `person_email`, or anonymous).
- Hooks every acting path must call: `test_sessions.services.record_case_activity`
  (`CaseActivity` choices) and `projects.case_credit.credit_cases`.
- LLM pattern for a tool-free call: `agents/services/person_step_advisor.py`
  (`send_chat_completion`, fenced inputs, capped output, `AIServiceError`).
- A web console for doing the steps was rejected before
  (`docs/plan/feat-tester-console.md`, Superseded), because only the runner can
  take screenshots of the test machine.

## Goals

- `RunHelpMode.MANUAL` (`"manual"`), chosen like the other modes (superuser,
  project default, Start dialog).
- The tester works only on the Test Runner window: picks a case, sees the
  verbatim steps, marks each step Pass or Fail with a screenshot (and a note
  for Fail), and submits a case verdict Passed, Failed or Blocked (note
  rules in Decisions D3/D4). The window is hidden for every screenshot.
- The tester can Ask AI about the current manual step (D12), reusing the
  existing Ask AI service.
- No agent, orchestrator, confirm sub-agent, machine setup, cleanup or teardown,
  and no Celery execution chain.
- Submitting is instant. Screenshots go into a local spool and are uploaded in
  the background afterwards. Uploads resume after a disconnect or a runner
  restart and are idempotent.
- The case page, run page, Steps card, Needs re-test, Reset, Re-run, test
  sessions, the leaderboard and all three TestRail push paths keep working for
  manual cases.
- At push time an AI rewrite makes the wording of the tester's notes clean and
  consistent across cases. Statuses, step texts, numbers and quoted strings
  stay exactly as they were. If the AI fails, the tester's own text is sent.

## Non-goals

- No web way to perform a manual case (D5). The web only shows
  results and offers admin actions (release a stuck claim, Stop run, Re-run,
  Reset, Needs re-test).
- No Need help, Night auto-fix, Pause and discuss, help chat, person steps
  or failure notifications in manual runs. Ask AI is kept (D12) but not
  redesigned here: its context improvements belong to
  `docs/plan/fix-ask-ai-context.md`.
- No AI rewriting of the steps themselves, and no AI verdict.
- No "Skip / not tested" verdict: an untested case is released (D6).
- No early (pre-finish) TestRail send for manual Blocked cases:
  `CASE_SEND_STATUSES` stays Failed / Needs re-test, as for every mode.
  Blocked goes out with Send checked or Push.
- No changes to how screenshots are stored generally. They stay under MEDIA
  like every other `TestRunScreenshot`.

## Design

### Data model

Manual results reuse the checkpoint and screenshot rows, so downstream readers
need almost no changes. One new model holds the hand-in, upload and AI
rewrite state.

- `RunHelpMode.MANUAL = "manual", "Manual"`.
- `FailedBy.MANUAL = "manual", "Manual verdict"`. The case's `result` holds the
  tester's case note verbatim. If the note is empty, it holds a generated line
  ("Passed: 6 of 6 steps passed", "Failed at step 3 of 6").
- `BlockedReason.MANUAL_VERDICT = "manual_verdict", "Marked blocked by the
  tester"` (fits `max_length=32`). This is a person's Blocked, next to
  `HELP_SKIPPED` ("Marked blocked by a person", help resolution) and
  `PERSON_REVIEW` (applied result change). Neither existing reason fits:
  `HELP_SKIPPED` means "a help request was skipped" and drives help history
  and leaderboard help counts (`CREDITED_HELP_RESOLUTIONS`). A manual Blocked
  has `result` = the tester's required note, so `pivot_result_text` gives
  "Marked blocked by the tester: <note>" through the existing
  `format_blocked_result_text`.
- Blocked invariant: `grep "status = TestRunTestCaseStatus.BLOCKED"` must
  still have exactly one hit (`case_help_services.py`). The submit service
  assigns the status from the validated verdict variable (a
  `verdict → status` map), the same way `person_mark_verdict` and
  `case_result_change_services` do. Infrastructure faults still never block:
  in manual mode only the tester's explicit verdict can.
- **Checkpoint (reused, no version bump).** On claim, the server writes an
  `OrchestratorCheckpoint` with these fields:
  - `plan`: the verbatim steps, as `SubTask(description=step text,
    expected_result=expected, timeout_seconds=0, case_steps=(n,))`.
  - `attempts=()`, `next_step=1`, the default `browser`, `case_step_count`
    and `case_steps_testrail`.
  - A new optional key `manual: true` (a `manual: bool = False` field,
    written only when True).

  On submit, the server adds one attempt per step the tester marked:
  `kind="step"`, `performed_by="person"`, `attempt_id = "m" + <shot group
  key>`, `result.status` pass/fail, `result.summary` = the tester's note
  (empty for a Pass with no note) and `duration_seconds` from the window's
  per-step timer (capped). Unmarked steps get no attempt, so the Steps card
  shows them as not run. The `manual` flag lets push and evidence code tell
  manual cases apart without an extra query. The run's `help_mode` alone is
  not enough: project scope `all` can rewrite finished runs.
- **Steps from the case**, in a new `projects/manual_case_steps.py` (pure, no
  queries):
  - A steps-template case uses `numbered_case_steps`.
  - A free-text case becomes one step: description = `steps` text,
    expected = `expected` text (D1).
  - A case with no steps becomes one step built from the title (plus
    preconditions shown above it).
  - Capped at 100 steps, with each text capped like
    `PERSON_STEP_CASE_STEP_TEXT_MAX_LENGTH`.
- **`TestRunScreenshot`** gets one new field, `manual_key` (CharField 64,
  blank). It holds the runner-minted shot id and has a partial unique
  constraint on `(test_run_test_case, manual_key)` where the key is not empty:
  this is the upload idempotency key. Manual shots use `tool_name`
  `manual_pass` / `manual_fail` / `manual_case`, plus `step_num` and the
  attempt's `attempt_id`.
- **New `ManualCaseSubmission`** (`projects/models.py`), one row per hand-in:
  - `test_run_test_case` FK; `submission_id` (UUID, unique, minted by the
    runner); `claim_token` (the claim it closes); `execution_generation`.
  - Who submitted: `submitted_by` (nullable FK), `submitted_label`,
    `submitted_via`, `submitted_at`.
  - `verdict` (`success` / `failed` / `blocked`) and `verdict_note`.
  - Upload state: `shots_expected`, `shots_received`, `upload_state`
    (`uploading` / `complete` / `superseded` / `incomplete`), and
    `last_shot_at`.
  - AI rewrite: `refine_state` (`pending` / `done` / `failed` / `skipped`),
    `refined_summary`, `refined_step_notes` (JSON `{num: text}`),
    `refined_input_hash` and `refined_at`.
  - An index on `(test_run_test_case, -submitted_at)`. The newest
    non-superseded row is the case's current hand-in.
- **Claim fields on `TestRunTestCase`**: `manual_claim_token` (CharField 32,
  blank), `manual_claimed_by` (nullable FK), `manual_claimed_label` and
  `manual_claimed_at`, cleared by a submit, a release, Reset or Re-run. The
  invariant "at most one claimed case per run" is enforced under the run lock
  in the service. A partial unique constraint is not possible because the
  constraint would need `test_run`, which is not a column on a filtered row
  set alone. Plain fields are used because a separate claim table would add a
  join to every snapshot.
- **Ask AI anchor (D12)**: `PersonStepQuestion.person_step` becomes
  nullable, and the model gains `manual_case` (nullable FK to
  `TestRunTestCase`, `related_name="manual_questions"`) and
  `manual_step_num` (nullable small int; null = the case as a whole). A check
  constraint requires exactly one of `person_step` / `manual_case`, and a
  partial unique constraint allows one `pending` question per
  `(manual_case)` (mirroring `person_step_one_open` for questions). If
  `fix-ask-ai-context.md` changes the anchor first, follow its shape instead;
  this plan only needs "a question about manual case X, step N".
- Migrations: `projects.00xx_manual_run_mode` (choices including
  `BlockedReason`, fields, new table, constraints, the `PersonStepQuestion`
  changes; additive) and `test_sessions.00xx_manual_activity` (choices).

### Services (new modules; one use case each)

- `projects/manual_case_steps.py`: verbatim step extraction (above).
- `projects/manual_run_policy.py` (leaf): `is_manual_run(run)`,
  `is_manual_checkpoint(json)`, and the user-safe refusal strings shared by
  Resume, help, person-step and mode-switch paths.
- `projects/manual_case_claim_services.py`:
  - `claim_manual_case(pivot_id, actor, expected_run_id)`: lock project → run
    → case. Refuses unless all of these hold: the run is STARTED and manual,
    the case is CREATED, no other case of the run is claimed, and the actor is
    allowed (owner connection, enforced by the caller). It sets IN_PROGRESS,
    `started_at` (first pick-up), `segment_started_at`, a new claim token and
    the step checkpoint, then records `CaseActivity.START` and broadcasts. No
    credit yet.
  - `release_manual_case(pivot_id, actor | web user)`: back to CREATED and
    clears the claim and checkpoint. The window can release (D6), and so can a
    writable member on the web for a stuck claim.
- `projects/manual_case_submit_services.py`, `submit_manual_case(form,
  actor)`:
  1. Idempotent: if the `submission_id` already exists, return the stored
     outcome and its missing shot keys.
  2. Lock and validate: the claim token matches, the case is IN_PROGRESS in
     this generation, the step numbers fit the stored plan, there are no
     duplicate shot keys, the note and manifest are within their caps, a
     Fail note is present on every failed step, and the verdict note rules
     hold (D3/D4: required for Blocked, and for Failed with no failed step).
  3. Write the attempts into the checkpoint and set the case status from
     the verdict map: SUCCESS, FAILED (`failed_by=MANUAL`) or BLOCKED
     (`blocked_reason=MANUAL_VERDICT`). Clear the other field (`failed_by` /
     `blocked_reason`), set `result`, `finished_at`, close the segment into
     `active_duration`, clear the claim, and create the submission
     (`upload_state=uploading`, or `complete` when there are no shots).
     Leaderboard (Blocked = 1 point by current status) and test sessions
     treat a manual Blocked like any other person verdict, through
     `credited_user` and the session row.
  4. Call `credit_cases` for a member actor, and `record_case_activity` with
     the new `CaseActivity.MANUAL_RESULT`.
  5. Finish the run if no case is CREATED or IN_PROGRESS (same conditional
     `finish_test_run` with the run's generation).
  6. Broadcast after commit, nudge the runner, and return the missing shot
     keys.

  It must not import `projects.services`' worker internals. The run-finish
  helper is exposed or moved to a small module if a cycle appears.
- `projects/manual_screenshot_services.py`, `store_manual_screenshot(project_id,
  submission_id, key, sha256, data)`:
  1. Check that the submission belongs to the project, is `uploading`, and
     lists the key in its manifest. A superseded submission is refused with
     "drop".
  2. Check the bytes: size ≤ `MANUAL_SHOT_MAX_BYTES`, sha256 matches,
     Pillow `verify()` then `load()`, format PNG or JPEG, pixels ≤ a cap
     (decompression-bomb guard).
  3. Get or create the `TestRunScreenshot` on `(pivot, manual_key)`.
  4. Bump `shots_received` with a conditional update, and set `complete` once
     it equals `shots_expected`.

  The file is written before the row (the same way `_persist_screenshot`
  does it). A duplicate key answers ok without writing.
- `projects/runner_manual_state_services.py`, `get_runner_manual_state(project_id)`.
  Builds the window snapshot in a constant number of queries (about 3):
  - The project's STARTED or recently finished manual run.
  - Its cases (id, title, TestRail id, status, claimed label), capped at 300
    in run order.
  - The claimed case's verbatim steps and preconditions, read from the
    checkpoint plan.
  - Submissions of this project still `uploading`, with their missing keys,
    so a reconnected runner can resume.
- `projects/manual_refinement_services.py`:
  - `refine_manual_result(pivot)`: build the input (verdict, case title,
    verbatim steps with each step's outcome and the tester's note, case note)
    and hash it. Reuse the stored rewrite when the hash matches. Otherwise
    call the AI, validate, and store.
  - `manual_comment_parts(pivot)` returns the text the push uses: the rewrite
    when it is `done` and the hash matches, otherwise the tester's verbatim
    text.
- `agents/services/manual_result_refiner.py` + `manual_result_refiner_prompts.py`
  (no ORM):
  - One tool-free `send_chat_completion` call. The input is fenced as data and
    the output is strict JSON `{summary, step_notes: {num: text}}`.
  - The prompt carries a fixed style guide so every case reads alike:
    neutral past tense, "Expected … / Actual …" for failures, no invented
    facts, error messages, IDs, numbers, paths and quoted text kept
    character for character, no new steps, no status words that contradict
    the given outcome.
  - Validation in code: the same step numbers as the input notes, non-empty
    where the input was non-empty, and length ≤ max(3 × input, 400). A
    failure raises `AIServiceError`.
  - Consistency is mostly structural: code assembles the comment layout; the
    AI only rewrites sentences.
- Changes to existing modules (kept small):
  - `start_test_run`: for a manual run, after commit only broadcast and nudge
    the runner. No `queue_test_run_cases`.
  - `get_run_readiness(test_run=)`: for a manual run, drop the visual-engine
    and machine-prepared checks and add "Test Runner supports manual testing"
    (`MANUAL_RUN_V1`, window enabled).
  - `run_help_mode_services.change_run_help_mode`: refuse switching to or from
    `manual` unless the run is WAITING.
  - `project_run_mode_services`: `manual` applies to new runs and drafts only.
    For `open` / `all` it skips runs whose mode would cross the manual
    boundary and reports the count.
  - Resume, help, person-step, case pause and result-chat redesign services
    refuse manual cases (`manual_run_policy`).
  - `rerun_cases` / Re-run in place and `reset_test_run`: clear the claim,
    mark that case's submissions `superseded` (later uploads are refused and
    the runner drops them) and keep the existing screenshot rows. The
    "freeze session rows first" rule stays.
  - `abort_test_run`: a claimed case → CANCELLED and its claim cleared.
    Submissions already made keep uploading.
- **Ask AI for manual steps (D12)**: `projects/person_step_question_services.py`
  gets a sibling entry point `ask_manual_step_question(pivot, project_id,
  actor, claim_token, step_num, question)`. It uses the same throttle (10/min
  per project, shared), the 20-per-target cap, the stale-pending rule, the
  `assist` queue task and the advisor. The gate differs: the run is STARTED
  and manual, the case is IN_PROGRESS, and the claim token matches. The
  `PersonStepAdviceContext` is built from the case text and the current
  verbatim step (text + expected result, "Step N of M"), with no hand-off or
  how-to. The answer reaches the window in the next `manual_run_state`
  snapshot (`active.questions`), and the case page by the existing
  `person_step_questions` socket event. Advisory only: it never changes the
  step, the case or the run, and it is not session activity. **Depends on
  `docs/plan/fix-ask-ai-context.md`**, which adds the knowledge base and the
  machine readiness artifact profile to the advisor context and makes it
  answer about the current case and step. This plan does not change the
  advisor or its prompts. Build this seam after that fix lands, or build it
  against the current context and pick up the fix automatically.
- `projects/exceptions.py`: `ManualRunError` (user-safe message).

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_start` (existing) | POST | `@project_membership_required`, writable; mode only for a superuser | existing start form, new choice `manual` | flash | readiness refusals incl. "update the Test Runner" |
| `projects:test_run_case_manual_release` `…/cases/<pivot>/manual/release/` | POST | `@project_membership_required` (read-only 403) | `ManualReleaseForm` (`next`) | redirect to case page | `ManualRunError` → flash |
| Run page (existing) | GET | membership | — | `manual_panel`: "Manual run: performed at the Test Runner", claimed case and who has it, per-case upload badge "Uploading screenshots 3/12" / "Screenshots incomplete" | — |
| Case page (existing) | GET | membership | — | `manual_result`: verdict, tester label, per-step outcome and notes (the Steps card already renders them from the checkpoint), screenshots grouped by step (existing gallery + `step_num`), upload progress, "Prepared for TestRail" block showing the AI-rewritten text next to the original once refined; Release button for a claimed case | — |
| Project Settings › Run mode (existing) | POST | superuser + writable | existing, new choice | skip count in the flash | — |

All new text is shown escaped. Runner-sent notes are never passed through
`|safe`. The rewritten text is shown escaped too.

### Background work and WebSockets

**Controller protocol 0.13.0.** New capability `MANUAL_RUN_V1`, advertised
only with the window (added to `_PERSON_WINDOW_CAPABILITIES`). Every message
is parsed and capped in `protocol.py`.

- server → runner `manual_run_state`: `{run: {id, name} | null, cases: [...],
  active: {pivot_id, claim_token, title, preconditions, steps: [{num, text,
  expected}], claimed_at, questions: [...], can_ask} | null,
  pending_uploads: [{submission_id, missing: [key]}]}`. `questions` uses the
  existing question payload shape, with asker labels blanked as today. Pushed on the same nudge path as `person_step_state`
  (`controller_person_steps_changed`, coalesced, deduplicated), only to
  `MANUAL_RUN_V1` runners.
- runner → server `manual_case_action` `{action: claim|release, pivot_id,
  claim_token?}` → `manual_case_action_result {ok, message}`.
- runner → server `manual_case_submit` `{submission_id, pivot_id,
  claim_token, steps: [{num, outcome: pass|fail, note, shots: [key],
  seconds}], verdict, verdict_note, shots: [{key, step_num |
  null, kind: pass|fail|case, sha256, size}]}`, with `verdict:
  passed|failed|blocked`. At most 64 KiB plus 1 KiB per step → `manual_case_submit_result {ok, message, missing: [key]}`.
- runner → server `manual_screenshot_upload` `{submission_id, key, sha256,
  mime, data_b64}` → `manual_screenshot_ack {submission_id, key, ok, drop,
  message}`. Raw image ≤ `MANUAL_SHOT_MAX_BYTES` = 7 MiB, so the base64
  message fits the 10 MiB `MAX_MESSAGE_SIZE`, below Daphne's 32 MiB cap.
- Ask AI: the existing `person_step_question` message gains optional
  `pivot_id` + `claim_token` + `step_num` (manual form; the step form keeps
  `person_step_id`). The answer goes back as `person_step_question_result`.
  It is accepted only from `MANUAL_RUN_V1` runners that also advertise
  `PERSON_STEP_ASSIST_V1`.
- Consumer (`projects/controller_consumer.py`): four handlers, plus the
  manual branch of the question handler.
  - Each checks the owner connection, the raw-length cap, the throttle
    (submit and claim 20/min/project shared with person answers, uploads
    `MANUAL_SHOT_RATE_PER_MINUTE` default 120/project), a `forms.py` form,
    then calls the service through `sync_to_async`.
  - Uploads are handled one at a time: the runner keeps one in flight. A
    separate small upload task on the runner never blocks agent replies, and
    a manual run has no agent traffic anyway.
- **Why WebSocket and not HTTP:** the runner has only `websockets` (plus
  `python-socks` for `CONTROLLER_PROXY`). An HTTP upload endpoint would need
  a second client, a second proxy path and Connection-key auth on a Django
  view (a new credential surface that would need its own throttle). The
  socket already gives auth, owner checks, proxying and the size limits.
- **Runner side** (`controller_client/`):
  - `manual_spool.py`: the spool lives in `controller_client/manual_spool/`,
    which is git-ignored and `.dockerignore`d. Each case's shots are written
    as `<submission_id>/<key>.png`, and a `manifest.json` is written
    atomically on Submit. Files are deleted after each ack, the directory
    after the last one, and an ack with `drop` deletes the whole submission.
    Size cap `CONTROLLER_MANUAL_SPOOL_MAX_MB` (default 2048): the window
    warns and refuses new shots at the cap.
  - `manual_uploader.py`: a background asyncio task. On every connect it
    first re-sends any `manifest.json` whose submission the server does not
    list yet, then the shots the server lists as missing. Uploads are
    sequential, with backoff on errors.
  - Submit works offline: it is queued in the spool and sent on reconnect.
    Claim needs a connection.
  - `client.py`: `update_runner` is refused while the spool is not empty
    ("finish uploading screenshots first"), because self-update deletes the
    old folder.
  - `manual_capture.py`, the capture sequence for every tester screenshot
    (Pass, Fail, Retake, case screenshot), D9:
    1. The window sends a `capture_request` over IPC (`{seq, purpose, step}`)
       and disables its capture buttons until the reply comes.
    2. The runner process enters `PersonWindow.hidden_for_action()`, the same
       hold `_dispatch` uses for agent actions. `ensure_hidden` sends
       `HideCommand`, waits for the ack (≤ `_HIDE_ACK_TIMEOUT_SECONDS`, 1.5 s)
       and then the compositor settle (`_HIDE_SETTLE_SECONDS`, 0.15 s).
       Manual capture adds `MANUAL_CAPTURE_EXTRA_SETTLE_SECONDS` (0.25 s),
       because the window was just clicked and the hide animation may lag on
       macOS and Windows.
    3. Only after a confirmed hide: `screen_capture.capture_screen()`
       (run in a thread) and encode as PNG. If the PNG is over 7 MiB,
       re-encode as JPEG q85, and downscale if it is still too large. Then
       write it to the spool.
    4. Leave the hold. The window is shown again at once for a manual
       capture (a new `show_after_capture` path; it does not use the
       5 s / 30 s agent re-show timers). The window is then told
       `capture_result {seq, ok, key, thumbnail}`.
    5. If the hide fails, the capture is aborted. That covers a timeout (the
       manager kills the window today), an IPC error, a window that is not
       ready, or `ensure_hidden` raising. In that case nothing is captured,
       nothing is spooled, and the step is not marked. The window (restarted
       by the supervisor if it was killed) shows "Could not hide the window,
       screenshot not taken. Try again." To make this possible,
       `ensure_hidden` must report the outcome: a new
       `hide_confirmed() -> bool` or exception, used only by this path. The
       agent `_dispatch` path keeps its current behaviour (an agent action
       still runs after a killed window, which is already off screen).
    6. Captures are serialized with one lock. Each IPC request is handled
       once, so a double click yields one shot. While a server-driven agent
       action holds the window, a manual capture waits (nesting allowed by
       the hold counter).
- **Window** (`person_window_app.py`, `person_window.py`, IPC): a "Manual run"
  view, shown while the snapshot has a manual run:
  - A case list with status and a Start button. The next CREATED case in run
    order is preselected.
  - The active case: title, preconditions, and each verbatim step with its
    expected result. Each step has **Pass** (screenshot) and **Fail**
    (screenshot + required note), a thumbnail with Retake, and the step
    status.
  - Previous/next step navigation with the current step highlighted.
  - The case section: **Passed / Failed / Blocked**, a note (required for
    Blocked, and for Failed with no failed step), an optional case
    screenshot, and Submit. Submitting Passed while a step is failed, or
    Failed with every step passed, asks for confirmation (D3).
  - "Ask the AI about this step": the same collapsible box as for person
    steps, for the highlighted step. It never hides or raises the window
    and keeps a separate pending slot (D12).
  - "Release case".
  - An upload status line ("Uploading 4 screenshots…", "Offline: 2 cases
    waiting to upload").
  - After Submit the window moves to the next case at once.
- Celery: one new task `projects.tasks.refine_manual_case_result(pivot_id)`
  on `upload`, queued when a submission becomes `complete`. It fills the
  rewrite early, so the case page shows it before anyone checks the case. No
  execution task runs in manual mode.

### TestRail push

- `testrail_results_services.pivot_result_text` / `pivot_step_breakdown`
  branch on `manual_run_policy.is_manual_checkpoint`. For a manual case:
  - Header: "Passed manually by <tester label> in run “X”."
  - The rewritten summary (or the verbatim note).
  - "Steps (performed manually):" with "n. Passed/Failed/Not run:
    <verbatim step text> — <rewritten note>".

  The pass/fail outcome always comes from the checkpoint, never from the AI.
  The case status maps through the existing `map_pivot_status_to_testrail`:
  Passed 1, Failed 5, Blocked 2 (`TESTRAIL_STATUS_BLOCKED`), Needs re-test 4.
  A manual Blocked comment leads with "Marked blocked by the tester: <note,
  rewritten>". The status and the reason label are never rewritten.
  This covers all three paths (suite Push, Send checked, early case send),
  because they share these two functions.
- The push tasks (on `upload`) call `refine_manual_result` before building the
  comment. A hash match uses the stored rewrite. Otherwise the AI is called
  inline with `MANUAL_REFINE_TIMEOUT_SECONDS`. On failure the verbatim text is
  sent and logged; the push never fails because of the AI. The comment hash
  changes when a rewrite lands, so the existing changed-comment logic
  re-sends once (acceptable).
- Evidence: `freeze_testrail_evidence` / `select_evidence` add a manual
  branch. A manual case (Passed, Failed or Blocked) attaches all of its
  manual shots in step order, capped at `MANUAL_TESTRAIL_MAX_SHOTS` (default
  30, D7).
  `check_locked_pivots` refuses to check a manual case whose current
  submission is still `uploading`
  ("screenshots are still uploading from the Test Runner"). An `incomplete`
  submission can be checked (the comment says "n of m screenshots
  received").
- `incomplete` is set by a beat sweep on `notify`:
  `projects.tasks.sweep_stalled_manual_uploads`, every 15 minutes. It applies
  when there has been no shot for `MANUAL_UPLOAD_STALE_HOURS` (default 24).
  A later upload still completes the submission.

### Settings / infrastructure

New settings in `settings.py` via decouple, all added to `example.env`:
`MANUAL_SHOT_MAX_BYTES` (7 MiB, also a protocol constant; the setting may
only lower it), `MANUAL_SHOT_RATE_PER_MINUTE` (120),
`MANUAL_TESTRAIL_MAX_SHOTS` (30), `MANUAL_REFINE_TIMEOUT_SECONDS` (120),
`MANUAL_UPLOAD_STALE_HOURS` (24). Add a Celery route for the refine task, a
beat entry for the sweep, and the runner `.env` key
`CONTROLLER_MANUAL_SPOOL_MAX_MB` in the download `.env`. No new queue or
compose service.

### Data flow

1. A superuser sets the run (or the project default) to Manual. A member
   presses Start. Readiness requires a `MANUAL_RUN_V1` runner. The run goes
   STARTED with no chain, and the runner gets nudged.
2. The window receives `manual_run_state` and shows the case list. The tester
   presses Start on a case, which sends `manual_case_action claim`. The
   server checks it under lock, sets the case to In progress, and writes the
   verbatim plan to the checkpoint. The snapshot returns the steps.
3. For each step the tester presses Pass or Fail. The window hides (a
   confirmed hide and settle), the screen is captured and saved to the spool,
   and the window comes back. A failed hide takes no screenshot. A Fail asks
   for a note. The tester may Ask AI about the step. Nothing is sent yet.
4. The tester chooses Passed, Failed or Blocked for the case, adds a note
   (required for Blocked), and presses Submit. The window writes the manifest, sends `manual_case_submit`, and
   moves to the next case. The server writes the attempts, the verdict, the
   credit, the session row and the submission, maybe finishes the run, and
   answers with the missing keys.
5. The uploader sends `manual_screenshot_upload` one at a time. The server
   verifies and stores each shot and acks it. When the submission is
   `complete`, `refine_manual_case_result` is queued.
6. A member checks the case and sends it (early send for Failed, Send checked
   or Push at the end). The push uses the rewritten text and the frozen
   manual shots.

## Alternatives considered

- **Reuse `CasePersonStep` rows per step.** Rejected: those rows mean
  "handed out by the agent, confirmed by the agent". Their statuses, the
  worker re-queue, panels and runner snapshot logic would all need manual
  branches. The checkpoint already powers the Steps card, TestRail and
  evidence.
- **A new `ManualStepResult` table instead of checkpoint attempts.** Cleaner
  queries, but every reader (Steps card, step breakdown, evidence) would need
  a second source. It can be added later if reporting needs SQL over manual
  steps.
- **HTTP multipart upload endpoint.** Rejected for now (see Background work).
  Revisit if screenshots routinely exceed 7 MiB.
- **Chunked WebSocket upload.** Not needed at 7 MiB per shot. A single
  message keeps idempotency trivial.
- **AI rewrite at submit time only.** It would miss later edits and the "at
  push" wording of the request. The chosen approach pre-computes at
  completion and re-checks the hash at push.
- **Running manual cases through the Celery chain.** This would give a free
  "run finished" path, but a worker would sit idle for the whole time the
  person works, so it was rejected.

## Risks and mitigations

- **Image uploads from the runner** (malicious or huge files). Mitigation:
  owner connection only, a size cap before decoding, a sha256 check, Pillow
  verify plus a pixel cap, PNG/JPEG only, a server-chosen file name, and a
  per-project rate. Screenshots land under MEDIA, which nginx serves without
  auth. That is the existing exposure for all screenshots, recorded here and
  not widened.
- **Anyone with the Connection key can claim and submit** in any member's
  name (the accepted trust model of `person_steps_v1`). Results are labelled
  "via Test Runner", and only a resolved active member gets credit.
- **XSS**: runner notes are shown escaped everywhere. TestRail comments are
  plain text through the existing `_one_line` / indent helpers.
- **AI rewriting changes meaning.** Mitigation: statuses come from data, the
  prompt keeps literals, code validates the output, the original is shown on
  the case page next to the rewrite, and the human check before sending sees
  the rewrite. Accepted residual risk: a subtle rewording.
- **Lost screenshots** (runner wiped, spool deleted). Mitigation: the
  `incomplete` state after the stale window, shown on the run page and in the
  comment. Self-update is refused while the spool has files.
- **A Re-run or Reset races an upload.** The submission is `superseded` under
  the case lock, and the upload service re-checks the state with a
  conditional update.
- **Mode switches across the manual boundary** on live runs could leave a
  chain-less agent run or a chained manual run. They are refused outside
  drafts.
- **Compatibility**: older runners never get `manual_run_state`. A manual run
  cannot start without `MANUAL_RUN_V1`. If an older runner connects mid-run,
  the run page says "Update the Test Runner to continue this manual run".
  Self-update is already blocked while a run is STARTED, so the tester has to
  Stop the run, update, and Re-run the remaining cases (documented).
- **Window in a screenshot**: a capture runs only after a confirmed hide
  plus settle; a failed hide aborts the capture (D9). Residual risk: an OS
  that acks the hide before redrawing. The extra settle mitigates it, and the
  verification checks it on macOS, Windows and Linux.
- **Ask AI depends on another plan**: if `fix-ask-ai-context.md` is not done,
  manual Ask AI still works with the current advisor context (case + step +
  application context). There is no hard ordering, only weaker answers.
- **Query hotspots**: the snapshot is about 3 queries per push and the upload
  handler 2–3 per shot. The run page upload badges come from one annotated
  query over the submissions.

## Implementation steps

1. **Shared foundation** (orchestrator, before sub-agents):
   - `projects/models.py`: enums (`RunHelpMode.MANUAL`, `FailedBy.MANUAL`,
     `BlockedReason.MANUAL_VERDICT`), claim fields, `ManualCaseSubmission`,
     `TestRunScreenshot.manual_key` + constraint, the `PersonStepQuestion`
     anchor.
   - Migrations.
   - `test_sessions/case_activity.py`: `MANUAL_RESULT` + migration.
   - `agents/types.py` / `agents/checkpoint.py`: the `manual` flag.
   - `projects/manual_case_steps.py`, `projects/manual_run_policy.py`,
     `projects/exceptions.py`.
   - `controller_client/protocol.py`: capability, CLIENT_VERSION 0.13.0, the
     four message types, payload dataclasses, parsers, caps and
     `MANUAL_SHOT_MAX_BYTES`.
   - `auto_tester/settings.py`, `example.env`.
2. **Server services** (sub-agent A):
   - New: `manual_case_claim_services.py`, `manual_case_submit_services.py`,
     `manual_screenshot_services.py`, `runner_manual_state_services.py`.
   - `person_step_question_services.ask_manual_step_question` and the
     advice-context builder for a manual step (after or alongside
     `fix-ask-ai-context.md`).
   - Changes in `services.py` (start without a chain, readiness, abort,
     reset, rerun), `run_rerun_services.py`, `run_resume_services.py`,
     `run_help_mode_services.py`, `project_run_mode_services.py`, and the
     refusals in the help, person-step and pause services.
3. **Consumer wiring** (sub-agent B, after A's signatures are fixed):
   `projects/controller_consumer.py`, `projects/forms.py` (four runner forms
   and the manual question form), and the snapshot push for `MANUAL_RUN_V1`.
4. **Test Runner** (sub-agent C):
   - New: `controller_client/manual_spool.py`, `manual_uploader.py`,
     `manual_capture.py`.
   - Changes: `client.py` (handlers, update refusal, capture requests),
     `person_window.py` (`hide_confirmed`, `show_after_capture`, capture
     lock),
     `person_window_app.py`, `person_window_ipc.py`, `config.py`,
     `example.env`, `.gitignore`, `.dockerignore`, and `diagnose --window`
     (renders a fake manual state).
5. **TestRail rewrite** (sub-agent D):
   - New: `agents/services/manual_result_refiner.py`, its prompts,
     `projects/manual_refinement_services.py`.
   - Changes: the `projects/tasks.py` refine task and sweep,
     `testrail_results_services.py`, `testrail_mapping.py` (manual header),
     `testrail_evidence.py` / `testrail_evidence_services.py`,
     `run_case_check_services.py` (upload gate), and the settings routes and
     beat entry.
6. **Web UI** (sub-agent E):
   - New: `projects/manual_case_views.py` (Release), a URL, and
     `templates/projects/includes/_manual_case_card.html`.
   - Changes: run page and case page context and templates, the start dialog
     and settings mode copy, badges in `static/css/punk-hazard.css`.
7. **Integration** (orchestrator): imports, URL and route registration,
   `pre-commit run --all-files` for the server and controller configs,
   `manage.py check`, `makemigrations --check`.

## Verification

Manual checks (the project has no test suite):

1. `pre-commit run --all-files` (strict mypy for the server and
   `controller_client/` including win32), `python manage.py check`,
   `makemigrations --check`, and `sqlmigrate` for both migrations.
2. As a superuser, set a draft to Manual. Start is refused with an old runner
   and allowed with a 0.13.0 runner. The run is Running and no
   `execute_test_run_case` task is queued (Flower).
3. Window flow on a steps-template case and on a free-text case:
   - Claim shows the verbatim steps. Pass, Fail, Retake and the case
     screenshot all take screenshots with the window hidden: open each
     stored image and confirm the window is not in it, on macOS, Windows and
     Linux. Fail requires a note.
   - Force a hide failure (stub the window to never ack). No screenshot is
     taken, the step stays unmarked, and the window comes back with the
     error.
   - Submit a Blocked case. A note is required, the case is Blocked with
     reason "Marked blocked by the tester", it gets leaderboard credit and a
     session row, `grep "status = TestRunTestCaseStatus.BLOCKED"` still has
     one hit, and Send checked posts TestRail status 2.
   - Ask AI on a manual step from the window. The answer arrives in the
     window and on the case page, and the throttle and pending rule apply.
   - Submit moves on at once. The case page shows the verdict, the step notes
     and the shots grouped by step, with upload progress going to complete.
4. Kill the network mid-upload, then restart the runner. The uploads resume,
   no duplicate rows appear (`manual_key`), and the spool directory is
   emptied.
5. Submit the same `submission_id` twice (replay) and get one result. Send an
   upload with a wrong sha256, a non-image file or a file over 7 MiB, and see
   each refused. Send from a superseded connection and see it refused. Hit the
   throttle.
6. Re-run a submitted case while its uploads are pending. Later shots are
   dropped and the runner deletes them.
7. Check a case while it is uploading and see the refusal. After completion,
   the rewrite shows on the case page, and an early Failed send and Send
   checked post the manual comment with all shots. Status is unchanged. With
   `OPENAI_BASE_URL` pointed at a dead host, the verbatim text is sent.
8. Mode switch on a Running run to or from Manual is refused. Project scope
   `all` skips crossing runs.
9. Test session and leaderboard: the claim adds a session row, the submit
   adds credit for a member and none for an anonymous runner.
10. Run `reviewer` and `verifier` in parallel on the combined diff.

## Decisions (open questions resolved 2026-11-01)

- D1 (Q1). Free-text cases are one step holding the whole steps text and
  expected result. Only "Test Case (Steps)" cases are split, using the
  existing parser.
- D2 (Q2). A failed step does not stop the case. The tester may continue or
  submit at any time, and unmarked steps are reported as "Not run".
- D3 (Q3). The case verdict may contradict the step results after a
  confirmation in the window. A Failed verdict needs a note when no step
  failed.
- D4 (Q4, changed by the user). Case verdicts are Passed, Failed and
  **Blocked**, all from the Test Runner window. A Blocked note is required.
  Blocked is stored as BLOCKED with `blocked_reason=MANUAL_VERDICT` ("Marked
  blocked by the tester"), assigned from the verdict variable (the Blocked
  invariant holds). It goes to TestRail as status 2, through Send checked or
  Push (not the early Failed send), and counts on the leaderboard and in
  test sessions like other person verdicts. A case can still be released
  untested; there is no Skip verdict.
- D5 (Q5). Doing manual cases is runner-only. The web views results and can
  release a stuck claim.
- D6 (Q6). Any order, one claimed case per run, next case in run order
  preselected. Release is allowed.
- D7 (Q7). All manual screenshots go to TestRail (Passed, Failed and
  Blocked), capped at 30 per case.
- D8 (Q8). One mandatory screenshot per marked step, with Retake, plus an
  optional case screenshot with the verdict.
- D9 (added by the user). Every tester screenshot runs hide → wait for ack →
  settle (0.15 s + 0.25 s) → capture → show. A failed hide aborts the capture
  and never captures the window. The mechanism is the existing
  `hidden_for_action` / `ensure_hidden` in `controller_client/person_window.py`,
  extended to report a confirmed hide.
- D10 (Q9). The existing Check step, which shows the rewritten text next to
  the original, is enough review. The AI touches only the tester's notes and
  case note, never the TestRail step texts, statuses or the Blocked reason
  label.
- D11 (Q10, Q11). Mode switches to or from Manual are allowed on drafts only,
  and project scope "all" skips crossing runs. Manual runs skip the visual
  engine and machine-prepared readiness checks.
- D12 (added by the user). Manual steps support Ask AI by reusing the person
  step question service, the advisor and the `assist` task. The context is
  the case and the current verbatim step. This depends on
  `docs/plan/fix-ask-ai-context.md` (knowledge base, machine readiness
  artifact profile, case-and-step focus). Ask AI is not redesigned here.

## Open questions

None open.

## Changelog

- 2026-11-01: created (Draft).
- 2026-11-01: user answers folded in; Status Ready. All defaults accepted
  (D1–D3, D5–D8, D10–D11). Changes: Blocked case verdict from the window
  with a required note, a new `BlockedReason.MANUAL_VERDICT` and TestRail
  status 2 (D4). Explicit hide → settle → capture → show sequence where a
  failed hide aborts the capture (D9). Ask AI on manual steps reusing the
  existing service, with a nullable `PersonStepQuestion` anchor, depending on
  `fix-ask-ai-context.md` (D12).
- 2026-11-01: implemented (foundation, then five parallel parts, integration); review and verification rounds 1–3 addressed. Accepted deviations: the window snapshot shows only a STARTED manual run (nothing to claim in a finished one; pending uploads always listed); TestRail attachments keep upload order (resumable counter), selection under the cap is by priority and filenames carry the step number; no credit at Start for manual runs (credit at hand-in for a resolved member); Re-run deletes the re-run cases' screenshots as before; a submit-parse refusal drops the hand-in (documented CLIENT_VERSION rule). Added beyond the plan: per-shot `drop_shot` and a shared pixel cap. Done.
