# feat: Result chat (discuss a finished case's result with the agent; it proposes a result change or a redesigned re-run, a person applies)

Status: Done
Created: 2026-10-26
Request (verbatim): "Feature: Need ability to discuss the last state (result) of a test case with agent and ask it to change it if needed"

Restated: on a run case that has a result (**Passed**, **Failed** or **Blocked**), a project member can open a multi-turn **result chat** with the agent about that result. The agent starts with the case and its latest result:

- the verdict text;
- the tester's plan, each step's final status, attempt summaries, decisions and failed-attempt digests;
- the help history, earlier attempts and earlier result changes;
- the case log (paged on demand);
- the case's stored screenshots (it can ask the vision model about one);
- full access to the test machine through helpers (`run_setup_task`), as in the help chat.

The person can challenge the result, and the agent can propose one of two things. Neither changes anything until a person clicks.

1. **A result change**: a new status (any of Passed / Failed / Blocked) with a new result summary and an explanation. The person clicks **Apply**. The earlier result is kept as history.
2. **A redesigned test plan**: the help-chat whole-plan redesign, where leading steps may be carried from steps that passed. The person clicks **Re-run with redesigned plan**. The run reopens, and the case runs again from the first step that is not carried, after the usual cleanup and a restore of the carried steps' state.

Type: `feat`.

## Context

- **Result fields** (`projects/models.py` `TestRunTestCase`):
  - `status`, `result` (free text: the agent's verdict summary, or the server's text for Mark failed / Mark blocked), `blocked_reason`, `finished_at`;
  - Checked/Sent fields (`checked_at`, `checked_by`, `testrail_source_result_id`, `_sent_at`, `_attachment_count`, `_skip_reason`);
  - 018 push tracking (`testrail_pushed_status_id`, `testrail_pushed_comment_hash`);
  - `execution_checkpoint`.
