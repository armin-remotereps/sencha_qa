# Refresh a single test case from TestRail

Date: 2026-10-30
Plan: docs/plan/feat-testrail-case-refresh.md

## What changed
- `projects/testrail_client.py`: `get_case`, `TestRailCase.suite_id`, per-client `max_rate_limit_retries`.
- `projects/testrail_services.py`: `_apply_data` / `_UPSERT_FIELDS` are now public `apply_testrail_case_data` / `TESTRAIL_CASE_FIELDS` (imports and refresh share one write path); `build_testrail_client(max_rate_limit_retries=)`; `get_testrail_field_mapper` caches case types and priorities per project for 1 h (refetched when the case uses an unknown id; dropped on settings save/clear).
- `projects/testrail_case_refresh_services.py` (new): `refresh_test_case_from_testrail`, `can_refresh_from_testrail`, `format_refresh_message`, `TestCaseRefreshOutcome`.
- `projects/exceptions.py`: `TestCaseRefreshError` + `TestCaseNoTestRailIdError`, `TestRailCaseNotFoundError`, `TestCaseRefreshBusyError`, `TestCaseRefreshThrottledError`.
- `projects/testrail_views.test_case_refresh_testrail`, `forms.TestCaseRefreshForm`.
- UI: "Refresh from TestRail" in the Test Cases row menu (rows with a TestRail id, project with TestRail settings) and in the run case page header; one shared confirm `templates/projects/includes/_test_case_refresh_dialog.html`.

## Why
Picking up a case edited in TestRail needed a re-import of the whole suite or run.

## Behaviour and contract changes
- New POST URL `projects:test_case_refresh_testrail` → `/projects/<id>/test-cases/<test_case_id>/refresh-testrail/` (`next`, same-host), `@project_membership_required` (read-only 403), case scoped to the project (404).
- Synchronous: one `get_case` call (3 on a cold vocabulary cache), no sleeping on 429; 30 refreshes per minute per project.
- Overwrites the nine text fields (same mapping as imports, incl. "Test Case (Steps)") on every case of the project with the same stored `testrail_id`; order fields, `testrail_id` and the source run are kept. Unchanged rows are not written.
- Refused while any of those cases is unfinished (created / in progress / need help / awaiting person) in a Running or Needs-help run. Draft and finished runs show the new text (no per-run snapshot, as with imports and Edit).
- Messages: "Refreshed C123 from TestRail: title and steps changed.", "C123 is already up to date with TestRail.", a suite-moved hint, and one message per error (no settings, no id, deleted case, 401, 403, 429, unreachable, other).
- No migration, setting, task or protocol change.

## Notes for future changes
- The busy re-check under the case row locks only narrows the race with run Start/Resume (they lock runs, not cases).
- A Resume after a refresh continues the earlier plan; Re-run plans from the new text (said in the dialog).
- A type/priority renamed in TestRail with the same id keeps the old name for up to an hour.
- Follow-up: bulk "Refresh selected" (Celery `upload` task).
- Not exercised against a live TestRail or a database: checked with pre-commit (strict mypy all configs), `manage.py check`, template loading, message formatting smokes and two reviewer/verifier rounds.
