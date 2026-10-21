# Run test cases in TestRail order

Date: 2026-10-22
Plan: docs/plan/feat-testrail-case-order.md

## What changed
- `TestCase`: new `testrail_suite_id` and `testrail_position` (0-based rank in TestRail suite order). Only imports write them; they are not in `TestCaseData`, so manual edits keep them.
- `projects/testrail_client.py`: new `TestRailSection` and `get_sections`. `TestRailCase` gains `section_id` and `display_order`, parsed leniently (bad value means no order). `iter_cases` accepts `suite_id=None` for single-suite mode.
- `projects/testrail_case_order.py` (new, pure): `rank_testrail_cases` walks sections depth-first, siblings by `display_order`, then orders cases by their `display_order`, then by case id. `append_unranked` handles the fallback.
- `projects/testrail_services.py`: new `rank_suite_cases` (ranks cases already fetched) and `fetch_testrail_case_ranks` (streams the suite). `upsert_test_cases_from_testrail` takes `suite_id` and `positions` and writes the order fields.
- Suite import (`tasks._run_testrail_import`) ranks the cases it fetched. Run-link import (`import_testrail_run_cases`) fetches the whole suite to rank the run's cases:
  - cases missing from the suite go last, in `get_tests` order;
  - with no suite id, a failed order fetch falls back to `get_tests` order.
- `projects/run_case_order.py` (new): `RUN_CASE_ORDERING` (suite, position, nulls last, then `test_case_id`) and `PICKER_CASE_ORDERING`.
- `projects/services.py` uses the new ordering:
  - `start_test_run`: execution chain order.
  - `get_case_position`: now one query.
  - `list_test_run_cases`: failures first, then TestRail order.
  - Failed and blocked title lists, and `get_run_case_picker`.
  - `_clone_test_case` copies the order fields.
- `testrail_results_services.plan_testrail_push`: uses the same ordering.

## Why
Runs executed their cases in an undefined database order. They must follow TestRail's order. TestRail's list endpoints return records sorted by ID, so the UI order is rebuilt from the section tree and each case's `display_order`.

## Behaviour and contract changes
- Migration `0020_testcase_testrail_order`: two nullable `AddField`s, additive only.
- Ordering only:
  - execution, "Case N of M", the run results page (within the failures-first groups), the wizard case picker and the push plan now follow TestRail order;
  - cases without a position (manual, XML-era, not yet re-imported) come last.
- Each import also reads every page of `get_sections`. A run-link import additionally reads every page of the suite's `get_cases`.
- Restart the `upload` and `execution` workers on deploy.

## Notes for future changes
- Existing cases have no position until their suite or run is re-imported once.
- Run order is computed live from the cases, not stored per run. A finished run's displayed order can shift after a re-import reorders TestRail. Execution order is fixed when the run starts.
- Case `display_order` is undocumented in the TestRail API. If an instance omits it, cases stay grouped by section and fall back to case id. Compare one real suite with the TestRail UI.
- Runs mixing several suites order them by suite id, not by TestRail's suite order.