- **Verdict writers today**:
  - `projects/services._finalize_pivot` (agent; pass/fail comes from the steps' final attempts in `agents/services/orchestrator._build_verdict`, and the LLM only words `result`);
  - `case_help_services.mark_failed_after_help` and `mark_blocked_after_help`. The latter is the only writer of BLOCKED, and `docs/changed/2026-10-26-no-automatic-blocked.md` asks that `grep "status = TestRunTestCaseStatus.BLOCKED"` keeps exactly one hit.
- **Counts and run status** are computed, never stored (`get_test_run_summary`, `_annotate_run_counts`). `_update_test_run_status_if_needed` only looks at non-terminal statuses. A terminal → terminal change therefore needs no run write.
- **TestRail**:
  - 018 push (`testrail_results_services._is_changed`) re-plans a case whose status id or comment hash changed;
  - Send checked sends at most once per execution (`testrail_source_result_id`), and Re-run and Resume clear Checked/Sent;
  - `pivot_result_text` sends `result`.
- **Reopen machinery** (`projects/run_reopen_services.py`, shared by Resume and Re-run in place):
  - `lock_project_and_run`;
  - `list_reopen_blockers`: other run Started, Send checked or 018 push live, machine not ready / visual engine offline, setup turn live;
  - `reopen_run(test_run, pivots, help_mode=)`: run → Started, generation + 1, queue and broadcast on commit;
  - `REOPENABLE_RUN_STATUSES` = Done, Cancelled.
- **Resume** (`projects/run_resume_services._resume_pivots`): keeps log, screenshots and checkpoint; snapshots a `TestRunCaseResume`; `_reset_execution_fields` sets status CREATED, clears result, finish time and Checked/Sent; then `reopen_run`.
- **How a resumed case restarts** (`projects/services.execute_test_run_test_case`, `orchestrator.run_orchestrator`):
  - without `continue_pending` it runs `_safe_cleanup`;
  - the orchestrator runs the restore sub-task before the cursor step when `1 < next_step <= len(plan)`;
  - a failed restore raises `ResumeStateLostError`, which becomes a Need help fault with continue mode `restore`.
- **Plan redesign** (`agents/plan_revision.py`, `projects/case_plan_revision_services.py`, `CasePlanRevision`, `docs/changed/2026-10-24-help-chat.md`). It is coupled to a help request in these places:
  - `CasePlanRevision.help_request` is a non-null FK, with the constraint `plan_revision_one_pending` on `help_request`;
  - `PlanRevisionRecord.help_request_id: int`;
  - `apply_plan_revision(..., help_request_id)` keeps the checkpoint's `help` key, and sets `paused_step = next_step` and `paused_digest` from the cursor step;
  - `orchestrator_prompts.redesign_for_help` recognises a redesign by `revisions[-1].help_request_id == help.request_id`, and `redesign_verdict_note` / `format_redesign_state_line` say "during help" and "from the current screen";
  - `case_step_progress.EarlierPlan.help_request_id: int` / `help_step_num`, and the Steps card label "help at step N";
  - every function in `case_plan_revision_services` goes through `lock_waiting_help_request`.

  The validator (`validate_plan_revision`: carried steps are a strictly increasing prefix of steps whose final attempt passed, each carried once, at least one step runs) and `carriable_steps` / `snapshot_plan` / `build_plan_draft_view` / `plan_fingerprint` do not depend on a help request.
- **Help chat thread** (`machine_setup`):
  - `SetupConversation(kind=case_help, help_request=…)`, opened by `services/open_help_chat.py`;
  - role `ThreadRole.CASE_HELP` (`thread_role.py`) picks the prompt (`setup_prompt._build_help_chat_prompt`) and the tools (`setup_tools`, `help_chat_tools.py`, which include `_parse_steps` and `_STEP_ITEM_SCHEMA`);
  - `run_turn.py` chooses the role;
  - the thread list and header come from `chat_page._help_threads` / `_thread_from_row`.
- **Proposal-card pattern** (`docs/changed/2026-10-26-main-chat-docs-and-runs.md`): one pending row per thread, a NOTICE `SetupMessage` row with an FK renders the card through `message_payload` upserts (no new socket event), and POST views route through a `machine_setup` decision service.
- **Send gates** (`machine_setup/services/send_message._refusal`): a live turn, a STARTED run, or a machine not READY. A PAUSED run is allowed. One live turn per project is a DB constraint. A Night auto-fix falls back to a person when a turn is live.
- **Vision Q&A**: `agents/services/vision_qa.answer_screenshot_question` exists.
- **Import rules**:
  - `projects.*` never imports `machine_setup.services`;
  - `projects.case_checkpoint_services` must not import `agents.services`;
  - modules that `projects.services` imports cannot import `case_plan_revision_services`.
- Versions: Django ~=5.2.11, channels ~=4.3.2. No new library behaviour is relied on: partial `UniqueConstraint`, `select_for_update`, `FieldFile.open()` and `AlterField(null=True)` (Postgres `DROP NOT NULL`, metadata only) are already in use.

## Goals

- "Discuss result" on the case page of a Passed / Failed / Blocked case opens (or reopens) the case's result chat, a Machine setup thread of a new kind with a new role. It reuses the thread, turn, transcript, sub-task, Stop and limits machinery.
- In that thread the agent has:
  - the project-data tools and `run_setup_task` (full machine);
  - `get_case_result`, `read_case_log` and `ask_case_screenshot`;
  - `propose_result_change` and `propose_plan_change` (the redesign).
- **Result change**: a card shows from → to, summary, explanation and effects. **Apply** writes the status and result, records the change with the replaced values, and clears Checked/Sent. It is refused when the case changed since the proposal.
- **Redesign re-run**: a card shows the side-by-side draft (current plan with statuses → new plan with carried / runs). **Re-run with redesigned plan** opens a confirm dialog with the help-mode select. It then goes through the reopen gates, applies the redesign, records an Earlier attempts entry, resets the case like Resume (Checked/Sent cleared, log and screenshots kept), and reopens the run. The case runs cleanup → restore of carried steps 1..C → step C+1 (no restore when C = 0).
- Plan revisions are decoupled from help requests: a revision has a `source` (`help` / `review`), and the checkpoint record's `help_request_id` is optional.
- The case page shows a "Result changed by a person" line, a **Result changes** history, an Earlier attempts entry "Re-run with a redesigned plan", and the review redesign under Earlier plans.

## Non-goals

- A person editing the result or the plan by hand without the agent (Q7).
- Changing step-level results in the checkpoint on a result change. The Steps card shows what was executed.
- Cancelled, Need help, Created or In progress cases. Need help has the help chat.
- Applying a redesign without re-running, or re-running other cases with it.
- Notifications on a person's change.
- Any controller protocol, Celery task or settings change.

## Design

### Data model

**`projects/models.py`**, migration `projects/0032_case_result_chat`:

- `BlockedReason.PERSON_REVIEW = "person_review", "Marked blocked by a person after review"` (choices only).
- `CaseRestartKind.REDESIGN = "redesign", "Re-run with a redesigned plan"` (choices only; `TestRunCaseResume.kind` max_length 10 is too short, so the migration changes it to 16).
- `PlanRevisionSource(TextChoices)`: `HELP = "help"`, `REVIEW = "review"`.
- **`CasePlanRevision`** changes:
  - `help_request` becomes `null=True, blank=True`, an `AlterField` on a populated table that is metadata only on Postgres;
  - new field `source` (default `help`; existing rows are help);
  - new constraint `UniqueConstraint(fields=["test_run_test_case"], condition=Q(status="proposed", source="review"), name="plan_revision_one_pending_review")`;
  - `CheckConstraint`: `source='help'` ⇔ `help_request IS NOT NULL` (`plan_revision_source_matches_request`).
  - Help code keeps filtering by `help_request`, so it never sees review rows.
- New model **`CaseResultChange`**:
  - `test_run_test_case` FK (CASCADE, `related_name="result_changes"`);
  - `base_fingerprint` (CharField 64): sha256 of `(status, blocked_reason, finished_at ISO, result)`;
  - `from_status`, `from_blocked_reason`, `from_result` (snapshot; rewritten at apply with the values actually replaced);
  - `to_status`;
  - `summary` (≤ 4000) and `explanation` (≤ 2000), both model-written;
  - `status` (`ResultChangeStatus`: PROPOSED / APPLIED / DISCARDED / SUPERSEDED), `proposed_at`, `decided_by` (SET_NULL), `decided_at`;
  - `conversation_id` (plain int, null);
  - `UniqueConstraint(fields=["test_run_test_case"], condition=Q(status="proposed"), name="result_change_one_pending")` and index `(test_run_test_case, -proposed_at)`.
  - APPLIED rows are the audit trail.
- Constants `RESULT_CHANGE_SUMMARY_MAX_LENGTH = 4000`, `RESULT_CHANGE_EXPLANATION_MAX_LENGTH = 2000`.

**`machine_setup/models.py`**, migration `machine_setup/0006_case_result_thread`:

- `SetupConversationKind.CASE_RESULT = "case_result", "Result chat"`.
- `SetupConversation.result_case` FK → `projects.TestRunTestCase` (SET_NULL, null, `related_name="result_conversations"`), plus `UniqueConstraint(fields=["result_case"], condition=Q(result_case__isnull=False, archived_at__isnull=True), name="setup_one_result_thread_per_case")`. One thread per case is reused across executions (Q10).
- `SetupMessage.result_change` FK → `projects.CaseResultChange` and `SetupMessage.plan_revision` FK → `projects.CasePlanRevision` (both SET_NULL, `related_name="+"`), set on the NOTICE row that shows each card.

**Checkpoint** (`agents/types.py`, `agents/checkpoint.py`; the version stays 1 and every key is optional):

- `PlanRevisionRecord.help_request_id: int | None` and a new `source: Literal["help", "review"] = "help"` (key `source`; a missing key means `help`).
- **Rollback**: the previous parser requires an int `help_request_id`, so it drops a review record as malformed. The checkpoint stays usable; only that history line is lost.

### Plan revision decoupling (`agents/plan_revision.py`, pure)

- `apply_plan_revision(checkpoint, validated, *, revision_id, help_request_id: int | None, reason, source="help")`. For `source="review"`:
  - `help` is set to `None`: the earlier help belongs to the replaced plan's step numbers and stays in the `CaseHelpRequest` rows and the record;
  - `phase = "step"`;
  - `paused_step` = the **last step whose final attempt failed** (0 when none), and `paused_digest` = that attempt's digest, instead of the cursor (a finished case's cursor can be past the last step);
  - everything else is unchanged: carried attempts, `next_step = C+1`, `decisions = ()`, version + 1, record appended.
