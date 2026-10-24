# feat: Help chat (talk to the agent about a case that needs help; redesign its test plan)

Status: Done
Created: 2026-10-24
Request: "I want ability to chat with the test agent when it needs help, sometimes I need more info or I need to discuss something with it which needs more than single message. On that chat, it should be able to re design the steps or have the full access to the test machine."
Follow-up (Q5): "I want it to be able to re design the whole test, maybe it misunderstood the whole test, and be able to map the old done steps to new one and mark them as passed."

Restated: when a case is **Need help**, a project member can open a multi-turn **help chat** about it. The agent in that chat starts with full context:

- the case text;
- the tester's plan with step progress;
- the failing step, and the tester's reason and ask;
- the failed attempt's actions digest;
- earlier notes and auto-fix reports.

It can use the test machine fully, through the existing Machine setup helper sub-tasks. It can **redesign the whole test plan**. In the new plan it may map leading new steps to old steps that already passed ("carried"), and those count as Passed without re-running. The person reviews the redesign side by side and ends the chat with the existing actions:

- **Continue**, optionally with the redesign. The case then resumes at the first new step that is not carried.
- **Mark failed**.
- **Skip**.

The agent can suggest an action; only the person acts. Type: `feat`.

## Context

- **Need help** (`docs/changed/2026-10-24-human-in-the-loop.md`, `docs/plan/feat-human-in-the-loop.md`):
  - The person's actions live in `projects/case_help_services.py`: `continue_after_help`, `mark_failed_after_help`, `skip_after_help`, `stop_auto_fix`, `continue_paused_run`, and `finish_case_auto_fix`.
  - Each action locks project → run, requires PAUSED, and resolves the single open `CaseHelpRequest`.
  - Continue writes the checkpoint `help` key (`_write_help_key`, `continue_pending=True`), sets the pivot to CREATED, and calls `queue_pending_cases`.
  - The case then resumes at the cursor without the pre-case cleanup or the restore sub-task (`projects/services.py` `execute_test_run_test_case`; `orchestrator.run_orchestrator`). The restore runs only for `0 < i < total`, never at step 1.
  - On resume, `_rebuild_state` anchors the help line and the retry note on the failed attempt of `help.step_num`.
  - Today the person can only add a note (≤ 2000 characters).
- **Auto-fix threads** (`machine_setup`):
  - `SetupConversation(kind=auto_fix, help_request=…)`, opened by `services/case_auto_fix.start_case_auto_fix`. Its first USER row comes from `auto_fix_brief.build_auto_fix_brief`.
  - A server turn (`SetupTurn.auto_fix=True`) gets `report_case_fix` and is settled from `run_turn._finish`.
  - A person can reply in the thread after that turn ends. Those turns are ordinary S0 turns: no case tools and the S0 prompt.
  - Thread page and socket: `machine_setup:thread` and `ws/projects/<id>/machine-setup/threads/<conversation_id>/`.
- **Setup lead** (`setup_prompt.py`, `setup_tools.py`, `run_turn.py`, `run_setup_task.py`):
  - The lead has project-data tools plus `run_setup_task`. The helper sub-agent has every tester machine tool, which is the "full machine access".
  - The transcript holds only delegation calls and reports.
  - One live turn per project.
  - A send is refused for a STARTED run or a machine that is not READY, and allowed for a PAUSED run.
- **Checkpoint** (`agents/checkpoint.py`, `agents/types.py`):
  - Keys: `plan`, `attempts` (`step_num`, kind step/recovery/resume, result), `next_step`, `decisions`, `phase`, `help`.
  - `CHECKPOINT_VERSION = 1`. New keys must be optional and lenient, and an unknown attempt kind makes the whole checkpoint unusable.
  - `_Progress.checkpoint()` rewrites the checkpoint from memory, so a new key must live in `_Progress`.
