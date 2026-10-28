# Case duration counts every execution segment; average case time per run

Date: 2026-10-28
Plan: docs/plan/fix-case-duration.md

## What changed
- `TestRunTestCase.segment_started_at` and `active_duration` (migration `projects.0036`): the start of the current worker's segment and the sum of the closed segments (null = unknown).
- `projects/case_duration_services.py`:
  - `open_case_segment` and `close_case_segment` (one idempotent conditional UPDATE);
  - `get_run_case_duration_stats` and `get_case_wall_clock_with_waiting`;
  - `AVERAGE_CASE_STATUSES`.
- `projects/services.py`:
  - a segment opens in `_mark_pivot_in_progress` and closes in `_finalize_pivot` (at the verdict, so teardown is not counted), `_pause_run_for_help` and `_mark_pivot_cancelled`;
  - Start, reset and Re-run clear both fields;
  - `_annotate_run_counts` adds `avg_case_duration`.
- `case_step_progress`: "current activity" uses `segment_started_at`.
- TestRail `elapsed` (`format_testrail_elapsed`) is built from `active_duration`.
- Run page:
  - new "Avg per case" stat card (Passed + Failed cases with timing);
  - the case Duration column shows `TestRunTestCase.shown_duration` (finished statuses only).
- Runs tab: "Avg per case" column.
- Case page: active time, plus "(… incl. waiting for help)" when the wall clock is more than 1 minute longer.
- `auto_tester/durations.format_duration` and the `duration` template filter: one formatter ("14m 32s", "1h 02m", "< 1s", "—").
- Migration `projects.0037`: backfills only old cases that provably ran in one segment.

## Why
`_mark_pivot_in_progress` reset `started_at` on every pick-up. A case that paused for help and continued (or was resumed, auto-fixed or redelivered) showed only its last segment: 2 minutes of a 15-minute case.

## Behaviour and contract changes
- Migrations `projects.0036` (two nullable columns) and `0037` (data, reverse no-op).
- `started_at` now means "first start since the last from-scratch start". `finished_at` is unchanged.
- No new URL, setting or socket event.

## Notes for future changes
- The backfill leaves null ("—", excluded from averages) every old case with a help request that has no later Re-run snapshot, and every old case with a resume/redesign snapshot. The old `started_at` of these rows is not trustworthy.
- Known gap: when Celery redelivers a case after a worker is killed, the dead worker's open segment is lost. It is logged as a warning in `open_case_segment`.
- The run's own Duration is still wall clock from Start to Done (`projects/run_durations.run_duration`). It is used by the run-finished push and the digest.
- Not exercised against a migrated database or a live run. Checked with strict mypy, `manage.py check`, `makemigrations --check`, SQL inspection of 0037, and three reviewer and verifier passes.
