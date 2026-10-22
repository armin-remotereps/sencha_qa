# Mark run cases as Checked and send them to their source TestRail run

Date: 2026-10-22
Plan: docs/plan/feat-testrail-checked-results.md

## What changed
- `projects/models.py`, migration `0021_checked_results_source_push` (additive, no backfill):
  - `TestCase.testrail_source_run_id`;
  - on `TestRunTestCase`: the source-run snapshot `testrail_source_run_id`, `checked_at`/`checked_by`, `testrail_source_result_id`/`_sent_at`/`_attachment_count`, and `testrail_source_skip_reason` (`CheckedPushSkipReason`);
  - `TestRun.testrail_source_push_*` status fields, separate from the 018 push fields.
- `projects/testrail_services.py`, `testrail_run_import_services.py`: run-link imports write the case's source run (`upsert_test_cases_from_testrail(source_run_id=...)`). Suite imports leave it alone.
- `projects/services.py`:
  - `create_test_run_with_cases`, `add_cases_to_test_run` and `rerun_cases` copy the source run onto each pivot;
  - `reset_test_run` locks the run, refuses while a send is live, and clears the Checked/Sent/skip state.
- `projects/run_case_check_services.py` (new): `check_run_cases` / `uncheck_run_cases` (one UPDATE each; uncheck is refused while a send is live) and `format_check_message`.
- `projects/testrail_checked_push_services.py` (new):
  - `get_checked_push_panel` (2 queries);
  - `start_checked_push`, which takes the slot with a conditional UPDATE and turns a stale slot into FAILED first;
  - `plan_checked_push`, `push_checked_results`, `get_checked_push_run` (slot-token check), `get_source_run_url`.
- `projects/checked_push_limits.py` (new): the task time limits, plus `is_checked_push_live` / `is_checked_push_stale`.
- `projects/testrail_client.py`:
  - `Content-Type` is no longer a client default; it was breaking multipart uploads;
  - new `add_attachment_to_result` (bytes, so a 429 retry resends them) and `get_run_case_ids`.
- `projects/testrail_mapping.py`: `build_checked_result_comment`, `format_checked_push_summary`, `format_checked_push_confirm_body`, `screenshot_attachment_filename`. `testrail_results_services._pivot_result_text` is renamed to `pivot_result_text`.
- `projects/tasks.py`: `push_checked_results_to_testrail(test_run_id, links_base_url, slot_token)` on the `upload` queue, 3600/3660s.
- Views/URLs:
  - `test_run_cases_check` / `test_run_cases_uncheck` (`projects/views.py`), with `RunCaseSelectionForm`;
  - `testrail_send_checked` (`projects/testrail_views.py`).
- `projects/consumers.py`: `TestRunConsumer` adds the `testrail_source_push` message and `current_state.testrail_source_push_status`.
- Templates:
  - run page: checkbox column, Checked column, a Check/Uncheck bulk bar (`runCaseChecks`), and the includes `_testrail_checked_push_actions.html` / `_testrail_checked_push_status.html`;
  - case page: Mark as checked / Uncheck / Sent;
  - `_checked_skip_reason.html`.
- `static/js/punk-hazard.js`: `bulkSelect` is now built from a `bulkSelectState` factory; new `runCaseChecks` component.
- `auto_tester/settings.py`: task route; `CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 4 * 3600}`.

## Why
A reviewer marks the AI verdicts they have checked. Only those results go back to the TestRail run the cases were imported from, with every screenshot as evidence, instead of to a new TestRail run.

## Behaviour and contract changes
- New POST URLs, all with `@project_membership_required`:
  - `projects:test_run_cases_check` → `/projects/<id>/test-runs/<run_id>/cases/check/`
  - `projects:test_run_cases_uncheck` → `.../cases/uncheck/` (fields `pivot_ids`, `next`)
  - `projects:testrail_send_checked` → `.../testrail/send-checked/`
- Rules:
  - Checking needs a Passed/Failed/Blocked verdict.
  - Uncheck is allowed until the case is sent. It is also refused while a send is live.
  - Sending needs a finished run.
  - Reset clears Checked/Sent and is refused while a send is live.
- Each result goes to the case's source run, at most once per execution, with the 018 status mapping. The comment says "Checked by <name> on <date>" and "N screenshots attached".
- All screenshots are attached in `(created_at, id)` order, except `controller_omniparser` overlays.
- Cases that can't be sent are recorded on the pivot and leave the ready count until unchecked or re-checked:
  - the case is not in the source run;
  - the source run is closed or missing;
  - the case has no TestRail id.
- WebSocket: group event `test_run.testrail_source_push` → client `{"type": "testrail_source_push", "push_status"}`; `current_state.testrail_source_push_status`.
- Deploy:
  - run `migrate`;
  - restart the `upload` worker (new task route) and all workers (broker transport options).
- Upgrade: no backfill. Runs created before deploy show an explanation instead of the button. Cases get a source run the next time their TestRail run link is imported; create the run after that.
- The 018 "Push to TestRail" is unchanged.

## Notes for future changes
- Resume safety depends on the fixed screenshot order and on reset being blocked while a send is live. Keep both if screenshots ever change.
- The slot token stops a redelivered or old task from acting after a takeover. A worker killed between `add_results_for_cases` returning and the `bulk_update` can still duplicate that batch (accepted).
- The visibility timeout must stay above every task's hard limit. That includes `execute_test_run_case`, whose limit grows with `CONTROLLER_RECONNECT_GRACE_SECONDS`.
- A long send holds one `upload` worker slot (up to an hour), which delays imports on that worker.
- Not verified against a live TestRail instance:
  - the `add_attachment_to_result` field name and response shape;
  - the full send flow (plan Verification steps 2–12).
  - Checked so far with mypy, pre-commit, `manage.py check` and smoke scripts on a throwaway test DB.