- New `review_carriable_note(checkpoint) -> str`: the carriable list for the brief (reuses `carriable_steps`).
- The validator, `plan_fingerprint`, `build_plan_draft_view` and the JSON helpers are unchanged. `snapshot_plan` shows "needs help" only with phase `awaiting_help`, so a finished case shows passed / failed / not run.

**Orchestrator wording** (`agents/services/orchestrator.py`, `orchestrator_prompts.py`):

- `redesign_for_help` matches only `source == "help"` records.
- New `redesign_for_review(revisions)`: the latest record when its `source == "review"`.
- `_rebuild_state`: when `redesign_for_review` matches and the attempts hold carried steps, the carried lines are followed by "The test plan was redesigned after a person reviewed the earlier result: <reason>. Steps 1–C were carried over and their state was restored; continue at step C+1." With C = 0 it says "No step was carried over; the case runs again from step 1." Step C+1 gets the record's `paused_digest` as its retry note, labelled "earlier plan, step N", only when one exists. A later help request in the same execution behaves as today, because its own help key anchors on the new step numbers.
- `verdict_redesign` / `redesign_verdict_note`: for a review record, "The test plan was redesigned after a person reviewed the earlier result (reason). Steps 1–C were carried over from the earlier plan, where they had passed; their state was restored, and they were not re-run."
- `format_attempt_lines` is unchanged ("PASSED under the earlier plan as step k").
- `case_teardown_prompts`, `list_resume_steps`, `final_step_indices` and `truncate_checkpoint` need no change.

**Why cleanup and restore run.** The case finished. Its own teardown/cleanup ran, and later cases or the result chat's helpers changed the machine. Unlike Continue after help, nothing guarantees the screen is where the case left it. Reusing the Resume path gives:

- the pre-case cleanup;
- the restore sub-task for C ≥ 1, which re-creates the carried steps' state;
- `ResumeStateLostError` → Need help with continue mode `restore` when the restore fails.

None of this is new code. The prompt tells the agent that it does **not** need to prepare the machine for the re-run.

### Services

**projects** (flat use-case modules):

- New **`projects/case_result_change_services.py`**:
  - `RESULT_CHANGEABLE_STATUSES = (SUCCESS, FAILED, BLOCKED)`, `is_result_changeable(pivot)` (no query), `result_fingerprint(pivot)` (pure);
  - `propose_result_change(*, pivot_id, to_status, summary, explanation, conversation_id) -> ResultChangeView`. It locks project → run → pivot, requires a changeable status and `to_status` in the changeable statuses and ≠ current, caps the texts, and supersedes the pending row. It raises `ResultChangeError`.
  - `apply_result_change(*, pivot, change_id, user) -> None`:
    - locks project → run → pivot → change;
    - checks that the row is PROPOSED and this pivot's, the fingerprint matches ("The case changed since this was proposed: it was re-run, resumed or its result changed. Ask the agent again."), the status is still changeable, and no Send checked or 018 push is live (the `run_reopen_services` messages);
    - writes `status = change.to_status` (assigned from a variable), `blocked_reason` (`PERSON_REVIEW` for Blocked, else ""), and `result = format_changed_result(...)`;
    - clears Checked/Sent through the shared helper below (Q4), and keeps `finished_at` and the checkpoint;
    - marks the row APPLIED and broadcasts the pivot status on commit (existing public broadcast helpers).
  - `discard_result_change(...)`, `get_result_change_view(change, pivot) -> ResultChangeView` (pure, with `outdated` and an `effects` line), `list_result_changes(pivot)` (APPLIED, one query).