- **Consumers of `attempts` by step number**:
  - `final_step_indices`;
  - `_build_verdict` (pass = every planned step's final attempt passed, `len(finals) == len(plan)`) and `format_attempt_lines`;
  - `case_step_progress.build_case_step_progress`;
  - `case_teardown_prompts.pending_close_clauses`;
  - `case_checkpoint_services.list_resume_steps`;
  - `truncate_checkpoint` (Resume from step);
  - `case_auto_fix._failed_attempt_digest`.
- **Limits**: `ORCHESTRATOR_MAX_SUBTASKS` (30), and `agents/services/sub_task_budget.resolve_timeout_seconds` / `build_timeout_bounds`.
- **Import rule**: `projects.*` must never import `machine_setup.services`; it may import only the leaf `machine_setup.models` / `turn_state` / `auto_fix_stop` / `task_names`.

## Goals

- A person can open a help chat from a Need help case (case page, run banner). It reuses the Machine setup thread, turn, sub-task, recorder, Stop and limits machinery.
- There is one thread per help request. An existing Night auto-fix thread becomes the help chat; otherwise a new `case_help` thread is opened with the same brief.
- In person turns of a help thread, the agent gets a help-specific prompt, the project-data tools, `run_setup_task` (full machine), and three new tools: `get_help_case`, `propose_plan_change` and `propose_resolution`.
- **Whole-test redesign**:
  - the agent proposes a full new step list in which leading steps may be **carried** from old steps whose final attempt passed;
  - the proposal is validated and stored as a draft;
  - the thread page shows it side by side with the mapping;
  - it is applied only when the person presses Continue with "Use the redesigned plan" ticked;
  - carried steps are Passed, and the case resumes at the first step that is not carried, with no cleanup and no restore;
  - the earlier plan and its attempts stay visible as history.
- The thread page shows the help panel (request, draft, the agent's suggestion, and Continue / Mark failed / Skip / Discard draft), and actions return to the thread.

## Non-goals

- A hand editor for plans; the person asks the agent instead.
- Plan tools in the unattended Night auto-fix turn (Q2).
- Embedding the chat on the case page (Q4).
- Images in the lead's transcript; a read-only helper looks at the screen.
- Carrying old steps that did not pass, or carrying steps out of order or after a step that will run (see the validator).
- Automatically undoing the effects of old steps that are not carried. The chat agent prepares the machine before Continue (prompt rule).
- A new run or case status, a notification kind, or any controller protocol change.

## Design

### Data model

**`projects/models.py`**, migration `projects/0030_help_chat` (additive):

- `PlanRevisionStatus(TextChoices)`: `PROPOSED`, `APPLIED`, `DISCARDED` (by a person), and `SUPERSEDED` (by a newer draft, or because the request was resolved without applying it).
- New model **`CasePlanRevision`**:
  - `test_run_test_case` FK (CASCADE, `related_name="plan_revisions"`);
  - `help_request` FK → `CaseHelpRequest` (CASCADE, `related_name="plan_revisions"`);
  - `base_fingerprint` (CharField 64): sha256 of the canonical checkpoint `plan`, `attempts`, `next_step` and `plan_version` at proposal time;
  - `steps` (JSON list of `{description, expected_result, timeout_seconds, carried_from: int|null}`), the full new plan;
  - `previous_steps` (JSON list), the full old plan, kept for display after the checkpoint changes;
  - `resume_step` (PositiveSmallInteger) = carried count + 1;
  - `reason` (TextField ≤ 1000);
  - `status`, `proposed_at`, `decided_by` (FK user, SET_NULL), `decided_at`;
  - `conversation_id` (PositiveIntegerField, null). It is a plain id, so `projects` keeps no schema dependency on `machine_setup`.
  - `UniqueConstraint(fields=["help_request"], condition=Q(status="proposed"), name="plan_revision_one_pending")`;
  - index `(help_request, -proposed_at)`.
- **`CaseHelpRequest`** gains `agent_proposal_action` (blank / `continue` / `mark_failed` / `skip`), `agent_proposal_note` (≤ 2000) and `agent_proposal_at` (null). The defaults are constants, so there is no table rewrite.

**`machine_setup/models.py`**, migration `machine_setup/0004_case_help_thread`:

- `SetupConversationKind.CASE_HELP = "case_help", "Help chat"` (choices only).
- `UniqueConstraint(fields=["help_request"], condition=Q(help_request__isnull=False), name="setup_one_thread_per_help_request")`. Run the duplicate check first (Verification 1).

**Checkpoint** (`agents/types.py`, `agents/checkpoint.py`; the version stays 1 and every key is optional):

- **`attempts` and `decisions` always describe the current plan only.** Applying a revision moves the earlier plan's attempts and decisions into a revision record. Every existing consumer keyed by step number (`final_step_indices`, verdict, Steps card, teardown clauses, `list_resume_steps`, `truncate_checkpoint`) therefore stays correct without filtering. A rolled-back server also sees a self-consistent plan.
- `SubTaskAttempt.carried_from: int | None = None` (attempt key `carried_from`) marks a **carried attempt**. It is a synthetic `kind="step"`, `status="pass"` attempt written at apply time, one per carried new step:
  - `summary` is the old step's final passing summary;
  - `iterations` is 0;
  - `duration_seconds` is the old one.

  The kind stays `step`, so an older parser accepts it as a plain pass.
- `OrchestratorCheckpoint.plan_version: int = 0` (top-level key) counts the revisions applied to this case.
- `OrchestratorCheckpoint.plan_revisions: tuple[PlanRevisionRecord, ...] = ()` (key `plan_revisions`). Only the newest 5 records are kept, and a malformed record is dropped. These are display and prompt data only and never make a checkpoint unusable. Each record holds:
  - `version`, `revision_id`, `help_request_id`, `reason` (≤ 1000);
  - `previous_plan`: a tuple of `(description, expected_result)`, ≤ 30 entries, 500 characters each;
  - `earlier_attempts`: the old attempts, without `actions_digest`;
  - `earlier_decisions`;
  - `mapping`: a tuple of `(new_step, old_step)`;
  - `paused_step`: the old N;
  - `paused_digest`: the failed attempt's digest at old N, ≤ 6000;
  - `resume_step`.
- `CheckpointHelp` is unchanged. Its `step_num` still names the old step N. `_rebuild_state` recognises a redesign by `plan_revisions[-1].help_request_id == help.request_id`.
- **Rollback**: an older parser ignores `carried_from`, `plan_version` and `plan_revisions`. It sees the new plan, the carried passes and the cursor. The help line is missing (no failed anchor), which is acceptable.

### Validation and apply (pure, `agents/plan_revision.py`)

- `PlanStepInput(description, expected_result, raw_timeout, carried_from: int | None)`.
- `plan_fingerprint(checkpoint) -> str`.
- `carriable_steps(checkpoint) -> dict[int, SubTaskResult]` returns the old steps whose **final step attempt passed**. That is always some subset of the steps before the cursor N, because N's final attempt failed and later steps never ran.
- `validate_plan_revision(checkpoint, steps, *, bounds, max_steps) -> ValidatedRevision` raises `PlanRevisionInvalid(message)` (in `agents/exceptions.py`) with a model-readable message. The rules:
  1. 1 ≤ len(steps) ≤ `max_steps`. Each description is non-empty and ≤ 2000 characters; each expected result is ≤ 1000. Timeouts go through `resolve_timeout_seconds`. Total text is ≤ 24 000 characters.
  2. `carried_from` refers to a step in `carriable_steps` ("Step k cannot be carried: its final attempt did not pass").
  3. Each old step is carried at most once.
  4. **Carried steps form a prefix** of the new plan: once a step is not carried, no later step may be.
  5. Within the prefix, `carried_from` is **strictly increasing**. Gaps are allowed: an old step can be dropped.
  6. At least **one step is not carried**: `resume_step ≤ len(steps)`.
- `apply_plan_revision(checkpoint, validated, *, revision_id, help_request_id, reason) -> OrchestratorCheckpoint`:
  - `plan` = the new steps;
  - `attempts` = carried attempts for new steps 1..C;
  - `decisions` = ();
  - `next_step` = C + 1;
  - `plan_version` + 1;
  - appends the record (built from the old checkpoint);
  - keeps `help`, `browser`, `phase` (`awaiting_help`) and `total_iterations`.
- `build_plan_draft_view(previous_checkpoint, steps) -> PlanDraftView` builds the side-by-side rows:
  - `old_rows`: num, description, final status (passed / failed / not run / needs help), `carried_to` (new step number or None, which means "dropped");
  - `new_rows`: num, description, expected result, `carried_from`, and `state` ("passed, carried from step k" / "resumes here" / "will run").
- `attempt_origin_label(attempt, revisions) -> str` gives "Passed under the earlier plan as step k: <old description>" for a carried attempt. It is shared by the verdict prompt and the Steps card.

**Why the prefix rule plus strictly increasing order.** The checkpoint has a single cursor: every consumer treats "steps before `next_step`" as done and runs from the cursor onward. The restore sub-task and Resume-from-step make the same assumption.

- A carried step after a step that will run would have to count as passed **before** the step it depends on runs. It would also need a second cursor or skip-set, which every consumer would have to learn.
- Strictly increasing order keeps the carried state in the order the machine actually went through, so the state lines and later restores tell a true story.
- Gaps are safe to allow. The dropped step's effects are on the machine, and the chat agent (which has full access) is told to undo or account for them before proposing. The panel lists dropped steps explicitly.
- A rule that allows any mapping was rejected. It cannot be expressed with one cursor, and the user's case ("it misunderstood the whole test") is covered by the prefix.

**Why at least one step that is not carried.** Continue means "run again from here". A plan made only of carried steps would turn a Need help pause into a Passed verdict with nothing re-run after the help, which no person action allows today. If the earlier steps really were the whole test, the agent proposes a final verification step.

**No step carried: resume at step 1 with no cleanup and no restore (chosen).**

- The restore sub-task already never runs at step 1. The pre-case cleanup is skipped because the help chat exists precisely to put the machine into the right state, with a person watching. Cleanup would close the browser, kill the sessions and processes the helper set up, and empty the cleanup folder the helper may have just populated.
- This is safe because a person confirms Continue on a panel that says "Resumes at step 1 on the machine as it is now (no cleanup)". The prompt also tells the agent to bring the machine to the state the first new step expects, via helpers.
- The cleanup still runs before the next case, as always.

### Services

**agents**:

- `agents/plan_revision.py` (above), `agents/exceptions.py` (`PlanRevisionInvalid`).
- `agents/checkpoint.py`: (de)serialization of the new keys and lenient parsing. `truncate_checkpoint` is unchanged: carried attempts have current step numbers.
- `orchestrator.py`:
  - `_Progress` carries `plan_version` and `plan_revisions` into every checkpoint, and `_new_progress` loads them.
  - `_rebuild_state`:
    - a carried attempt writes the state line "Step j: <summary> (done before the plan was redesigned; earlier step k)";
    - when the latest record belongs to the current help, it adds, after the carried lines: "The test plan was redesigned during help: <reason>. Steps 1–C were carried over; continue at step C+1 from the current screen." plus the existing help line (ask, auto-fix summary, note);
    - it sets `retry_notes[resume_step]` to the earlier plan's failed attempt with `paused_digest`, labelled "earlier plan, step N", so the first new step knows what was done to the screen;
    - without a redesign, behaviour is unchanged.
  - `_applied_help` also accepts the redesign case (the help's request matches the latest record), so the verdict still names the help.
- `orchestrator_prompts.py`:
  - `format_attempt_lines(attempts, plan, revisions=())` writes a carried attempt as "Step j (<new description>): PASSED under the earlier plan as step k (<old description>), not re-run — <summary>";
  - `build_verdict_prompt` / `_deterministic_summary` add "The test plan was redesigned during help (reason). Steps 1–C were carried over from the earlier plan, where they had passed; they were not re-run. Mention this." The overall-status rule is unchanged: carried attempts are passes.
- `case_teardown_prompts`, `list_resume_steps` and `final_step_indices` need no change.
- `machine_setup/services/case_auto_fix._failed_attempt_digest` needs no change: a redesign happens only after the auto-fix brief.

**projects** (flat `<use_case>_services.py` modules):

- New **`projects/case_plan_revision_services.py`**:
  - `propose_plan_revision(*, help_request_id, steps: list[PlanStepInput], reason, conversation_id) -> PlanDraftView`:
    - locks project → run; requires the run PAUSED, the request open and the pivot NEED_HELP;
    - parses the checkpoint, raising "no plan to redesign" when there is none;
    - runs `validate_plan_revision`;
    - supersedes any pending draft, then creates the row (`previous_steps`, fingerprint, `resume_step`).
    - Raises `PlanRevisionError` (in `projects/exceptions.py`).
  - `discard_plan_revision(*, pivot, revision_id, user) -> None`.
  - `get_pending_plan_draft(help_request_id) -> PlanDraftView | None`. One query, built from the row (`previous_steps` and the stored statuses), so the page never re-parses a large checkpoint.
  - `apply_plan_revision_locked(*, pivot, request, revision_id, user) -> None` (internal, called from Continue under its locks):
    - `revision_id` must be the request's pending draft; otherwise `RunHelpError("The agent proposed a newer plan. Review it, then continue again.")`;
    - the fingerprint must match ("The case changed since this plan was proposed.");
    - it re-validates against the current checkpoint, runs `apply_plan_revision`, saves `execution_checkpoint`, and sets the row to APPLIED.
  - `supersede_pending_revisions(request) -> None`.
- New **`projects/case_help_proposal_services.py`**: `record_agent_proposal(*, help_request_id, action, note)` (same locks and checks) and `clear_agent_proposal(request)`.
- `projects/case_help_services.py`:
  - `continue_after_help(..., plan_revision_id: int | None, use_plan_revision: bool)`:
    - if a draft is pending and `use_plan_revision` is set, apply it; otherwise discard it;
    - then `_resolve`, `_set_continue_pending` (its `with_help` copies the revised checkpoint JSON) and `_queue_or_wait`.
  - Mark failed and Skip supersede the draft, so a skipped case resumes later on its old plan.
  - Every resolution clears the agent's proposal.
  - New `release_auto_fix_to_person(request)`: a running auto-fix with no thread yet becomes `not_fixed` without notifying.
  - Continue is refused while a **person's** turn is live in the request's thread ("The agent is still replying. Wait or press Stop."). A live auto-fix turn keeps today's resolve, stop and queue behaviour.
  - `HelpPanel` gains `pending_draft` and `latest_screenshot_url` (one query each). `_with_threads` widens to `auto_fix` and `case_help`.
- `projects/run_help_mode_services._request_waiting_for_person` skips a request that has a help thread, so a Night switch never starts an auto-fix over a person's chat.
- `projects/case_step_progress.py`:
  - `CaseStepRow` gains `carried_from` and `carried_label` ("Passed under the earlier plan as step k");
  - the row's history shows the earlier plan's attempts of step k, labelled "Earlier plan";
  - `CaseStepProgress` gains `earlier_plans`: one entry per record, with the old steps (number, description, final status from `earlier_attempts`, and "→ step j" or "dropped"), the reason and the version;
  - `StepProgressPayload` gains `carried_from` / `carried_label`.
- `projects/testrail_mapping.format_step_breakdown`: carried steps read "Passed (carried over from the earlier plan)", plus one line "The test plan was redesigned during help."

**machine_setup** (`machine_setup/services/`):

- New **`open_help_chat.py`**: `open_help_chat(*, project, pivot, user, text) -> SetupConversation`, under the project lock:
  - requires an open request on a PAUSED run;
  - reuses the request's thread if there is one;
  - otherwise calls `release_auto_fix_to_person`, creates `SetupConversation(kind=case_help, …)`, and writes the brief into `llm_transcript` and as a display USER row (author None, no turn);
  - then sends the first message through the existing `send_setup_message(conversation=thread)`. An empty `text` becomes "Look at the test machine read-only, tell me what you see and what you suggest." (Q3).
  - A refused send keeps the thread, and the page shows the error.
- `auto_fix_brief.py` → **`case_help_brief.py`**: `build_case_help_brief(data, *, audience)`, where the audience is `auto_fix` or `person`. The loader moves to **`case_help_context.py`**, shared by `start_case_auto_fix`, `open_help_chat` and `get_help_case`. It includes:
  - every plan step with its status;
  - **which steps can be carried**;
  - the help history;
  - earlier revisions;
  - the pending draft.
- New **`help_chat_tools.py`**:
  - `get_help_case()` returns the current brief from the database (≤ 24 000 characters), with the carriable list and whether the request is open.
  - `propose_plan_change(steps: array of {description, expected_result, timeout_seconds?, carried_from?}, reason)` calls `propose_plan_revision` and returns: "Saved as a draft: steps 1–C carried, resumes at step C+1. The person sees it next to the chat and applies it with Continue. Nothing has changed yet." A validation error is returned as a tool error, so the agent fixes the draft and calls again.
  - `propose_resolution(action, note)` calls `record_agent_proposal`.
  - Every handler re-checks the request (resolved, aborted, not paused → tool error) and broadcasts `help` after commit.
  - `ToolParameter.items_schema` (an optional JSON-schema dict, emitted by `llm_serializer`) describes the object items. The tester's schemas are unchanged byte for byte. The handler validates every field.
- `machine_setup/thread_role.py` (pure): `ThreadRole` is `MAIN`, `AUTO_FIX` (`report_case_fix` only) or `CASE_HELP` (help-chat tools). `setup_tools.main_tool_definitions(role)` and `build_setup_dispatcher(..., role, help_request_id)` use it.
- `setup_prompt.py`: `build_setup_system_prompt(..., role, help_status)`. For `CASE_HELP` the persona job is "help a person unblock a paused test case". The S0 GOAL, METHOD and PLANNING sections are replaced by **HELP CHAT RULES**. COMMUNICATION, REPORTS, SAFETY, CLEANUP FOLDER, HOUSEKEEPING (application context only when the person asks) and REPLY are kept. The rules (load-bearing; polish allowed):
  - Check the machine first with a `read_only` sub-task.
  - You may redesign the whole test when the tester misunderstood it. Give the complete new step list.
  - Carry an old step (`carried_from`) only when it passed and the new step means the same thing on the machine. Carried steps come first and in their original order.
  - Each step runs in a separate agent with no memory: make it self-contained (name the app, window or terminal). Keep the "Afterwards, close …" clauses (`CLOSE_CLAUSE_PREFIX`). No verification-only steps.
  - Before proposing, bring the machine (through helpers) to the state the first new step expects. Undo or account for the effects of old steps you dropped. Continue runs without any cleanup.
  - Never redesign around a real application defect; suggest `mark_failed` instead.
  - If you or a helper carried out a test action yourselves, do not carry it: add a step that verifies its result.
  - Only the person resolves the case.
- `run_turn.py` picks the role (`AUTO_FIX` if `turn.auto_fix`, else `CASE_HELP` if `conversation.help_request_id`, else `MAIN`) and passes it on. `help_status` costs one query.
- `chat_page.py`: `list_auto_fix_threads` → `list_help_threads` (both kinds; `case_help` labels come from the request's resolution). `get_setup_page` builds `thread` for both kinds.
- `events.py` / `payloads.py`: `broadcast_help(project_id, HelpThreadPayload)`, group event `machine_setup.help`. `HelpThreadPayload` holds:
  - `conversation_id`, `request_open`, `resolution_label`;
  - `agent_proposal` `{action, action_label, note}` | None;
  - `plan_draft` `{revision_id, reason, resume_step, old_rows[…], new_rows[…]}` | None, with capped text.
- `consumers.py`: `machine_setup_help` → `{"type": "help", "help": …}`. `current_state` carries `help` for help threads.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `machine_setup:help_chat_open` `/projects/<id>/machine-setup/help/<pivot_id>/` | POST | `@project_membership_required` | `OpenHelpChatForm` (`text`, optional, ≤ 8000) | redirect to `machine_setup:thread` | pivot not in project → 404; not paused / no open request → message + case page; send refused → thread page with message |
| `machine_setup:thread` (existing) | GET | same | `SetupMessageForm` | `chat.html` + `help_panel`, `help_form` (note never pre-filled from the agent's proposal, `use_plan_revision` initially True; unticked on the case page, which only summarises the draft), `help_case_url`, `threads` | 404 for a foreign thread |
| `projects:test_run_case_help_continue` / `_fail` / `_skip` (existing) | POST | same | `CaseHelpResolutionForm` + `plan_revision_id` (hidden, optional), `use_plan_revision` (checkbox), `return_thread_id` (hidden, optional) | case page, or `machine_setup:thread` when `return_thread_id` is the non-archived thread **of this request** (checked in the service, `reverse()`d) | `RunHelpError` (newer draft, case changed, agent still replying, gates) → message |
| `projects:test_run_case_plan_revision_discard` `/projects/<id>/test-runs/<run_id>/cases/<pivot_id>/help/plan/<revision_id>/discard/` | POST | same | `HelpReturnForm` (`return_thread_id`) | same redirect rule | `PlanRevisionError` → message; 404 if the revision is not this pivot's |

Templates (autoescaped or `x-text`; never `|safe` or `x-html`; JSON through `json_script`):

- **Thread page**, new include `templates/machine_setup/includes/_help_panel.html`:
  - Header: case, old step N, ask, waiting time, latest screenshot link.
  - **Redesign draft**, side by side (`.plan-diff`):
    - left, the **current plan**: each step with its status badge and the mapping ("→ new step j", or "dropped");
    - right, the **new plan**: each step with "Passed · carried from step k", "Resumes here" (the first step that is not carried) or "Will run", plus its expected result;
    - the reason; a line "Continue resumes at step C+1 on the machine as it is now (no cleanup)"; Discard.
    - On a narrow screen the two columns stack, and the mapping labels carry the link.
  - **Agent suggests**: the callout with the action and note.
  - The resolution form with `return_thread_id`. Continue is disabled while a turn is live or a gate fails.
  - The Alpine `helpPanel` reads `initial-help` and updates the draft and suggestion on `help` messages. The hidden `plan_revision_id` follows the shown draft.
- **Case page** (`_case_help_card.html`):
  - "Open help chat" (optional first message), or "Go to help chat";
  - a draft summary ("The agent proposed a redesigned test: N steps, C carried, resumes at step C+1") linking to the chat;
  - the agent's suggestion;
  - Continue gains `use_plan_revision` and the hidden id.

  The auto-fix branch adds "Stop auto-fix to talk to it yourself".
- **Steps card** (`test_run_case_detail.html`):
  - carried rows show the Passed badge plus "Carried over" and "Passed under the earlier plan as step k: <old description>";
  - their history disclosure lists the earlier attempts labelled "Earlier plan";
  - a collapsible **Earlier plans** section lists each previous plan (step, status, → new step or dropped) with the reason and the help it came from;
  - the live `steps` payload carries `carried_from` / `carried_label`.
- **Help history**: applied and discarded drafts per request.
- **Run banner**: a "Help chat" link.
- **Thread list**: "Help chat · <case> · step N · <state>".
- **CSS** (`punk-hazard.css`): `.help-panel`, `.plan-diff*` (`--carried`, `--dropped`, `--resume`), `.help-suggestion`, `.step-carried`, `.earlier-plans`.

### Background work and WebSockets

- No new Celery task. Opening the chat uses `send_setup_message` → `run_setup_turn` (`execution`). Continue uses `queue_pending_cases`.
- WebSocket: one new setup event, `machine_setup.help`. The case socket's `steps` payload gains two fields. The applied plan reaches open case pages on reload or on the resumed case's first checkpoint.
- Controller: no change; no `CLIENT_VERSION` or capability change.

### Interaction rules

- **End of chat**: only the person resolves, from the case page or the thread panel. `propose_resolution` is shown to the person only; its note is never pre-filled or sent to the tester. After resolution the thread stays usable as a setup chat, and the help tools return "no longer waiting for help".
- **Resume point**: the first step that is not carried (`next_step = C + 1`), with no cleanup and no restore. This includes C = 0 (step 1). Without a draft, the behaviour is unchanged (old step N).
- **Night auto-fix**:
  - While the auto-fix is running, "Help chat" opens the auto-fix thread. Stop (existing) settles it not fixed, and the person's next turn is `CASE_HELP`.
  - If the start task is still queued, opening the chat releases the request to the person, and the task then does nothing.
  - A switch to Night does not start an auto-fix over an open help chat.
- **Abort**: unchanged. The request is resolved `cancelled`, drafts are superseded, and the help tools fail closed. A person's live turn is not stopped.
- **Staleness**:
  - every write locks project → run and checks PAUSED, the open request and NEED_HELP;
  - the draft fingerprint covers the plan, attempts, cursor and version;
  - Continue applies only the draft id the person saw, and re-validates;
  - one pending draft per request;
  - no worker writes the checkpoint while the run is paused, and `queue_pending_cases` bumps the generation afterwards.
- **Q10 eligibility** (`auto_fix_eligible`) keys on `step_num` across requests. After a redesign, a later request's step number may coincide with an earlier one's, which sends that case to a person instead of the auto-fix. Accepted: conservative, rare, and the safe side.

### Settings / infrastructure

- No new settings or requirements.
- Deploy:
  - run `migrate` (`projects.0030`, `machine_setup.0004`);
  - restart Daphne/web and the execution workers.
- Rollback:
  - archive the `kind='case_help'` conversations;
  - the `CasePlanRevision` history is lost with the table;
  - checkpoints stay usable (see Data model).

### Data flow

1. Case X pauses at step N. The person clicks **Open help chat**. `open_help_chat` reuses or creates the thread and starts the first turn.
2. The `CASE_HELP` turn checks the screen read-only, talks with the person over several turns, and does machine work through `run_setup_task`.
3. The agent decides the test was misunderstood. It prepares the machine, then calls `propose_plan_change` with a full list whose first steps carry old passed steps. The draft is validated and stored, and the `help` event fills the side-by-side panel.
4. The agent calls `propose_resolution("continue", note)`, which the panel shows next to the chat (the note is not pre-filled).
5. The person presses **Continue** with the draft ticked. Under the locks:
   - the draft is re-checked (id, fingerprint, validity) and applied: new plan, carried passes, earlier plan in a record, cursor C+1, version +1;
   - the request is resolved, the help key is written, the pivot goes to CREATED and the run is queued.
6. The worker resumes at step C+1 with no cleanup or restore. The state has the carried lines, the redesign line, the help line, and the earlier plan's failed digest as the first step's retry note.
7. The verdict mentions the help and the redesign. The Steps card shows carried steps as Passed (earlier plan) and lists the earlier plan.

## Alternatives considered

- **A separate chat stack, or a chat inside the tester orchestrator.** Rejected: the setup lead already has delegation, a persistent transcript, Stop and limits, while the orchestrator is a one-shot loop that holds an execution slot.
- **Applying drafts immediately.** Rejected (Q1): the plan becomes instructions for an executor with shell access, so a person sees it first. Continue is the confirmation.
- **Keeping old attempts in `attempts`, tagged with a plan version.** Rejected: every step-number consumer (verdict, Steps card, teardown, resume list, truncate) would need filtering, and a rolled-back server would mix old and new numbering. Moving them into the revision record keeps `attempts` consistent with `plan`.
- **Carried steps anywhere in the new plan, or in any order.** Rejected: there is one cursor (see the rule's justification).
- **Cleanup before step 1 when nothing is carried.** Rejected: it would destroy the state the chat just prepared, and the restore already never runs at step 1. A person confirms on a panel that states "no cleanup".
- **A new attempt kind `carried`.** Rejected: an unknown kind makes the checkpoint unusable on an older server. A `step` pass with an optional `carried_from` key is not affected.
- **A screenshot in the lead's transcript.** Rejected: images get rewritten and replayed every round. A read-only helper sees the live screen.

## Risks and mitigations

- **Prompt injection into an agent with shell that now writes tester instructions.** Mitigations:
  - a person is present;
  - drafts apply only on an explicit Continue with a visible mapping;
  - the existing SAFETY rules apply;
  - "read as data" framing;
  - size and count caps.

  The residual risk is the same class as the planner today.
- **A redesign that masks a defect, or carries a step that did not really happen.**
  - Only steps whose final attempt passed can be carried.
  - The side-by-side view shows every mapping and every dropped step.
  - The verdict, the Steps card and TestRail state that the plan was redesigned and which steps were carried.
  - The prompt rule says to suggest Mark failed instead.
- **Effects of dropped old steps** remain on the machine. The prompt rule makes the agent account for them. The panel lists dropped steps, and the person can ask before continuing.
- **XSS**: all text is escaped or rendered with `x-text`, and `return_thread_id` is resolved server-side.
- **Authorization**: every view is membership-protected, and ids are scoped to the project, run and request (404 otherwise).
- **Constraint migration**: run the duplicate check first.
- **Size**: the records cap the earlier plans (5 records; attempts without digests; one paused digest ≤ 6000 each), which is about ≤ 60 KB worst case per checkpoint. The brief is ≤ 25 KB, `get_help_case` ≤ 24 KB per call, and the draft arguments ≤ 24 KB.
- **Queries**:
  - thread page: about 5 existing queries + help panel ≈ 4 + draft 1 + screenshot 1;
  - case page: +2;
  - tools: ≤ 4 indexed queries;
  - draft view is built from the row, without re-parsing the checkpoint.
- **`_Progress` dropping the new keys**: called out in Part A and covered by smoke check 2.

## Implementation steps

0. **Foundation** (orchestrator, sequential; everything else builds on it):
   - files:
     - `projects/models.py` + `projects/migrations/0030_help_chat.py`, `projects/exceptions.py` (`PlanRevisionError`);
     - `machine_setup/models.py` + `machine_setup/migrations/0004_case_help_thread.py`, `machine_setup/thread_role.py`;
     - `agents/types.py` (`PlanRevisionRecord`, `SubTaskAttempt.carried_from`, `OrchestratorCheckpoint.plan_version` / `plan_revisions`, `ToolParameter.items_schema`), `agents/checkpoint.py`, `agents/exceptions.py`, `agents/plan_revision.py`, `agents/services/llm_serializer.py`.
   - Freeze these contracts:
     - `validate_plan_revision`, `apply_plan_revision`, `build_plan_draft_view` / `PlanDraftView`, `attempt_origin_label`;
     - `propose_plan_revision`, `record_agent_proposal`, `continue_after_help(..., plan_revision_id, use_plan_revision)`, `open_help_chat`;
     - `HelpThreadPayload`;
     - `StepProgressPayload.carried_from` / `carried_label`, `CaseStepProgress.earlier_plans`;
     - context keys `help_panel`, `help_form`;
     - event `machine_setup.help`.
1. **Part A, orchestrator and step display derivations**:
   - files: `agents/services/orchestrator.py`, `agents/services/orchestrator_prompts.py`, `projects/case_step_progress.py`, `projects/testrail_mapping.py`, `agents/CLAUDE.md`.
2. **Part B, projects services and case UI**:
   - files:
     - `projects/case_plan_revision_services.py` (new), `projects/case_help_proposal_services.py` (new), `projects/case_help_services.py`, `projects/run_help_mode_services.py`;
     - `projects/forms.py`, `projects/views.py`, `projects/urls.py`;
     - `templates/projects/includes/_case_help_card.html`, `templates/projects/includes/_run_help_banner.html`, `templates/projects/test_run_case_detail.html` (Steps card carried rows, Earlier plans, help history, `steps` JS fields);
     - `static/css/punk-hazard.css` (all new classes, including Part C's `.help-panel`, `.plan-diff*` and `.help-suggestion`).
3. **Part C, machine_setup help chat**:
   - files:
     - `machine_setup/services/open_help_chat.py` (new), `machine_setup/services/case_help_context.py` (new), `machine_setup/case_help_brief.py` (renamed from `auto_fix_brief.py`);
     - `machine_setup/services/case_auto_fix.py`, `machine_setup/services/help_chat_tools.py` (new), `machine_setup/services/setup_tools.py`, `machine_setup/services/setup_prompt.py`, `machine_setup/services/run_turn.py`, `machine_setup/services/chat_page.py`;
     - `machine_setup/events.py`, `machine_setup/payloads.py`, `machine_setup/consumers.py`, `machine_setup/forms.py`, `machine_setup/views.py`, `machine_setup/urls.py`;
     - `templates/machine_setup/chat.html`, `templates/machine_setup/includes/_help_panel.html` (new), `templates/machine_setup/includes/_threads.html`.
   - Part C gets its CSS class names from the Foundation contract; Part B writes them.
4. **Integration** (orchestrator):
   - wire the seams;
   - check there is no `projects.*` → `machine_setup.services` import;
   - `pre-commit run --all-files`, `manage.py check`, `makemigrations --check`.
5. **Docs**: `CLAUDE.md` (Need help section: help chat, redesign and carried steps); `docs/changed/<date>-help-chat.md`.

## Verification

Manual; this project has no automated tests.

1. Before migrating, run `SetupConversation.objects.exclude(help_request=None).values("help_request").annotate(n=Count("id")).filter(n__gt=1)`; it must be empty. Then pre-commit (strict mypy), `manage.py check`, `makemigrations --check`, and migrate up and down on a scratch database.
2. Shell smoke (rolled back):
   - `validate_plan_revision` accepts a full redesign with carried prefix 1→1, 2→3 (a gap).
   - It refuses:
     - a carried step that failed or never ran (including the paused step N);
     - an old step carried twice;
     - a carried step after a step that is not carried;
     - non-increasing `carried_from`;
     - all steps carried;
     - an empty description;
     - more than 30 steps;
     - oversize text.
   - `apply_plan_revision`: plan, carried attempts, `next_step = C+1`, `decisions = ()`, `plan_version + 1`, a record with `earlier_attempts`, `paused_digest` and `mapping`.
   - The checkpoint round-trips. An old checkpoint loads. A record with garbage is dropped and the checkpoint stays usable.
   - `_new_progress` → `checkpoint()` keeps `plan_version` / `plan_revisions`.
   - `_rebuild_state` gives the carried lines, the redesign and help lines, and `retry_notes[C+1]` with the digest.
   - `truncate_checkpoint` after a redesign keeps the carried attempts below `from_step`.
   - `build_case_step_progress` gives carried rows and `earlier_plans`.
   - `format_attempt_lines` / `build_verdict_prompt` contain the carried wording.
3. **Attended**, live, with a forced environmental failure at step 4 (steps 1–3 passed):
   - Open help chat with no text: the agent inspects read-only and replies.
   - A multi-turn conversation works.
   - A helper sub-task changes the machine.
4. **Full redesign with carries**:
   - Ask for a redesign that carries old steps 1 and 3 (dropping 2) and replaces the rest. The panel shows the side-by-side mapping, "dropped" on old step 2 and "Resumes here" on new step 3.
   - Continue with the draft ticked:
     - the case resumes at new step 3 with no cleanup or restore;
     - the Steps card shows new steps 1–2 Passed with "Carried over… earlier plan as step 1 / 3";
     - Earlier plans lists the old plan with its statuses and mappings;
     - the verdict mentions the redesign and the carried steps;
     - a passing run ends Passed;
     - the TestRail breakdown says it was carried over.
5. A redesign carrying **nothing**: the panel says "Resumes at step 1 on the machine as it is now (no cleanup)". On Continue there is no cleanup (the runner's cleanup folder is untouched) and no restore, and the old attempts appear only under Earlier plans.
6. Ask for an invalid mapping (carry the failed step 4, or carry step 3 after a new step): the agent gets the tool error and proposes again, and no draft is stored for the invalid call.
7. Drafts:
   - Discard, then propose again: only the newest is pending.
   - Continue with the draft unticked: the old plan is used and the draft is discarded.
   - Mark failed or Skip with a pending draft: the draft is superseded, and a later Resume of the skipped case uses the old plan.
8. Races:
   - Submit Continue with an older draft id after the agent proposes a newer one: refused.
   - Continue while the agent is replying: refused.
   - Continue after a Resume or Abort changed the case: refused (fingerprint, or not paused).
9. `propose_resolution("mark_failed", …)`: the panel shows it (the note field stays empty), and nothing changes until the person clicks.
10. **Night**:
    - while the auto-fix is running, the help chat is the auto-fix thread; Stop, then type, and the turn has the plan tools (the auto-fix turn had none);
    - opening the chat while the start task is queued: no auto-fix thread, state not fixed;
    - a Night switch with a help chat open: no auto-fix.
11. Abort mid-chat: the tools refuse and the thread stays readable. A second member lands on the same thread. A non-member gets 404 on the thread, the actions and discard.
12. XSS: `<script>` in a case title, a step, a draft step description and a person's message is shown as text on the case page, the panel, the diff and the Steps card.
13. Rollback rehearsal on a scratch copy: load a revised checkpoint with the previous commit's `checkpoint_from_json`. It parses, and the resume runs from C+1.
14. Run the `reviewer` and `verifier` agents in parallel.

## Decisions (answered 2026-10-24)

- **Q1.** The agent only drafts. Continue applies the draft the person sees ("Use the redesigned plan": ticked by default on the help chat page, which shows it side by side; unticked on the case page, which only summarises it).
- **Q2.** No plan tools in the unattended Night auto-fix turn; only turns a person starts get them.
- **Q3.** Opening the help chat starts the agent at once (optional first message; the default asks for a read-only look and a summary).
- **Q4.** The chat lives on the Machine setup thread page, with the help panel and the actions there, linked from the case page and the run banner.
- **Q5. Changed by the user.** The agent may redesign the whole test and map old passed steps to new ones, which then count as Passed. Implemented as:
  - a full new step list with optional `carried_from`;
  - only steps whose final attempt passed can be carried, each at most once;
  - carried steps form a strictly increasing prefix, and at least one step is not carried;
  - the case resumes at the first step that is not carried, with no cleanup and no restore, also at step 1;
  - the earlier plan and attempts move into a revision record shown as history.

## Open questions

None.

## Changelog

- 2026-10-24: created (Draft).
- 2026-10-24: Q1–Q4 accepted as recommended. Q5 changed by the user: whole-test redesign with carried steps. Changes:
  - the tool now takes a full step list with `carried_from` (no `from_step`);
  - validator rules (passed-only, once each, strictly increasing prefix, at least one step that is not carried);
  - the checkpoint moves the earlier plan's attempts and decisions into `plan_revisions` records, and adds carried `step` attempts (`carried_from`) and `plan_version`. The per-attempt `plan_version` marker was dropped;
  - resume at C+1 with no cleanup or restore, also when nothing is carried;
  - side-by-side mapping view, carried rows and an Earlier plans section on the Steps card, carried wording in the verdict and TestRail;
  - Verification updated.

  Status Ready.
- 2026-10-24: review pass 1 (reviewer + verifier), all findings fixed:
  - the agent's `propose_resolution` note is no longer pre-filled into the person's Continue note (it would reach the tester as "Person: …"); a draft arriving over the socket unticks "Use the redesigned plan";
  - record/snapshot text caps, fingerprint without `checkpoint_to_json`, `get_help_case` returns an ≤ 8k case update, superseded drafts drop their step JSON, abort supersedes drafts and clears the proposal;
  - verdict keeps the redesign sentence after a later help; `person_turn_live` in `machine_setup/turn_state.py`; lock order project → run → request → case everywhere; single save on Continue; `help_thread_state.py`.
  - Accepted (not changed): no `help` broadcast from `projects` on resolve/discard (pages redirect back); `case_step_progress` builds the carried label locally (import cycle); Continue refuses whenever the pending draft differs from the shown one.
- 2026-10-24: review pass 2: setup prompt no longer says the suggestion pre-fills the form; the case page leaves "Use the redesigned plan" unticked (it shows only a summary); help reads defer `logs`. Pass 3 (reviewer + verifier) clean. Rejected: annotating the help-history step count instead of loading `steps` (bounded by person discards, not worth the complexity).

## Frozen contracts (Foundation, written 2026-10-24)

Already implemented (do not change signatures; read the code):

- `agents/types.py`: `SubTaskAttempt.carried_from`, `PlanRevisionRecord`, `OrchestratorCheckpoint.plan_version` / `plan_revisions`, `ToolParameter.items_schema` (serialized by `llm_serializer`).
- `agents/checkpoint.py`: (de)serialization, `MAX_PLAN_REVISION_RECORDS`.
- `agents/exceptions.py`: `PlanRevisionInvalid(message)`.
- `agents/plan_revision.py`: `PlanStepInput`, `RevisionStep`, `ValidatedRevision`, `PlanStepSnapshot`, `snapshot_plan`, `DraftOldRow`, `DraftNewRow`, `PlanDraftView`, `plan_fingerprint`, `carriable_steps`, `validate_plan_revision`, `apply_plan_revision`, `build_plan_draft_view`, `attempt_origin_label`, `revision_steps_to_json/_from_json`, `snapshots_to_json/_from_json`. `CasePlanRevision.steps` stores `revision_steps_to_json`; `previous_steps` stores `snapshots_to_json(snapshot_plan(checkpoint))`.
- `projects/models.py`: `HelpProposalAction`, `CaseHelpRequest.agent_proposal_{action,note,at}`, `PlanRevisionStatus`, `CasePlanRevision`, `PLAN_REVISION_REASON_MAX_LENGTH`. `projects/exceptions.py`: `PlanRevisionError(message)`.
- `machine_setup/models.py`: `SetupConversationKind.CASE_HELP`, constraint one thread per help request. `machine_setup/thread_role.py`: `ThreadRole`, `thread_role(auto_fix_turn=, help_request_id=)`.

To be written by Part B (consumed by Part C):

- `projects/case_plan_revision_services.py`
  - `propose_plan_revision(*, help_request_id: int, steps: Sequence[PlanStepInput], reason: str, conversation_id: int | None) -> PlanDraftView` — raises `PlanRevisionError` (validation messages from `PlanRevisionInvalid` passed through; "This case is no longer waiting for help." when the run is not paused / request resolved / pivot not NEED_HELP; "There is no test plan to redesign yet." without a parseable checkpoint).
  - `get_pending_plan_draft(help_request_id: int) -> PlanDraftView | None` (one query).
  - `discard_plan_revision(*, pivot: TestRunTestCase, revision_id: int, user: CustomUser) -> None`.
- `projects/case_help_proposal_services.py`
  - `record_agent_proposal(*, help_request_id: int, action: HelpProposalAction, note: str) -> None` — raises `RunHelpError("This case is no longer waiting for help.")`.
- `projects/case_help_services.py`
  - `continue_after_help(*, pivot, user, note, plan_revision_id: int | None = None, use_plan_revision: bool = True) -> HelpActionResult`.
  - `lock_help_request_for_chat(*, project: Project, pivot_id: int) -> CaseHelpRequest` — must be called inside `transaction.atomic()`; locks project → run → open request; raises `RunHelpError` unless the run is PAUSED with an open request for that pivot; if the request's auto-fix is `running` and the request has no thread yet, sets it `not_fixed` without notifying (the plan's `release_auto_fix_to_person`). Returns the request with `test_run_test_case` loaded.
  - `help_request_waiting(help_request_id: int) -> bool` — one query: request open, run PAUSED, pivot NEED_HELP.
  - `HelpPanel` gains `pending_draft: PlanDraftView | None` and `latest_screenshot_url: str` ("" when none); `thread` now covers `auto_fix` and `case_help` threads. `get_help_panel(test_run, project, *, pivot_id=None)` keeps its signature.
  - `resolve_return_thread(*, pivot: TestRunTestCase, thread_id: int | None) -> int | None` — the id only if it is the non-archived thread of this pivot's latest help request.
- `projects/forms.py`: `CaseHelpResolutionForm` gains `plan_revision_id` (hidden int, optional), `use_plan_revision` (BooleanField, required=False, initial=True), `return_thread_id` (hidden int, optional). New `HelpReturnForm` (`return_thread_id`, hidden, optional).
- URL `projects:test_run_case_plan_revision_discard` (kwargs `project_id`, `test_run_id`, `pivot_id`, `revision_id`), POST.
- CSS classes (in `punk-hazard.css`): `.help-panel`, `.help-panel-header`, `.help-suggestion`, `.plan-diff`, `.plan-diff-col`, `.plan-diff-row`, `.plan-diff-row--carried`, `.plan-diff-row--dropped`, `.plan-diff-row--resume`, `.plan-diff-map`, `.step-carried`, `.earlier-plans`.

To be written by Part A (consumed by Part B's templates):

- `projects/case_step_progress.py`: `CaseStepRow.carried_from: int | None`, `CaseStepRow.carried_label: str` ("" when not carried); history rows of carried steps include the earlier plan's attempts of step k with `StepAttemptRow` label prefixed "Earlier plan · ". `CaseStepProgress.earlier_plans: tuple[EarlierPlan, ...]` with `EarlierPlan(version: int, reason: str, help_request_id: int, resume_step: int, steps: tuple[EarlierPlanStep, ...])`, `EarlierPlanStep(num: int, description: str, status_label: str, carried_to: int | None)`. `StepRowPayload` gains `carried_from: int | None`, `carried_label: str`.

To be written by Part C (consumed by Part B's templates):

- URL `machine_setup:help_chat_open` (`help/<int:pivot_id>/` under the project include; kwargs `project_id`, `pivot_id`), POST, form `machine_setup.forms.OpenHelpChatForm` (field `text`, optional, ≤ 8000).
- `machine_setup/services/open_help_chat.py`: `open_help_chat(*, project: Project, pivot: TestRunTestCase, user: CustomUser, text: str) -> SetupConversation`.
- WebSocket: group event `machine_setup.help` → client message `{"type": "help", "help": HelpThreadPayload}`; `HelpThreadPayload` in `machine_setup/payloads.py`.
