# feat: AI-reviewed screenshots for TestRail results

Status: Done
Created: 2026-11-03
Request: "Refactor screen shot selection: When sending results to testrails,
deciding screenshot should be wiser, like if an app crashed on flow and the
screen shot is empty, and not relevant, don't send it, it will make confusion."
Today the screenshots attached to a TestRail result are chosen only by position.
For an agent case these are the last few shots of the step attempt that caused
the verdict; for a Manual case, every hand-in shot. When the application crashed
during that attempt, the shots show a black or white frame, the bare desktop, or
another window, and a TestRail reader takes them for the failure. From now on an
AI review decides which screenshots are attached. It sees the candidate
screenshots together with the case's log, title, verdict and failure summary;
for Manual cases the "log" is the tester's step marks and notes. If none is
relevant, nothing is attached. A case whose review is missing, pending, stale or
failed is never sent to TestRail. Typed `feat` (not `refactor`) because what is
sent changes.

## Context

- **Selection rules** (pure, no ORM): `projects/testrail_evidence.py`.
  - `select_evidence` (agent cases): Failed = last `TESTRAIL_EVIDENCE_SHOTS_PER_STEP`
    (3) shots of each failing final step attempt, at most `TESTRAIL_EVIDENCE_MAX_SHOTS`
    (10). Blocked = the attempt the case stopped on. Fallback = the case's last 3
    shots. Passed = none.
  - `select_manual_evidence` (Manual run cases): every shot of the current
    hand-in (Passed included), capped by `MANUAL_TESTRAIL_MAX_SHOTS` (30) by
    priority.
  - Overlays (`controller_omniparser`) are excluded.
  - Background: `docs/plan/feat-testrail-relevant-screenshots.md` (Done). It
    picks the *causing attempt* deterministically, and this plan keeps that.
- **Freeze**: `projects/testrail_evidence_services.freeze_testrail_evidence`.
  - Called from `run_case_check_services.check_locked_pivots`, inside the
    locked transaction of `check_run_cases` and of
    `testrail_case_send_services.start_case_send` (the early send checks,
    takes the push slot and queues the push in one go).
  - Constant query count. It sets `TestRunScreenshot.testrail_evidence`.
- **Send checked / case send**: `projects/testrail_checked_push_services.push_checked_results`,
  run by the task `push_checked_results_to_testrail` (`upload`, soft limit
  3600 s).
  - It first calls `refresh_manual_refinements` inline (900 s budget, up to 4
    concurrent AI calls in a `ThreadPoolExecutor`, ORM only in the calling
    thread).
  - Then `plan_checked_push` prefetches flagged shots. `_attach_screenshots`
    uploads them *after* the result is added and stores
    `testrail_source_attachment_count` as a resume index into the ordered list.
  - Readiness is SQL: `_READY_Q`, `_RESUME_Q`, `_SKIPPED_Q`
    (`testrail_source_skip_reason` parks a case until it is unchecked), the
    panel `get_checked_push_panel` and `_has_pending`.
- **Whole-run "Push to TestRail"** (018, `projects/testrail_results_services.py`):
  pushes every case with a verdict, checked or not. It attaches **no
  screenshots**.
- **Comment**: `testrail_mapping.build_checked_result_comment` ("N screenshots
  attached from step 3."). Manual comment parts come from
  `manual_refinement_services.ManualCommentParts`, which includes
  `format_manual_shots_note`.
- **Log**: `TestRunTestCase.logs` is a text blob. Each step attempt starts with
  `[Orchestrator] Sub-task N/M: ...` (`agents/services/orchestrator.py`) and
  ends with `Result: PASS|FAIL — ...`. `runner_case_services` reads a bounded
  tail with `Right("logs", n)`.
- **Manual "logs"**: the `manual` checkpoint carries a person attempt per marked
  step (status + note), the case note is `TestRunTestCase.result`, and the
  verbatim steps come from `projects/manual_case_steps.py`. Each shot carries
  `step_num` and a kind tool name (`MANUAL_SHOT_TOOL_NAMES`: case/pass/fail).
- **LLM client**: `agents/services/llm_client.send_chat_completion` with
  `ImageContent(base64_data, media_type)` (several images per message, as in
  `action_verifier.py`), `llm_config.build_llm_config()`, `store: False`. The
  model to copy is `agents/services/manual_result_refiner.py` (pure module:
  input dataclass, versioned input hash, JSON answer, strict validation,
  `AIServiceError`).
- **Where "still uploading" is shown today**:
  - the run table's Reason column (`templates/projects/test_run_detail.html`,
    `manual_panel.uploads` badges `badge-uploading` /
    `badge-upload-incomplete`);
  - the manual case card (`templates/projects/includes/_manual_case_card.html`);
  - the check flash `MANUAL_UPLOADING_MESSAGE` (`run_case_check_services`).