- New pure **`projects/case_result_text.py`**: `format_changed_result(...)` → "Result changed from Failed to Passed by <name> on <date> after review with the agent.\n\n<summary>" (Q8).
- New **`projects/case_review_redesign_services.py`**:
  - `propose_review_plan_revision(*, pivot_id, steps, reason, conversation_id) -> PlanDraftView`:
    - locks project → run → pivot and requires a changeable status;
    - parses the checkpoint ("There is no test plan to redesign; suggest a plain Re-run." when there is none);
    - runs `validate_plan_revision` (bounds and max steps as in the help path);
    - supersedes the pending review row and stores `CasePlanRevision(source=review, help_request=None, base_fingerprint=plan_fingerprint(...), previous_steps, steps, resume_step)`;
    - raises `PlanRevisionError`.
  - `discard_review_plan_revision(...)`, `get_review_draft_view(revision, pivot) -> ReviewDraftView` (the `PlanDraftView` plus `outdated` and `confirm_body`: server-built text from counts only, e.g. "Reopens the run. Cleans up the machine, restores the state of steps 1–2, then runs steps 3–7 of the new plan. Checked and Sent marks on this case are cleared.").
  - `rerun_with_redesigned_plan(*, pivot, revision_id, user, help_mode: str | None) -> None`. In one transaction:
    - `lock_project_and_run`; the run is in `REOPENABLE_RUN_STATUSES` (else "Only a finished or cancelled run can re-run a case."); `list_reopen_blockers` is empty (else the first blocker);
    - locks the pivot (`defer("logs")`); the status is changeable; the revision is this pivot's PROPOSED review row;
    - the fingerprint equals `plan_fingerprint` of the current checkpoint; re-validates;
    - `apply_plan_revision(source="review", help_request_id=None)`, then clears any `continue_pending` with `clear_help_continue_pending` (redundant after `help=None`, but cheap);
    - snapshots `TestRunCaseResume(kind=REDESIGN, from_step=resume_step, previous_*)`;
    - `reset_case_for_restart(pivot, now)`, saves, marks the revision APPLIED (decided_by/at) and supersedes a pending `CaseResultChange`;
    - `reopen_run(test_run, [pivot], help_mode=help_mode)`.

    It raises `RunResumeError` (gate and run messages) or `PlanRevisionError` (draft messages). This is the same path as Resume, with no parallel queueing.
- `projects/run_reopen_services.py`: `_reset_execution_fields` moves here from `run_resume_services` as public `reset_case_for_restart(pivot, now)`, and `clear_checked_and_sent(pivot)` is extracted from it (used by Apply too). `run_resume_services` imports both; its behaviour is unchanged.
- `projects/services.reset_test_run`: also deletes the run's `CaseResultChange` rows and supersedes pending review revisions (one UPDATE each) (Q11).
- `projects/case_step_progress.py`: `EarlierPlan.help_request_id: int | None`, new `source`, and the label "after result review" vs "help at step N".
- `projects/testrail_mapping.format_step_breakdown`: for a review redesign, "The test plan was redesigned after a result review."
- **Blocked invariant**: Apply assigns the status from a variable. `CLAUDE.md` and the change note state that a person sets Blocked in two places: Mark blocked (`help_skipped`) and an applied result change (`person_review`). The agent only proposes.
- Re-run in place, Resume and help actions are not otherwise touched. Their changes make pending cards outdated through the fingerprints.

**machine_setup** (`machine_setup/services/`):

- `thread_role.py`: `ThreadRole.CASE_RESULT`; `thread_role(*, auto_fix_turn, help_request_id, result_case_id)`. The order is auto-fix, then help, then result, then MAIN.
- New pure **`machine_setup/case_result_brief.py`**: `CaseResultBriefData`, `build_case_result_brief` / `build_case_result_update`, capped at about 24k characters. The brief contains:
  - the case text;
  - the status and result text;
  - the plan with each step's final status, summary and carried labels, plus decisions;
  - which steps can be carried;
  - failed-attempt digests (capped, newest first);
  - help history (6), Earlier attempts (5), applied result changes (5), earlier plans;
  - the pending result change and the pending redesign;
  - the screenshot list (id, time, tool; ≤ 60, newest kept);
  - the log size and page count.

  It is framed as data, never instructions.
- New **`services/case_result_context.py`**: `load_result_case(pivot_id)` (one query, `defer("logs")`), `case_result_brief_data(pivot)` (plus about 6 small queries), `build_result_brief` / `build_result_update`, and `describe_result_status(pivot_id)` (one line for the prompt: "The case is Failed; result changes and redesigns can be proposed." / "The case is running again; proposals are refused.").
- New **`services/open_result_chat.py`**: `open_result_chat(*, project, pivot, user, text)`, mirroring `open_help_chat`:
  - under the project lock, requires `is_result_changeable`;
  - reuses the case's non-archived result thread, or creates it with the brief (transcript + display USER row without an author);
  - then calls `send_setup_message`. An empty text sends "Review this result and tell me whether it looks right, with the evidence." on a new thread and nothing on a reopened one;
  - a refused send raises `ResultChatSendRefusedError` and keeps the thread.
