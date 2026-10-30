# TestRail: full result comments, no links back, Open in TestRail goes to results

Date: 2026-10-30
Plan: inline (small change; decisions taken with the user in chat)

## What changed
- `projects/testrail_mapping.py`: result comments are never shortened (no result text, step list or per-field caps) and no longer end with a "Details:" link to Punk Hazard. `LEGACY_COMMENT_HASH` added.
- `projects/testrail_results_services.py`: the suite push ("Push to TestRail") creates TestRail runs whose description has no Punk Hazard link; `links_base_url` is gone from the whole suite push chain. `_is_changed` re-sends a "legacy" row only on a status change. `build_testrail_result_url` replaces `build_testrail_case_url`.
- `projects/testrail_checked_push_services.py`, `projects/testrail_case_send_services.py`, `dashboard/services/failed_case_bulk.py`, `dashboard/failure_views.py`, `projects/testrail_views.py`, `projects/views.py`: `links_base_url` / `build_case_detail_url` removed.
- `projects/testrail_client.py`: `TestRailResult.test_id` parsed from add-result responses.
- `projects/models.py`: `TestRunTestCase.testrail_test_id` (suite push run; cleared by `_reset_pivot_tracking`) and `testrail_source_test_id` (source run; kept across re-sends), migration `0044_testrail_test_ids`.
- Run case page: "Open in TestRail" (`testrail_result_url`) opens the TestRail test the results went to (`tests/view/<id>`, source run first), else the run it was sent to (sent before test ids were stored), else the source run, else the case.

## Why
The user wants full result text in TestRail, no Punk Hazard links there, and the case page link to land on the results, not the case definition.

## Behaviour and contract changes
- Migration `projects.0044_testrail_test_ids`: two nullable fields plus a data step that sets every non-empty `testrail_pushed_comment_hash` to `"legacy"`, so results pushed before this change are not all re-sent because the comment format changed. A legacy row is re-sent only when its status changes; after that it has a real hash again.
- Celery tasks `push_test_run_results` and `push_checked_results_to_testrail` keep their second positional argument as unused `_links_base_url`; new messages send `""`.
- Template context: `testrail_case_url` renamed `testrail_result_url` on `projects/test_run_case_detail.html`.

## Notes for future changes
- A legacy row whose result text changes without a status change is not re-sent (accepted trade-off). Rolling the code back leaves `"legacy"` in the hash column, so old code would re-push each such row once.
- TestRail runs created before this change keep their old description with the link; `update_run` does not rewrite it.
- `TestRailResult.test_id` is parsed leniently (`_positive_id`): a bad value only loses the link, never fails a push after TestRail stored the results.
- The early checked/case send dedupes on `testrail_source_result_id`, so the legacy marking never affected it.
- Plans `feat-case-step-progress.md`, `feat-testrail-checked-results.md` and `feat-early-failed-testrail-push.md` still describe the old caps, Details link and `links_base_url`; this note supersedes them.