- **Pillow** `~=11.2.1` is in `requirements.txt` (used by
  `manual_screenshot_services`).

## Goals

- The AI decides which screenshots are attached, for agent **and** Manual cases,
  from the candidates plus the bounded log (agent) or step marks and notes
  (Manual), the case title, verdict and failure summary.
- None relevant → attach nothing; the comment says so; every screenshot stays in
  Punk Hazard.
- **Gate**: a case is sent (new result added) only when its evidence review is
  `done` and still current. This covers Send checked and the early case send.
  A case that cannot be reviewed stays checked and unsent, is labelled "Waiting
  for screenshot review" or "Screenshot review failed", and can be retried.
- No LLM call or file read inside the check transaction; freeze stays
  query-only.
- Resume-index safety: flags change only while `testrail_source_result_id IS
  NULL`, and never after a push has planned the case.
- Cost bounded: candidates only (≤ `TESTRAIL_EVIDENCE_REVIEW_MAX_IMAGES` = 8
  per agent case, ≤ `MANUAL_TESTRAIL_MAX_SHOTS` per manual case), images
  downscaled, a review reused while its inputs are unchanged, no AI call for
  cases with no candidate shots.

## Non-goals

- Changing which attempts are "causing".
- The 018 whole-run Push (it attaches no screenshots; see Q8).
- Showing per-shot judgments on the case page gallery (Q5); Django admin only.
- Re-judging results already added to TestRail.
- Any controller, vision worker or WebSocket protocol change.
- A deterministic blank-image pre-filter. Dropped on the user's answer: the AI
  decides. It would also save little, since one call per case is made anyway
  and a blank image costs few tokens.

## Design

### Data model (migration `projects.0056_evidence_review`)

`TestRunScreenshot` gains:
- `relevance = CharField(max_length=16, choices=ScreenshotRelevance.choices,
  blank=True, default="")`, with `ScreenshotRelevance` = `relevant` /
  `not_relevant` (`""` = not judged). It is valid only while the case's
  `evidence_review_key` matches.
- `relevance_note = CharField(max_length=200, blank=True, default="")`: the
  model's one-line reason. Admin only, never sent, never `|safe`.

`TestRunTestCase` gains:
- `evidence_review_state = CharField(max_length=16,
  choices=EvidenceReviewState.choices, blank=True, default="")`.
  `EvidenceReviewState`:
  - `""`: never prepared (unchecked since deploy);
  - `pending`: waiting for the AI;
  - `done`: reviewed (also when there was nothing to review);
  - `failed`: the last attempt failed.
- `evidence_review_key = CharField(max_length=64, blank=True, default="")`:
  SHA-256 of the review inputs (prompt version, status, verdict text, candidate
  shot ids in order, and for Manual cases the tester's marks and notes; not the
  log, which cannot change while the case is checked).