- New **`services/plan_step_args.py`**: `STEP_ITEM_SCHEMA`, `parse_plan_steps`, `required_text`, moved out of `help_chat_tools.py` (which imports them; its behaviour is unchanged).
- New **`services/result_chat_tools.py`**, bound to `(pivot_id, conversation_id, project_id)` and callbacks:
  - `get_case_result()` → `build_result_update`;
  - `read_case_log(page?)`: 12k-character pages, oldest first; loads only `logs`;
  - `ask_case_screenshot(screenshot_id, question)`:
    - the id must belong to this pivot; the file is ≤ 8 MiB, read via storage `image.open("rb")`;
    - `answer_screenshot_question(build_llm_config(), …)`, with the answer capped at 4000 characters; `AIServiceError` becomes a tool error;
  - `propose_result_change(status: passed|failed|blocked, summary, explanation)` → the projects service, then the `on_result_change_proposed(change_id)` card callback;
  - `propose_plan_change(steps, reason)` (same schema as the help tool, description says "Re-run with redesigned plan" instead of Continue) → `propose_review_plan_revision`, then the `on_plan_revision_proposed(revision_id)` card callback. It returns "Saved as a draft: steps 1–C carried; a re-run cleans up, restores their state and runs from step C+1. The person starts it; nothing has changed yet." A validation error becomes a tool error.
- `setup_tools.py`: `main_tool_definitions(CASE_RESULT)` = project-data tools + `run_setup_task` + the five result tools (Q5). `build_setup_dispatcher(..., result_case_id=, on_result_change_proposed=, on_plan_revision_proposed=)`.
- `setup_prompt.py`: `_build_result_chat_prompt(...)`. The persona job is "review a finished test case's result with a person; check the test machine through helpers when useful; propose a corrected result or a redesigned re-run for the person to apply". It keeps PROJECT DATA TOOLS, DELEGATION, TEST MACHINE, COMMUNICATION, REPORTS, SAFETY, CLEANUP FOLDER and REPLY, and replaces GOAL/METHOD/PLANNING with **RESULT CHAT RULES** (load-bearing; wording may be polished):
  - Base every claim on evidence: step summaries, digests, the log, screenshots, and what helpers see now. Say what you checked.
  - The machine has moved on since the case ran. What you see now is evidence about now. Use `read_only` helpers unless the person asks for changes.
  - Propose a result change only when the person asks for one or agrees with your suggestion. Never turn a real application defect into Passed. Blocked means "could not be judged".
  - The summary is the new result text a reviewer and TestRail will read: factual, self-contained, and it says what changed and why. The explanation is for the person.
  - Redesign when the test itself was misunderstood. Give the complete step list. Carry a step only when it passed and means the same thing on the machine; carried steps come first, in order, and at least one step runs. Each step is self-contained, keeps the "Afterwards, close …" clauses (`CLOSE_CLAUSE_PREFIX`), and has no verification-only steps.
  - Do **not** prepare the machine for the re-run: the server cleans up and restores the carried steps' state first. Undo anything your helpers changed that the cleanup would not.
  - Only the person applies a change or starts the re-run.
- `run_turn.py`: passes `result_case_id` to `thread_role` and the dispatcher, `help_status=describe_result_status(...)` for the result role, and `_on_result_change_proposed` / `_on_plan_revision_proposed`, which post the NOTICE card rows and broadcast them like `_on_runs_proposed`.
- `send_message._refusal(locked, now, target_kind)`: for `case_result`, refuses when a run of the project is **Started** (not Paused: user decision), plus the existing live-turn and machine-READY checks (Q6, see Decisions).
- `payloads.py`: `message_payload` adds `result_change` (`{change_id, status, status_label, from_label, to_label, summary, explanation, effects, outdated, decided_by_name, apply_url, discard_url}`) and `plan_redesign` (`{revision_id, status, reason, resume_step, old_rows, new_rows, outdated, confirm_body, rerun_url, discard_url}`; it reuses the existing draft-row serializer of `HelpThreadPayload`). The FKs are loaded with `select_related` (pivot `logs` deferred) where card rows are listed, and URLs are `reverse()`d.
- New **`services/result_card_decision.py`**: `apply_result_change_from_chat`, `discard_result_change_from_chat`, `rerun_redesign_from_chat`, `discard_redesign_from_chat`. Each scopes the id to the project, calls the `projects` service, and re-broadcasts the card row.
- `chat_page.py`: `CASE_THREAD_KINDS` (help kinds + `case_result`) for the list; `_thread_from_row` labels result threads "Result chat · <case> · <status label>"; `get_setup_page` builds a static `result_thread` header (case link, status badge, result excerpt of 300 characters).

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `machine_setup:result_chat_open` `/projects/<id>/machine-setup/result/<pivot_id>/` | POST | `@project_membership_required` | `OpenResultChatForm` (`text`, optional, ≤ 8000) | redirect to `machine_setup:thread` | pivot not in project → 404; not changeable → message + case page; send refused → thread page with message |
| `machine_setup:thread` (existing) | GET | same | `SetupMessageForm` | `chat.html` + `result_thread`, `help_mode_form` (for the re-run dialog) | 404 for a foreign thread |
| `machine_setup:result_change_apply` `…/machine-setup/result-changes/<change_id>/apply/` | POST | same | none (CSRF) | redirect to the thread + message | `ResultChangeError` → message; not in project → 404 |
| `machine_setup:result_change_discard` `…/result-changes/<change_id>/discard/` | POST | same | none | same | same |
| `machine_setup:plan_redesign_rerun` `…/machine-setup/redesigns/<revision_id>/rerun/` | POST | same | `RunHelpModeForm` (existing, `help_mode`) | redirect to the case page + "Re-running from step C+1." | `RunResumeError` / `PlanRevisionError` → message on the thread; not in project or not a review row → 404 |
| `machine_setup:plan_redesign_discard` `…/redesigns/<revision_id>/discard/` | POST | same | none | redirect to the thread | `PlanRevisionError` → message |

Templates (autoescaped or `x-text`; never `|safe` or `x-html`; JSON through `json_script`):

