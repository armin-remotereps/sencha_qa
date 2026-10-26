# Result chat: discuss a finished case's result with the agent, change it or re-run with a redesigned plan

Date: 2026-10-26
Plan: docs/plan/feat-case-result-chat.md

## What changed
- `projects/models.py`, migration `0032_case_result_chat`: `CaseResultChange` (one pending per case), `ResultChangeStatus`, `PlanRevisionSource`; `CasePlanRevision.help_request` nullable + `source` (constraints `plan_revision_one_pending_review`, `plan_revision_source_matches_request`); `BlockedReason.PERSON_REVIEW`; `CaseRestartKind.REDESIGN` (`TestRunCaseResume.kind` widened to 16).
- `machine_setup/models.py`, migration `0006_case_result_thread`: kind `case_result`, `SetupConversation.result_case` (one non-archived thread per case), `SetupMessage.result_change` / `.plan_revision` card FKs. `thread_role.ThreadRole.CASE_RESULT`.
- `agents`: `PlanRevisionRecord.source` and optional `help_request_id` (checkpoint version unchanged); `apply_plan_revision(source="review")` clears help, phase `step`, anchors on the last failed step; `orchestrator_prompts.redesign_for_review` + review state line / verdict sentence; new leaf `agents/plan_revision_limits.py` (timeout bounds + shared redesign validation, re-exported by `sub_task_budget`).
- `projects`: new `case_result_change_services.py` (propose / apply / discard), `case_result_text.py`, `case_review_redesign_services.py` (propose / discard / `rerun_with_redesigned_plan`), `case_review_redesign_store.py`, `case_result_page.py`; `run_reopen_services.reset_case_for_restart` / `clear_checked_and_sent` / `list_testrail_push_blockers` (moved from Resume, shared); `reset_test_run` clears result changes and review drafts; `case_help_policy.auto_fix_eligible` treats a REDESIGN re-run as from scratch; Steps card and TestRail wording for review redesigns; case page: Discuss result / Go to result chat, "Changed by" line, Result changes section.
- `machine_setup`: `case_result_brief.py`, `services/case_result_context.py`, `open_result_chat.py`, `result_chat_tools.py` (five tools), `plan_step_args.py` (moved from `help_chat_tools`), `result_card_decision.py`, `result_card_events.py`; role wiring in `setup_tools`, `setup_prompt` (RESULT CHAT RULES), `run_turn`; card payloads; thread header and list; `_result_change.html`, `_plan_redesign.html` cards and confirm dialogs in `chat.html`.

## Why
People wanted to question a finished case's verdict with the agent and fix it when it was wrong: either correct the recorded result, or have the agent redesign a misunderstood test and re-run it without losing the steps that passed.

## Behaviour and contract changes
- New POST URLs, all `@project_membership_required`, ids scoped to the project: `machine_setup:result_chat_open` (`result/<pivot_id>/`, field `text`), `result_change_apply` / `result_change_discard` (`result-changes/<id>/…`), `plan_redesign_rerun` (field `help_mode`) / `plan_redesign_discard` (`redesigns/<id>/…`).
- Eligible: Passed, Failed, Blocked, any to any. Blocked only by a person's Apply, reason `person_review`. Apply keeps `finished_at`, the checkpoint and the run status; writes "Result changed from X to Y by <name> on <date> after review with the agent." + the agent's summary; clears Checked/Sent (also after Sent). Refused while Send checked or the 018 push is live, or when the result fingerprint changed.
- Re-run with redesigned plan: Done or Cancelled run, Resume's gates, plan fingerprint re-checked; Earlier attempts row `redesign`; Checked/Sent cleared, log and screenshots kept; generation + 1; cleanup, restore of carried steps 1..C, then step C+1.
- The agent has full machine access in the result chat. Sends are refused while any run is Started (not Paused: user decision) or the machine is not READY.
- Message payload gains `result_change` and `plan_redesign` (cards update through `machine_setup.message` upserts; no new socket event). No controller protocol, Celery task or setting change.
- Deploy: `migrate` (`projects.0032`, `machine_setup.0006`), then restart Daphne/web and the execution workers. Rollback SQL (review revision rows, `person_review`, `redesign`) is in the plan's Settings / infrastructure section.

## Notes for future changes
- `grep "status = TestRunTestCaseStatus.BLOCKED"` keeps exactly one hit (`mark_blocked_after_help`); Apply assigns Blocked from a variable.
- Help code filters plan revisions by `help_request`, so it never sees review rows; the review redesign finder requires `source=review`.
- The review state line is used only while `next_step == resume_step` and no help is pending; the earlier failed digest is a retry note only until a new-plan step runs.
- Accepted risks: while another run is Paused for Need help, a result-chat helper may change that case's screen; the brief is a user-role transcript message (as in the help chat); names of people who changed results reach the LLM.
- Rejected review finding: a per-role spec replacing the `ThreadRole` if/elif chains (out of scope; separate plan).
- Verified with pre-commit (strict mypy), `manage.py check`, `makemigrations --check`, template compilation, throwaway-DB smoke scripts (proposals, Apply, re-run, gates, payloads, query counts), a DB-free orchestrator wording smoke, and three reviewer and verifier passes. Not yet exercised in a browser, against a live runner, or with the real model (plan Verification steps 3–10).
