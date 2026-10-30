# Open in TestRail links to the run's test

Date: 2026-10-30
Plan: inline (small fix)

## What changed
- `projects/testrail_client.py`: `TestRailCase.test_id` carries the `get_tests` test id (None for `get_case`/`get_cases`).
- `projects/models.py` / migration `0049`: `TestCase.testrail_source_test_id`, written with `testrail_source_run_id` by run-link imports only (`testrail_services._upsert_batch`, batch items now `_UpsertItem`).
- `projects/testrail_results_services.build_testrail_result_url`: after a sent test id, links `index.php?/tests/view/<id>` from the case's import test when the case's `testrail_source_run_id` still equals the run case's snapshot.

## Why
"Open in TestRail" on a run case imported from a TestRail run opened the whole run; it should open that case's test in the run.

## Behaviour and contract changes
- Migration `0049_testcase_testrail_source_test_id` (nullable column, no data loss).

## Notes for future changes
- Cases imported before this change have no test id and still link the run until the run is re-imported.
- A case re-imported from another run no longer matches older run cases' snapshot run, so those fall back to the run page instead of a wrong test.
- The import test id lives on `TestCase`, not the pivot: `TestRunTestCase.testrail_source_test_id` stays the send path's field.