- **Case page** (`test_run_case_detail.html`):
  - Result card: "Discuss result" (dialog with an optional first message) or "Go to result chat", disabled with `result_chat_blocker` while a run is active;
  - a "Changed by <name> on <date>" line;
  - a "The agent proposed a redesigned test · review in the result chat" line when a review draft is pending;
  - a **Result changes** section;
  - Earlier attempts wording for `redesign`;
  - Earlier plans label for review records.

  View context (`projects/views.test_run_case_detail`): `can_discuss_result`, `result_thread_id`, `result_chat_blocker`, `result_changes`, `pending_review_redesign` (bool), `open_result_chat_form`. This adds about 3 queries.
- **Chat cards**:
  - `templates/machine_setup/includes/_result_change.html`: from → to badges, summary, explanation, effects, Apply (with confirm) / Discard; outdated cards show Discard only;
  - `includes/_plan_redesign.html`: the side-by-side `.plan-diff` (reusing the help panel classes), the reason, and the `confirm_body`. "Re-run with redesigned plan" opens a confirm dialog with `_help_mode_select.html` and the reopen gates' wording. Discard. Outdated or decided cards show their state only.
- **Thread list**: result threads.
- **CSS** (`punk-hazard.css`): `.result-change-card`, `.result-change-arrow`, `.result-header`, `.result-changes`, `.plan-redesign-card`.
- **JS**: card rendering in the chat message renderer, and `TOOL_LABELS` for the five tools.

### Background work and WebSockets

- No new Celery task. Turns run in `run_setup_turn` (`execution`). The redesign re-run queues through `reopen_run` → `queue_test_run_cases` (`execution`), with the generation bumped.
- No new socket event. Cards arrive and update through `machine_setup.message` upserts. Apply broadcasts the pivot status to the run and case groups. The re-run broadcasts through `reopen_run`, and the open case page already reloads when its case goes back to Created.
- Controller: no change, and no `CLIENT_VERSION` or capability change.

### Settings / infrastructure

- No new settings or requirements; the limits are module constants.
- Deploy: run `migrate` (`projects.0032`, `machine_setup.0006`), then restart Daphne/web and the execution workers (new checkpoint key, new orchestrator wording).
- Rollback, before reversing 0032:
  - delete review `CasePlanRevision` rows (`help_request IS NULL`), since the column becomes NOT NULL again;
  - `UPDATE … SET blocked_reason='help_skipped' WHERE blocked_reason='person_review'`;
  - `UPDATE projects_testruncaseresume SET kind='rerun' WHERE kind='redesign'`;
  - archive `kind='case_result'` conversations.

  Review records in checkpoints are dropped by the old parser (see Data model).

### Data flow

1. The person clicks **Discuss result** on a Failed case of a Done run. `open_result_chat` creates the thread with the brief and sends the first message.
2. The `CASE_RESULT` turn reads the brief, pages the log, asks about a screenshot, and may run a `read_only` helper on the machine. It answers with evidence, over several turns.
3. Either:
   - **a.** The agent calls `propose_result_change("passed", …)`: a card appears, and **Apply** writes the status and result, clears Checked/Sent and records the change. Counts and the 018 push plan follow automatically.
   - **b.** The agent calls `propose_plan_change(steps, reason)`: a card shows the side-by-side draft. **Re-run with redesigned plan** → confirm with the help mode → `rerun_with_redesigned_plan`. Under the locks and reopen gates, the redesign is applied (`source=review`, help cleared), an Earlier attempts entry is written, the case is reset like Resume, and `reopen_run` bumps the generation and queues the case.
4. In case b, the worker runs cleanup, the restore sub-task for steps 1..C (none when C = 0), then step C+1 onward. The verdict mentions the review redesign. A failed restore asks for help (`restore`). The case ends with a new agent verdict; its Checked/Sent marks are already cleared.

## Alternatives considered

- **Reuse the help chat with a synthetic help request.** Rejected: a help request means the run is paused on this case (Continue, the banner, Night auto-fix, PAUSED gating).
- **A separate review-revision model.** Rejected: `CasePlanRevision` plus a `source` and a nullable request keeps one validator, one draft view and one history. The help code filters by `help_request`, so it is unaffected.
- **Apply the redesign with Continue-style "no cleanup, no restore".** Rejected: the case finished and the machine moved on. Resume's cleanup + restore is the existing, tested way to recreate the carried state.
- **Queue the re-run outside `run_reopen_services`.** Rejected: the gates, the generation bump and the broker failure handling would be duplicated.
- **Re-run in place (`rerun_test_run_cases`) instead of Resume semantics.** Rejected: it clears the checkpoint, log and screenshots, which would lose the carried steps and their evidence.
- **A help-style side panel with its own socket event.** Rejected: cards on message upserts need no new event or consumer code.
- **Agent writes the status directly.** Rejected: it breaks the "agent proposes, a person applies" rule.

## Risks and mitigations

- **Prompt injection** (logs, summaries and screenshots contain application text) into an agent that has the machine and writes tester instructions:
  - a person is present;
  - nothing applies without a click on a card that shows the full change or diff;
  - SAFETY rules and the data framing apply;
  - texts are capped.

  This is the same residual risk class as the help chat.
- **Masking a defect**:
  - the server header names the person and the earlier status;
  - the history keeps the replaced verdict;
  - the prompt forbids flipping a real defect;
  - the Steps card still shows the executed steps;
  - a redesign re-runs at least one step and the verdict states what was carried.