- `evidence_review_error = CharField(max_length=200, blank=True, default="")`:
  a short, user-safe reason for `failed` ("The AI service did not respond in
  time."), shown on the case page.
- `testrail_evidence_omitted = PositiveSmallIntegerField(default=0)`: the number
  of candidates judged not relevant. It feeds the comment note.

All fields are additive with constant defaults, which is metadata-only on
Postgres 11+, including on the populated screenshot table.

Migration data step (RunPython, one UPDATE): cases that are **already checked
and partly sent** (`testrail_source_result_id IS NOT NULL`) get
`evidence_review_state=done`. Their attachment resume must still go through,
and the gate applies to new results only anyway. Checked, unsent cases stay
`""`, so the next send reviews them inline before sending (the gate). No data
loss; the reverse is a no-op.

### Pure rules (`projects/testrail_evidence.py`)

- **Candidates** (`review_candidates`), the shots the AI judges:
  - Agent Failed/Blocked: *all* shots of the causing attempts (same attempt
    rules as today), newest first, latest step first, capped at
    `TESTRAIL_EVIDENCE_REVIEW_MAX_IMAGES`.
  - Agent fallback (no causing attempt has shots): the case's newest shots,
    same cap.
  - Agent Passed or Needs re-test: none.
  - Manual: the shots `select_manual_evidence` picks today (current hand-in,
    ≤ `MANUAL_TESTRAIL_MAX_SHOTS`, Passed included; none for Needs re-test).
- **Key**: `evidence_review_key(...)`, pure `hashlib`, includes
  `EVIDENCE_REVIEW_PROMPT_VERSION` from the agents prompt module.
- **Selection after review**: `select_reviewed_evidence(candidates, judgments,
  per_step, max_shots) -> EvidenceSelection(ids, omitted)`.
  - Only `relevant` candidates are kept.
  - Agent: at most `per_step` per causing attempt, then `max_shots`, keeping
    the latest. Manual: `MANUAL_TESTRAIL_MAX_SHOTS` in the existing step
    order.
  - No walk-back to other attempts or steps.
  - Nothing relevant → `[]`. The old "last 3 of the case" fallback no longer
    applies to unjudged shots.

### Images (`auto_tester/image_downscale.py`, new, generic, no ORM)

- `downscale_for_model(data: bytes, *, max_side: int) -> bytes`: Pillow
  re-encode to JPEG (quality about 80, longest side ≤ 1280).
- Raises `ValueError` on undecodable data. The review then marks that shot
  `not_relevant` with the note "unreadable image".
- This is the only Pillow use kept. It bounds tokens and memory; it does not
  judge.

### Services

- **`projects/testrail_evidence_services.py`**: prepare at check. It replaces
  the freeze body, stays query-only, and still runs from `check_locked_pivots`.
  - Load the locked pivots' `status`, `result`, `execution_checkpoint`,
    `evidence_review_state` and `evidence_review_key`, plus the candidate
    shots' `relevance` (same constant query shape). The current manual
    hand-in keys come from `current_manual_shot_keys`.
  - For each pivot:
    - **no candidates**: state `done`, key of the empty set, no flags,
      omitted 0;
    - **state `done` and stored key == computed key**: re-select from the
      stored judgments and set flags (reuse; no AI);
    - **otherwise**: state `pending`, clear flags, omitted 0.
  - Writes: clear flags, set flags, one `bulk_update` of the four pivot fields.
  - `queue_evidence_review(pivot_ids)`: `transaction.on_commit` +
    `celery_app.send_task(REVIEW_TESTRAIL_EVIDENCE_TASK, ...)` by name, in
    chunks of 20 pending ids. Best effort: a lost task leaves the case
    `pending`, which the push or the Retry action picks up.
  - The module keeps the check service free of LLM imports.
  - The key covers status, verdict text, candidate ids and manual notes, all
    already loaded, so nothing large is read in the transaction. It does
    **not** cover the log. The log cannot change while the case is checked,
    because every path that writes the log reopens the case and clears
    Checked.
- **`projects/testrail_evidence_review_services.py`** (new use case "review the
  evidence of checked cases"). Entry points:
  - `review_checked_evidence(pivot_ids)`: called by the task.
  - `refresh_evidence_reviews(*, test_run, pivot_ids, budget_seconds) ->
    ReviewOutcome`: called inline by the push.
  - `retry_evidence_reviews(*, test_run) -> int`: called by the Retry view.
    Sets checked, unsent `failed`/`pending` cases to `pending` and queues them.
    Refused with `EvidenceReviewError` while a send is live.

  The shared core:
  1. Outside any transaction, load the checked, unsent
     (`testrail_source_result_id IS NULL`) cases in scope whose state is not
     `done`, or whose key no longer matches (for example a late manual upload
     on an `incomplete` hand-in). Load the fields above plus
     `test_case__title` and `Right("logs", 4 × EVIDENCE_REVIEW_LOG_MAX_CHARS)`
     (agent only). Load the screenshot rows in one query.
  2. Task path only: skip, before any AI call, when the run's push is live.
     The push reviews inline itself.
  3. Build each case's input:
     - title, verdict label and text (truncated);
     - for agents, each causing step's description and failure summary from
       the checkpoint, plus the **log excerpt**: from the last
       `[Orchestrator] Sub-task {step}/` marker of the causing step in the
       loaded tail to its end, head and tail kept within
       `EVIDENCE_REVIEW_LOG_MAX_CHARS` (code constant, 12 000), else the plain
       tail;
     - for Manual, the verbatim steps with the tester's mark and note, and the
       case note;
     - images as opaque labels `S1..Sn`, each with step number and tool or
       shot kind, read from storage and downscaled.
  4. AI calls run in a `ThreadPoolExecutor` (up to 4 workers, no ORM in the
     threads). Each call is bounded by `TESTRAIL_EVIDENCE_REVIEW_TIMEOUT_SECONDS`
     and the remaining budget.
  5. Write, per case, in one transaction:
     - lock the run row (push fields only), then the pivot (the same order as
       `uncheck_run_cases` and `start_case_send`);
     - re-verify: still checked, `testrail_source_result_id IS NULL`, same
       status, same key, not already `done` with that key, and (task path)
       no live push;
     - **success**: write `relevance` / `relevance_note` on the candidates,
       state `done`, key, clear the error, run `select_reviewed_evidence`,
       write flags and `testrail_evidence_omitted`;
     - **failure** (`AIServiceError`, timeout, unusable answer, budget spent):
       state `failed`, a user-safe `evidence_review_error`, flags cleared;
     - if any re-check fails, write nothing.
     Logs carry the case id only, never image data or log text.
- **`agents/services/evidence_review.py`** + **`evidence_review_prompts.py`**
  (new, pure, no ORM):
  - `EvidenceReviewInput` (title, verdict, failure context, `log_excerpt` or
    manual steps, images) and `review_evidence(llm, data, *, timeout_seconds)
    -> dict[str, EvidenceJudgment]` (label → `relevant: bool`, `reason`
    ≤ 200 chars). One `send_chat_completion` call with every image as
    `ImageContent(media_type="image/jpeg")`.
  - The prompt asks for at most `per_step` relevant shots per step.
    - Relevant = it shows the failure or its cause (an error message or
      dialog, a crash reporter, the application in a wrong state, an expected
      element missing in a visible application window, for Manual the state
      the tester's mark describes).
    - Not relevant = blank, black, white or loading frames with no
      information; desktop or taskbar only; the Test Runner or an unrelated
      terminal; a different application; near-duplicates of a shot already
      chosen.
    - A crash dialog is relevant. A Passed Manual shot is relevant when it
      shows the step's outcome.
  - Case text, log and notes are fenced as data. The answer is JSON only.
  - Validation: every label exactly once, booleans; otherwise `AIServiceError`.
  - `EVIDENCE_REVIEW_PROMPT_VERSION` constant.
- **`projects/testrail_checked_push_services.py`**:
  - `push_checked_results` first calls `refresh_evidence_reviews(test_run=,
    pivot_ids=<checked unsent in scope>,
    budget_seconds=CHECKED_PUSH_EVIDENCE_REVIEW_BUDGET_SECONDS)` (600 s module
    constant), then `refresh_manual_refinements`, then `plan_checked_push`.
    Both budgets together stay under the 3600 s soft limit.
  - **Gate in SQL**: new `_REVIEWED_Q = Q(evidence_review_state="done")` joins
    `_READY_Q`. The `plan_checked_push` scope adds `Q(testrail_source_result_id__isnull=False)
    | _REVIEWED_Q`, so resumes of results already added are never blocked.
  - New `_REVIEW_WAITING_Q` (checked, sourced, not skipped, unsent, state
    `""`/`pending`) and `_REVIEW_FAILED_Q` (same, state `failed`).
  - `CheckedPushPanel` gains `review_waiting_count` and `review_failed_count`,
    in the same aggregate (no extra query). `_has_pending` and the panel's
    `pending` treat waiting and failed cases as sendable, because Send
    reviews them inline first.
  - `_disabled_reason` and `confirm_body` mention them ("3 cases are waiting
    for screenshot review; they are reviewed before sending").
  - `CheckedPushOutcome` gains `review_failed`. The summary appends "2 not sent:
    screenshot review failed (retry the review)". When nothing was delivered
    and the only reason is failed reviews, the send is stamped FAILED with that
    summary.
  - Cases left unreviewed get **no** `testrail_source_skip_reason`, so the next
    send or Retry tries again without an uncheck.
  - `_plan_pivot` passes `testrail_evidence_omitted` to the comment
    (`_PLAN_PIVOT_FIELDS`).
- **`projects/testrail_case_send_services.start_case_send`**: no new code path.
  Its check freezes the case `pending` (or `done`), and the push it dispatches
  reviews inline. A case that still fails review is reported by the push
  summary and labelled on the pages.
- **`projects/run_case_check_services.py`**: the docstring and the check flash
  gain "Screenshots are being reviewed before they can be sent." (shown when
  any checked case went `pending`). `CheckOutcome` gains `reviewing: int`
  (from the prepare step's return value).
- **`projects/testrail_mapping.py`**: `build_checked_result_comment(omitted_count=)`
  and the manual comment path:
  - attached > 0 and omitted > 0: "2 screenshots attached from step 4 (3 not
    relevant, not attached)."
  - attached = 0 and omitted > 0: "No screenshot attached: none of the N
    screenshots showed relevant evidence. Every screenshot is kept with the
    case in Punk Hazard."
  - omitted = 0: unchanged.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_cases_check` (existing) | POST | `@project_membership_required` (writable) | `RunCaseSelectionForm` (unchanged) | redirect; flash adds "N cases waiting for screenshot review" | unchanged |
| `projects:testrail_send_checked` (existing) | POST | same | none | panel: `review_waiting_count`, `review_failed_count`, updated confirm body and disabled reason | unchanged |
| `projects:test_run_cases_send_testrail` (existing) | POST | same | unchanged | unchanged; the outcome message adds "screenshots are reviewed before sending" when a case went pending | unchanged |
| `projects:testrail_evidence_review_retry` (new) `<project>/test-runs/<run>/testrail/review-screenshots/` | POST | `@project_membership_required` (writable), `@require_POST` | none | redirect to the run; flash "Reviewing screenshots of N cases…" | `EvidenceReviewError` (send live, nothing to retry) → error flash |

Where the review state is shown, next to the "uploading" badges:
- **Run table Reason column** (`test_run_detail.html`): for checked, unsent
  cases, "Reviewing screenshots" (`badge-reviewing`) for `""`/`pending` and
  "Screenshot review failed" (`badge-review-failed`) for `failed`. The field is
  added to the run table queryset's `.only()` (no extra query).
- **Checked push panel** (`includes/_testrail_checked_push_actions.html`): the
  counts, plus a "Retry screenshot review (N)" POST button when
  `review_failed_count` or a stale `pending` exists. The button uses the
  existing `partials/confirm_dialog.html` convention.
- **Case page** (`test_run_case_detail.html`, Checked/TestRail area and the
  manual case card): the same badge, plus `evidence_review_error` for `failed`
  (autoescaped).
- New badge classes go in `static/css/punk-hazard.css`.

### Background work and WebSockets

- New task `projects.tasks.review_testrail_evidence(pivot_ids: list[int])`:
  - queue `upload` (beside `refine_manual_case_result`);
  - `max_retries=0`;
  - soft limit = `TESTRAIL_EVIDENCE_REVIEW_TIMEOUT_SECONDS × ceil(20 / 4) + 60`,
    hard limit soft + 30;
  - on a soft time limit, cases not yet written stay `pending` (Send or Retry
    picks them up).
- The push task gains the inline pass; its name, queue and limits are
  unchanged.
- No new socket event. After the task writes, it calls the existing
  run-panel broadcast (`_broadcast_checked_push` or its equivalent nudge), so
  the badges and counts refresh the way they do for push progress. If that
  helper is push-only, a page reload shows the new state (Q9).
- No controller or vision worker change; old Test Runners are unaffected.

### Settings / infrastructure

`auto_tester/settings.py` (validated) + `example.env`:
- `TESTRAIL_EVIDENCE_REVIEW_MAX_IMAGES` (int, default 8, ≥ 1): candidates per
  agent case.
- `TESTRAIL_EVIDENCE_REVIEW_TIMEOUT_SECONDS` (int, default 90, ≥ 1): per AI
  call.
- **No on/off setting.** `TESTRAIL_EVIDENCE_LLM_REVIEW` from the first draft is
  removed.
  - Reason: "a not reviewed test should not go to TestRail". A switch that
    sends unreviewed cases would break that rule. A switch that blocks every
    send would only duplicate an AI outage.
  - With one code path, an AI outage shows as "Screenshot review failed" with
    Retry, and nothing unreviewed leaves.
  - Passed agent and Needs re-test cases have no candidates, so they are
    reviewed at once without the AI and are never blocked by an outage.

Code constants: `EVIDENCE_REVIEW_LOG_MAX_CHARS = 12_000`, image max side
1280, `CHECKED_PUSH_EVIDENCE_REVIEW_BUDGET_SECONDS = 600`, task chunk 20, pool
4.

Deploy: `migrate`, then restart web/Daphne and the `upload` workers. Execution
workers are untouched.

### Data flow

1. A case gets a verdict. Nothing evidence-related happens.
2. A person checks it (or uses the early case send). Prepare, query-only:
   - no candidates → `done`;
   - a matching stored review → `done` with flags;
   - else → `pending`, no flags, and the task is queued on commit.
3. The task, outside any transaction, builds the inputs (shots + log excerpt
   or manual marks), calls the AI, and under run + pivot locks writes
   judgments, `done`, flags and omitted. On failure it writes `failed`. It
   skips if a send is live, or the case changed, was unchecked or was sent.
4. The run page shows the "Reviewing screenshots" / "Screenshot review failed"
   badges and counts. Retry re-queues failed or stale pending cases.
5. Send (checked or case send): the push reviews every in-scope case that is
   not `done` (or whose key changed) inline, within 600 s. It then plans only
   `done` cases for new results (resumes always allowed) and attaches the
   flagged shots. The comment states attached and not-relevant counts. Cases
   still not `done` stay checked and unsent, appear in the summary, and keep
   their badge.

## Alternatives considered

- **Review when the verdict is written**: 6+ status writers, reviews for cases
  never sent, and the push safety net is still needed. Rejected; check is the
  gate every send passes through.
- **Refuse to check until reviewed**, like the uploading gate. Checking is the
  trigger, and the early case send checks and sends in one click. Gating the
  *send* keeps both flows working. Rejected.
- **Park unreviewable cases with `testrail_source_skip_reason`**: that reason
  only clears on uncheck, so a transient AI fault would need a manual uncheck
  and re-check. A separate review state plus Retry is clearer. Rejected.
- **An on/off switch**: see Settings. Rejected for the rule's sake.
- **A deterministic blank pre-filter**: dropped by the user's answer. The AI
  sees the images anyway.
- **One AI call per screenshot**: loses cross-shot context (near-duplicates,
  "last good frame") and costs more round trips. Rejected; one call per case.
- **Send the whole log**: unbounded tokens. The causing step's segment, bounded
  head and tail, is what explains the screenshots.

## Risks and mitigations

- **Sends blocked by an AI outage** (by design). The badges and the push
  summary say so; Retry and the next Send try again; cases without candidates
  are unaffected.
- **Resume index**: judgments and flags are written only for
  `testrail_source_result_id IS NULL` cases, under the run lock with a
  live-push check (task, Retry) or inside the push before it plans. Partly
  sent cases are `done` by the migration and are never re-reviewed.
- **Already checked, unsent cases at deploy** get reviewed on the next Send
  (inline). Their attachments may shrink. This is intended: they were never
  sent.
- **Real failure judged not relevant**: the prompt counts crash dialogs and
  wrong application states as relevant; the comment says shots were left out;
  every shot stays in Punk Hazard.
- **Prompt injection** through screenshot text, log or tester notes: the output
  is booleans plus a short admin-only note, so the worst case is a wrong
  keep/drop. Inputs are fenced as data.
- **Cost**: ≤ 8 (agent) or ≤ 30 (manual) downscaled JPEGs per case plus ≤ 12 000
  log characters, one call per case. The key reuses reviews across re-checks.
  The queued task and the inline pass duplicate a call only when a send
  starts while the task runs.
- **Manual cases now depend on the AI** (the user chose this). All their shots
  are judged, including on Passed cases.
- **Data leaving the server**: the same endpoint and `store: False` as every
  agent screenshot today; log excerpts were already sent to the same model
  during execution.
- **Populated tables**: additive columns, one bounded UPDATE for partly sent
  cases.
- **N+1**: prepare keeps its constant queries; panel counts are in the existing
  aggregate; the run table adds one column to `.only()`; the review loads two
  queries per chunk, with file reads bounded by the caps.

## Implementation steps

1. Model, migration, admin (files: `projects/models.py` (`ScreenshotRelevance`,
   `EvidenceReviewState`, fields), `projects/migrations/0056_evidence_review.py`
   (data step for partly sent cases), `projects/admin.py` (read-only fields),
   `projects/exceptions.py` (`EvidenceReviewError`)).
2. AI judge (files: new `agents/services/evidence_review.py`,
   `agents/services/evidence_review_prompts.py`). Independent of 1.
3. Image utility (files: new `auto_tester/image_downscale.py`). Independent.
4. Pure rules (files: `projects/testrail_evidence.py`: candidates, key,
   `select_reviewed_evidence`, `EvidenceSelection`). Needs the prompt version
   from 2.
5. Prepare at check + queue (files: `projects/testrail_evidence_services.py`,
   `projects/run_case_check_services.py` (`CheckOutcome.reviewing`, flash
   text)). Needs 1 and 4.
6. Review use case, task, settings (files: new
   `projects/testrail_evidence_review_services.py`, `projects/tasks.py`,
   `auto_tester/settings.py`, `example.env`). Needs 1 to 5.
7. Push gate, panel, comment (files: `projects/testrail_checked_push_services.py`,
   `projects/testrail_mapping.py`, `projects/manual_refinement_services.py`
   only if the manual comment parts need the omitted count). Needs 6.
8. Pages (files: `projects/testrail_views.py` (retry view), `projects/urls.py`,
   `projects/views.py` / the run and case page context builders (state in
   `.only()`, counts), `templates/projects/test_run_detail.html`,
   `templates/projects/includes/_testrail_checked_push_actions.html`,
   `templates/projects/test_run_case_detail.html`,
   `templates/projects/includes/_manual_case_card.html`,
   `static/css/punk-hazard.css`). Needs 7.
9. Docs (files: `CLAUDE.md` one line under Key Patterns and the Manual run
   paragraph (screenshots are now AI-selected), `docs/changed/2026-11-xx-testrail-evidence-relevance.md`).

Steps 2 and 3 can go to parallel sub-agents after step 1. Steps 5 to 7 are line
by line dependent: one implementer. Step 8 can be a separate sub-agent once the
panel fields and URL name are fixed.

## Verification

- `pre-commit run --all-files` (isort, black, strict mypy), `python manage.py check`,
  `python manage.py makemigrations --check`.
- Migration on a dev DB copy: partly sent checked cases are `done`; other
  checked, unsent cases are `""` and show "Reviewing screenshots"; the next
  Send reviews them inline.
- Agent case whose app is killed during the failing step: check it. It shows
  "Reviewing screenshots", then flags only relevant shots (desktop-only shots
  `not_relevant` in admin). The comment has the not-relevant note. With every
  shot irrelevant: no attachment and the "No screenshot attached" note.
- Normal Failed case (a wrong value on screen): the relevant shots are
  attached; no note. A crash dialog is kept.
- Passed agent case: `done` at once, no AI call (check logs), sent with no
  screenshots.
- Manual case (Passed and Failed): all hand-in shots are judged with the
  tester's marks; an incomplete hand-in that gets a late shot after check is
  re-reviewed by the next Send (key change).
- AI unreachable (`OPENAI_BASE_URL` to a dead host): check → `failed` badge with
  error. Send → the case is not sent, the summary says so, the case stays
  checked. Restore the AI and press Retry or Send → sent.
- Early case send from the run page and from `/failures/`: the task skips (push
  live), the push reviews inline, then sends; on AI failure the case is not
  sent and is labelled.
- Stop the `upload` worker after check (state stays `pending`), start it and
  press Send: reviewed inline, then sent.
- Kill a push mid-attachments and send again: no duplicates; the partly sent
  case is not re-reviewed and its flags are unchanged.
- Uncheck/check with nothing changed: no new AI call.
- Read-only member: Retry answers 403.
- Query counts: check 1 vs 100 cases gives the same count; the panel and run
  table query counts are unchanged.
- Run the `reviewer` and `verifier` agents.

## Open questions

Resolved by the user (2026-11-03):
- Q1 Detection: the AI decides from the candidate screenshots plus the log
  (agent: bounded excerpt of the causing step; manual: the tester's marks and
  notes), title, verdict and failure summary. No blank-check layer.
- Q2 Fallback: nothing. None relevant → attach none and say so in the comment.
- Q3 Manual runs: yes, all hand-in shots go through the same review.
- Q4 Comment note: yes.
- Q5 Per-shot judgment in the gallery: not in this change.
- Q6 Gate: an unreviewed case never goes to TestRail (Send checked, case send).
  Inline review in the push, otherwise it stays checked and unsent with a badge
  and Retry. `TESTRAIL_EVIDENCE_LLM_REVIEW` removed (see Settings).
- Q7 Cost limits: the defaults (8 images, 90 s per call, 600 s inline budget).

New, not blocking (assumptions used):
- Q8 **The whole-run 018 "Push to TestRail"** attaches no screenshots. Should
  it also refuse cases without a done review? (Assumption: no. There is no
  screenshot to judge, it pushes unchecked cases too, and gating it would
  spend AI calls for nothing. If "Push" in the answer meant this button, the
  gate would apply the same `done` rule to its plan and run the review inline
  there.)
- Q9 **Live refresh of the badges** when the background review finishes.
  (Assumption: reuse the run page's existing checked-push broadcast if it can
  be sent without a live push; otherwise the state shows on the next reload.)

## Changelog

- 2026-11-03: created (Draft).
- 2026-11-03: user answers applied, Status Ready.
  - The AI decides from screenshots + log (manual: marks and notes).
  - The Pillow blank check and the `blank` field are dropped; Pillow is kept
    only for downscaling.
  - No walk-back: none relevant → none attached.
  - Manual cases are reviewed.
  - Send gate on `evidence_review_state=done` with inline review in the push,
    badges where uploads are shown, and a Retry action.
  - `TESTRAIL_EVIDENCE_LLM_REVIEW` removed.
  - New Q8 (018 push) and Q9 (live badge refresh).
- 2026-11-03: review round 1 fixed: unsourced cases are `done` at check and
  never reviewed; a corrupt image only marks that shot not relevant; the inline
  push review runs in chunks of 20; Retry takes `failed`/`""` only (button shown
  for failed reviews); the push rewrites manual notes first, then reviews, then
  recomputes the not-reviewed set right before planning; manual AI input sends
  the tester's case note as `case_note`; `testrail_evidence` imports the prompt
  version from the prompts module; the early case send flash mentions the review.
  Rejected: moving the task off `upload` (`assist` serves latency-sensitive Ask
  AI answers; reviews are short and bounded), and the live badge broadcast (Q9:
  the run page only listens for push status; badges refresh on reload).
- 2026-11-03: review round 2: a transient storage `OSError` fails the review
  (retryable) instead of dropping the shot; the review and the push recheck
  load cases in chunks of 20; the task checks for a live send before each
  chunk. Review and verification clean; Status Done.
