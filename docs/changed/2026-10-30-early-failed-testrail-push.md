# Send failed cases to TestRail before the run finishes, also from the Failures page

Date: 2026-10-30
Plan: docs/plan/feat-early-failed-testrail-push.md

## What changed
- `projects/testrail_case_send_services.py` (new): `start_case_send` checks the selected Failed cases (evidence frozen, Checked by the sender; already checked ones keep their reviewer, skip reason cleared) and sends exactly those to their source TestRail run, in any run status except Draft. Also `is_case_sendable`, `is_case_missing_source`, `list_case_sendable_pivot_ids`, `format_case_send_message`.
- `projects/testrail_checked_push_services.py`: public `take_checked_push_slot` (run statuses as a parameter), `fail_stale_checked_push`, `dispatch_checked_push`; `plan_checked_push` / `push_checked_results` take `pivot_ids`. `start_checked_push` is unchanged (finished runs only).
- `projects/run_case_check_services.check_locked_pivots`: the shared check step (only writer of evidence flags).
- `projects/tasks.push_checked_results_to_testrail`: optional 4th argument `pivot_ids` (None = whole run, as before).
- `projects/run_reopen_services.list_wake_blockers`: the reopen gates without the TestRail push gates; used by `queue_pending_cases`, the help and person step panels, help resolution and the run pause panel.
- Run page: "Send to TestRail (N)" in the bulk bar in every run status, a per-row Send, shared confirm `templates/projects/includes/_testrail_case_send_dialog.html`; `runCaseChecks` takes a 4th id list. Case page: Send button, or "Can't send: imported without a TestRail run link".
- Failures page: per-row Send, the bulk Send now checks and sends only the selected failed cases per run (also runs still going), "Can't send: no TestRail run link" on rows without a source run; `testrail_configured` SQL annotation on the list.
- Status line reads "Sending results to TestRail…".

## Why
Failures should reach TestRail as soon as they happen, not after the whole Punk Hazard run finishes.

## Behaviour and contract changes
- New POST URL `projects:test_run_cases_send_testrail` → `/projects/<id>/test-runs/<run_id>/cases/send-testrail/` (`pivot_ids`, `next`), `@project_membership_required` (read-only gets 403).
- `dashboard:failed_cases_bulk` `action=send` now means "check and send the selected failed cases" instead of "send every checked case of each finished run". New context keys `sendable_ids`, `no_source_ids` on `/failures/`.
- Only Failed cases with a source TestRail run (run-link imports) and no result yet. Suite imports keep the post-run Push to TestRail.
- One send per run at a time: a second one is refused ("already being sent"). "Send checked to TestRail" stays finished-only and counts early-sent cases as already sent.
- A verdict replaced later (Resume / Re-run / applied result change / Reset) clears Sent, and the next send adds a new TestRail result (existing behaviour).
- No migration, no new setting. Deploy: restart the `upload` worker before or with web (the task has a new argument; an old worker would leave the slot stuck until it goes stale, about 62 minutes).

## Notes for future changes
- `start_case_send` must not run inside an outer transaction (it dispatches after its own commit; it raises `RuntimeError` otherwise).
- A wake path must only touch NEED_HELP / AWAITING_PERSON / CREATED cases; anything that changes a finished verdict uses `list_reopen_blockers`.
- An interrupted early send (result posted, screenshots incomplete) is finished only by the run-level "Finish sending" after the run ends; a case send never re-selects a case with a result id.
- The case page and Failures page show Sent after a reload only (no live state); the run page reloads when the send settles.
- Not exercised against a database or a live TestRail (Postgres was not running): checked with strict mypy, pre-commit, `manage.py check`, `makemigrations --check`, template loading and three reviewer/verifier passes.