- **Stale actions**:
  - fingerprints (result for changes, `plan_fingerprint` for redesigns);
  - locks project → run → pivot → row;
  - one pending row of each kind per case;
  - re-validation on re-run.
- **Machine contention**: result-chat sends are refused while any run is Started, so its helpers never disturb a running case. Accepted risk (user decision): while another run is Paused for Need help, the result chat may change the screen that paused case is waiting on. The re-run itself goes through `list_reopen_blockers`, which includes a live setup turn.
- **Restore can fail** on a machine that changed a lot. That is the existing path: Need help with `restore`, then Continue or Mark failed/blocked.
- **Data migrations on populated tables**: `CasePlanRevision.help_request` drop-not-null and `TestRunCaseResume.kind` widening are metadata-only on Postgres. The CheckConstraint validates existing rows, which all have a request.
- **Blocked invariant**: a second person-only writer with its own reason; docs are updated.
- **TestRail**: Apply and the re-run are refused while either push is live. Clearing Sent means a later send adds a new result, which TestRail shows as the latest.
- **Size and queries**:
  - brief ≤ ~24k characters, log pages 12k, screenshots ≤ 8 MiB;
  - case page about +3 queries, thread page +1;
  - card payloads via `select_related` (no N+1);
  - tools ≤ 6 indexed queries.
- **XSS**: escaped or `x-text` everywhere; `reverse()`d URLs; `confirm_body` built from counts only.
- **Authorization**: all views are membership-protected and every id is scoped to the project (and the screenshot to its pivot), else 404.
- **Import cycles**: the new `projects` modules import `run_reopen_services`, `case_checkpoint_services` and `agents.plan_revision`/`agents.checkpoint` (pure), never `agents.services` or `machine_setup.services`. `case_review_redesign_services` must not be imported by anything `projects.services` imports.

## Implementation steps

0. **Foundation** (orchestrator, sequential). Files:
   - `projects/models.py` + `projects/migrations/0032_case_result_chat.py`, `projects/exceptions.py` (`ResultChangeError`);
   - `machine_setup/models.py` + `machine_setup/migrations/0006_case_result_thread.py`, `machine_setup/thread_role.py`, `machine_setup/exceptions.py` (`ResultChatSendRefusedError`);
   - `agents/types.py` (`PlanRevisionRecord.help_request_id: int | None`, `source`), `agents/checkpoint.py`, `agents/plan_revision.py` (`apply_plan_revision(source=)`, review `paused_step`).

   Freeze the signatures above:
   - `propose_result_change`, `apply_result_change`, `discard_result_change`, `ResultChangeView`, `is_result_changeable`;
   - `propose_review_plan_revision`, `discard_review_plan_revision`, `rerun_with_redesigned_plan`, `ReviewDraftView`;
   - `reset_case_for_restart`, `clear_checked_and_sent`;
   - `open_result_chat`, the payload keys, the URL names and the case page context keys.
