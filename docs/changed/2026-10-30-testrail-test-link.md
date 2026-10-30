# Open in TestRail links to the run's test

Date: 2026-10-30
Plan: inline (small fix)

## What changed
- `projects/testrail_client.py`: `TestRailCase.test_id` carries the `get_tests` test id (None for `get_case`/`get_cases`).
- `projects/models.py` / migration `0049`: `TestCase.testrail_source_test_id`, written with `testrail_source_run_id` by run-link imports only (`testrail_services._upsert_batch`, batch items now `_UpsertItem`).
- `projects/services.py` (`create_test_run_with_cases`, `add_cases_to_test_run`, rerun): run cases snapshot the test id into `TestRunTestCase.testrail_source_test_id` with the source run; `build_testrail_result_url` already links `index.php?/tests/view/<id>` from it.
- `projects/testrail_test_id_backfill_services.py` + command `backfill_testrail_test_ids [--project-id]`: fills missing test ids on existing test cases and run cases, one `get_tests` call per source run (`TestRailClient.get_run_test_ids`).

## Why
"Open in TestRail" on a run case imported from a TestRail run opened the whole run; it should open that case's test in the run.

## Behaviour and contract changes
- Migration `0049_testcase_testrail_source_test_id` (nullable column, no data loss).
- New management command `backfill_testrail_test_ids`; run once after deploying (`docker compose exec -T api python manage.py backfill_testrail_test_ids`). Safe to re-run: it only fills empty ids.

## Notes for future changes
- `TestRunTestCase.testrail_source_test_id` means "the test in the source run", whether it came from the import snapshot or a send; both give the same id for the same run and case.
- Runs that no longer exist in TestRail (400/404) are reported and keep the run link.
- Link priority: source test only while a result is sent there, else the pushed run's test, else the source test. After Reset/Re-run/Uncheck clears the send, a case that was also pushed links the pushed test.