1. **Part A, projects and agents** (no `machine_setup` files). If split further, A1 = `agents/*` and A2 = `projects/*` + templates, whose files are disjoint.
   - `agents/services/orchestrator.py`, `agents/services/orchestrator_prompts.py`, `agents/CLAUDE.md`;
   - `projects/case_result_change_services.py` (new), `projects/case_result_text.py` (new), `projects/case_review_redesign_services.py` (new);
   - `projects/run_reopen_services.py`, `projects/run_resume_services.py` (import the moved helper only), `projects/services.py` (`reset_test_run` only);
   - `projects/case_step_progress.py`, `projects/testrail_mapping.py`, `projects/views.py` (`test_run_case_detail` context only);
   - `templates/projects/test_run_case_detail.html`;
   - `static/css/punk-hazard.css` (all new classes, including Part B's card classes).
2. **Part B, machine_setup**:
   - `machine_setup/case_result_brief.py` (new);
   - `services/case_result_context.py`, `open_result_chat.py`, `plan_step_args.py`, `result_chat_tools.py`, `result_card_decision.py` (all new);
   - `services/help_chat_tools.py` (import the moved helpers), `setup_tools.py`, `setup_prompt.py`, `run_turn.py`, `send_message.py`, `chat_page.py`;
   - `payloads.py`, `forms.py`, `views.py`, `urls.py`;
   - `templates/machine_setup/chat.html`, `includes/_result_change.html` (new), `includes/_plan_redesign.html` (new), `includes/_threads.html`, and the chat JS renderer and `TOOL_LABELS`.
3. **Integration** (orchestrator): seams; no `projects.*` → `machine_setup.services` import; `pre-commit run --all-files`, `manage.py check`, `makemigrations --check`.
4. **Docs**: `CLAUDE.md` (a Result chat paragraph under Need help, and the Blocked invariant note), `docs/changed/<date>-case-result-chat.md`.

## Verification

Manual; this project has no automated tests.

1. pre-commit (strict mypy), `manage.py check`, `makemigrations --check`; migrate up and down on a scratch database (with the rollback statements above).
2. Shell smoke on a throwaway database:
   - result changes: refused for Cancelled / Need help / In progress, the same status and oversize text; supersede; Apply writes status, header + summary, blocked reason (`person_review` / cleared) and clears Checked/Sent, while `finished_at` and the checkpoint stay; refused after a Re-run, after another Apply, or while Send checked is live; counts and `plan_testrail_push` reflect it;
   - redesigns: `apply_plan_revision(source="review")` on a Failed checkpoint (cursor past the end) sets `help=None`, phase `step`, `paused_step` = last failed step with its digest, and carried attempts; the checkpoint round-trips; the previous parser drops the review record and the checkpoint stays usable;
   - `_rebuild_state` gives the review line and a retry note; `redesign_for_help` ignores review records; the verdict prompt has the review sentence;
   - `rerun_with_redesigned_plan`: refused on a Paused or Started run, with a gate blocker (machine offline, live turn, push live), for a stale fingerprint, or for a help-source revision; otherwise it writes a REDESIGN history row, resets the case (log and screenshots kept), bumps the generation and queues after commit;
   - help-chat paths are unchanged (propose, apply on Continue, the help history lists only help rows).
3. Live, Failed case of a Done run: Discuss result with no text; reopen (same thread, nothing sent); `read_case_log` page 2; `ask_case_screenshot` own id and foreign id (tool error); a `read_only` helper looks at the machine.
4. Result change to Passed and Apply: the run row and counts update live; the case page shows the header, "Changed by" and the Result changes entry; Checked is cleared; Check + Send checked adds a new TestRail result; the 018 push lists the case as changed. Repeat with Blocked: the reason reads "Marked blocked by a person after review" and Resume is offered.
5. Redesign carrying steps 1 and 3 (dropping 2): the card diff, then Re-run with Night mode. The run reopens; cleanup runs; the restore sub-task restores the carried state; new step 3 runs; the Steps card shows carried rows and Earlier plans "after result review"; Earlier attempts shows "Re-run with a redesigned plan"; the verdict mentions the review redesign.
6. A redesign carrying nothing: cleanup then step 1, with no restore. A restore forced to fail: Need help with fault `resume_state_lost` and Continue mode `restore`.
7. Stale cards: propose twice (the older card shows Replaced); Re-run the case in place with pending cards (both show outdated and refuse); Apply a result change, then the redesign still works and supersedes nothing new.
8. Gates: with a run Started, Discuss result is disabled (a Paused run does not disable it) and sends are refused; with the runner disconnected, sends are refused (machine READY) and Re-run shows the reopen blocker.
9. Non-member: 404 on open, thread, apply, discard and rerun. Ids from another project: 404.
10. XSS: `<script>` in a case title, a step, the log, the agent's summary, explanation and draft steps, and a person's message, shown as text on the cards, the header, the case page and the history.
11. Run the `reviewer` and `verifier` agents in parallel.

## Decisions (answered 2026-10-26)

- **Q1.** Both: a result change (person applies) and a redesign with carried steps that re-runs the case from the first non-carried step. The person confirms with an explicit "Re-run with redesigned plan" click. It reuses `agents/plan_revision.py`, `CasePlanRevision` (now with `source`, the request optional) and `run_reopen_services`. Cleanup and restore run (Resume semantics).
- **Q2/Q3.** Passed / Failed / Blocked are eligible, and any may become any other. Blocked only through a person's Apply, with the new reason `person_review`.
- **Q4.** Apply and the redesign re-run both clear Checked and Sent, also after Sent.
- **Q5.** Full machine access (`run_setup_task`) in the result chat, so the send needs the machine READY.
- **Q6 (user decision: Started only).** Result-chat sends are refused while any run of the project is Started; a Paused run does not block them (the user accepts that a helper may disturb a paused case's screen). The redesign re-run additionally needs a Done or Cancelled run and passes `list_reopen_blockers`.
- **Q7–Q11.** As assumed:
  - no hand editing;
  - the server header + agent summary is the new text;
  - the run status is unchanged by Apply and there are no notifications;
  - one result thread per case, reused;
  - Reset deletes the result changes and supersedes pending review redesigns.

## Open questions

None.

## Changelog

- 2026-10-26: created (Draft).
- 2026-10-26: answers applied. Scope widened (Q1: redesign + re-run from the first non-carried step, through the Resume path with cleanup and restore; plan revisions decoupled from help requests via `CasePlanRevision.source` and an optional `PlanRevisionRecord.help_request_id`), full machine access (Q5) with the machine-READY gate kept, and result-chat sends refused while a run is Started (Q6). Status Ready.
- 2026-10-26: Q6 confirmed by the user as Started only (Paused runs do not gate the result chat).
- 2026-10-26: review pass 1. Fixed: review-redesign state line gated on the cursor (`next_step == resume_step`, no help) and its retry note dropped once a new-plan step ran; `auto_fix_eligible` treats a REDESIGN re-run as a from-scratch boundary; `read_case_log` page capped (`MAX_LOG_PAGE`); state line says the carried state "is restored before step C+1"; timeout bounds and plan validation shared via `agents/plan_revision_limits.py`; card joins defer the checkpoint; `broadcast_case_cards` limited to pending + just-decided rows; shared supersede in `projects/case_review_redesign_store.py`; `from_label` on `CaseResultPanel`; id-only card finders; merged person-name helper. Rejected: a per-role spec replacing the `ThreadRole` if/elif chains in `setup_tools`, `setup_prompt` and `run_turn` (reviewer finding 5). It is a refactor of the existing main/help/auto-fix roles outside this feature's scope; worth a separate plan. Accepted residuals (verifier, Low): the brief is a user-role transcript message (as in the help chat), and member names of people who changed results reach the LLM.
- 2026-10-26: review pass 2: superseded redesign cards hide the emptied diff; the redesign refusal no longer blames a result change. Pass 3 (narrow) clean apart from a docstring wording fix. Documented in docs/changed/2026-10-26-case-result-chat.md. Status Done.
