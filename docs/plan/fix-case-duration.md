# fix: Case duration counts only the last execution segment; add average case time per run

Status: In progress
Created: 2026-10-28
Request: A run case's displayed duration is far too short: a case that took about
15 minutes shows about 2 minutes. Find out why, make the case duration correct,
and show the average time a case took for each run (on the run page and on the
run lists).

## Context
- `TestRunTestCase.started_at` / `finished_at` (`projects/models.py:456-457`) are the
  only timing fields on a case. Every display and the TestRail `elapsed` value is
  `finished_at - started_at`:
  - run page case table: `templates/projects/test_run_detail.html:447`
    (`pivot.started_at|timesince:pivot.finished_at`);
  - case page header: `templates/projects/test_run_case_detail.html:117-118`;
  - TestRail: `format_testrail_elapsed(pivot.started_at, pivot.finished_at)` in
    `projects/testrail_results_services.py:156` and
    `projects/testrail_checked_push_services.py:612` (fields loaded via `.only(...)`
    at `:632-633`); formatter in `projects/testrail_mapping.py:167`.
- One execution of a case is one `execute_test_run_test_case` call
  (`projects/services.py:2876`), queued by `queue_test_run_cases` (`:2457`). It calls
  `_mark_pivot_in_progress` (`:3109`), which sets `started_at = now()`
  unconditionally (`:3111`).
- The same case is executed again, keeping its checkpoint, by:
  - Continue after Need help (`projects/case_help_services.continue_after_help`,
    `:299`, via `_set_continue_pending` `:733`, then `queue_pending_cases`);
  - Night auto-fix "fixed" (`finish_case_auto_fix` `:514`), same queue path;
  - Resume of a Blocked/Cancelled case (`projects/run_resume_services.py`,
    `reset_case_for_restart` in `projects/run_reopen_services.py:127`, whose
    docstring already says "``started_at`` is set again when the worker picks the
    case up");
  - Result-chat "Re-run with redesigned plan" (`projects/case_review_redesign_services.py`,
    Resume path).
  - A Celery redelivery (`CELERY_TASK_ACKS_LATE` + `REJECT_ON_WORKER_LOST`,
    `auto_tester/settings.py:161-169`) after a worker is killed.
- Segment ends that write `finished_at`: `_finalize_pivot` (`:3214`),
  `_mark_pivot_cancelled` (`:3356`, also used by `abort_test_run` for In progress,
  Need help and Created cases), `mark_failed_after_help` / `mark_blocked_after_help`
  (`case_help_services.py:372,411`, `finished_at = now` while the case was paused).
  `_pause_run_for_help` (`:3307-3324`) sets `finished_at = None`.
- Re-run in place (`projects/run_rerun_services.py:140,189`), Start/reset
  (`projects/services.py:2335`) clear `started_at` and `finished_at`: a from-scratch
  execution.
- The step progress code relies on `started_at` meaning "the current worker's start":
  `projects/case_step_progress._current_activity` (`:325-334`) shows "Starting" while
  the checkpoint's `phase_started_at` is older than `started_at` (a resumed case
  before its first new checkpoint). Callers pass `pivot.started_at`:
  `pivot_step_payload` (`:510`), `case_step_view` (`:518`),
  `_broadcast_case_steps` (`services.py:2775`), `pivot_step_breakdown`
  (`testrail_results_services.py:126`).
- Per-step durations already exist in the checkpoint
  (`SubTaskAttempt.duration_seconds`, `agents/types.py:202`), shown on the Steps card.
  They time sub-agents only (not planning, evaluation, verdict, waits).
- Run lists: `_annotate_run_counts` (`projects/services.py:486`) annotates
  `case_count/success_count/failed_count/blocked_count` with one join on
  `pivot_entries`; shared by `list_test_runs_for_project` (Runs tab,
  `templates/projects/includes/_test_run_table.html`) and `get_project_overview`.
  Run page summary: `get_test_run_summary` (`:2487`, one aggregate, also broadcast
  over WebSocket as `dict[str, int]`). The run page reloads itself 1.5 s after the
  run turns Done/Cancelled (`test_run_detail.html:841`), so server-rendered values
  catch up.
- Dev database has 1 run, 1 case, never executed: no data evidence available
  locally (read-only query, 2026-10-28).

## Root cause
**Confirmed (code path):** `_mark_pivot_in_progress` (`projects/services.py:3109-3112`)
overwrites `pivot.started_at` with `timezone.now()` every time a worker picks the
case up, and every display computes `finished_at - started_at`. A case that pauses
for help (Need help → Continue, or Night auto-fix → fixed), or is resumed, is
executed in several segments; only the **last segment** survives. A 15-minute case
that asked for help at minute 13 and finished 2 minutes after Continue shows
"2 minutes". The regression arrived with resumable cases (Resume, then Need help /
Continue, commit `0846124`): before them a case ran exactly once per Start.

**Contributing (confirmed, small):**
- `finished_at` is written at the verdict (`_finalize_pivot`), before
  `_safe_teardown` (up to `CASE_TEARDOWN_TIMEOUT_SECONDS`, default 120 s) and
  `_safe_cleanup` run in the `finally`. Teardown time is never counted.
- `mark_failed_after_help` / `mark_blocked_after_help` set `finished_at = now`, so
  for those cases the shown span includes the wait for a person (the opposite
  error), but still starts at the last segment.

**Suspected (cannot confirm without production data):** a worker killed mid-case
(deploy, OOM) is redelivered and re-executed; that also resets `started_at`. It is
indistinguishable from a Continue in the data.

No timezone issue: all writes use `timezone.now()` and `timesince` on aware values.

## Goals
- A case's "time taken" covers every execution segment since it last started from
  scratch (Start or Re-run), not only the last one.
- One definition used everywhere: run page table, case page, TestRail `elapsed`.
- Average time taken per case for each run, on the run page (stat card) and in the
  Runs tab table, computed by a DB aggregate (no N+1, no extra query on the list).
- Old rows whose true duration cannot be known show "—" (unknown), never a wrong
  number.

## Non-goals
- Changing the run's own Duration (`TestRun.started_at → finished_at`, wall clock
  incl. Need help waits and, after a Re-run in place, the gap since the original
  Start). Noted in Open questions; untouched here.
- Live ticking durations while a case runs.
- Timing per TestRail step, or changing per-step durations on the Steps card.
- WebSocket payload changes (the run page reload covers finishing).

## Design

### Definition (recommended)
**Time taken = active execution time**: the sum of the case's execution segments
since it last started from scratch. A segment runs from the moment a worker picks
the case up (`_mark_pivot_in_progress`) to the moment that worker stops owning it:
verdict, pause for help, or cancel. Time spent waiting for a person, in a Night
auto-fix, or Blocked before a Resume is excluded. Wall clock (first start → final
finish) stays derivable from `started_at` / `finished_at` and is shown as a
secondary detail on the case page only.

Why: the user's 15-minute figure is "how long the machine worked on it". Wall clock
for an overnight Night-mode case could be hours and would make the average useless
for estimating how long a run will take.

### Data model
`TestRunTestCase` (`projects/models.py`), two new nullable columns, migration
`projects.0035_case_active_duration` (number after the parallel plan's 0034, see
Risks):
- `segment_started_at = DateTimeField(null=True, blank=True)`: when the current
  worker picked the case up; null when no worker owns it.
- `active_duration = DurationField(null=True, blank=True)`: sum of the case's
  closed segments. Null means "unknown" (legacy row not backfilled, or never run).
  Postgres `interval`; `Avg`/`Sum` on it return `timedelta` (Django 5.2 docs: Avg
  return type is "same as input field" for non-int input).

Field semantics after the fix:
- `started_at`: first pick-up since the last from-scratch start; set only while it
  is null. Start, reset and Re-run already clear it; Resume and Continue keep it.
- `finished_at`: unchanged (verdict / cancel / person's mark time). The parallel
  failures plan indexes and sorts on it, so its meaning must not move.

Optional follow-up data migration `projects.0036_backfill_case_active_duration`
(RunPython, single set-based `UPDATE`, see Open question Q2): for finished cases
whose current execution had exactly one segment, set
`active_duration = finished_at - started_at`. One segment means: no
`CaseHelpRequest` of the case newer than its latest `rerun` `TestRunCaseResume`
snapshot (or any at all, if never re-run), and no `TestRunCaseResume` of kind
`resume`/`redesign` newer than that `rerun` snapshot (or any, if never re-run).
`started_at` is not used to scope help requests: before this change every pickup
reset it, also the one after a help Continue (which writes no snapshot), so a
help request older than `started_at` still marks an earlier segment. Skip `mark_failed/blocked_after_help` outcomes
(they have a help request, so the rule already skips them). Everything else stays
null = unknown. Reverse = no-op.

### Services
New module `projects/case_duration_services.py` (one use case: tracking and
reporting case execution time). Names, not bodies:
- `open_case_segment(pivot, now) -> None`: sets `segment_started_at = now`; sets
  `started_at = now` only if null; sets `active_duration = timedelta(0)` only when
  `started_at` was null (a fresh execution). A legacy row (started before deploy,
  `active_duration` null, `started_at` set) stays unknown until it is re-run, so it
  never shows a partial number. A non-null `segment_started_at` at open (redelivered
  task, killed worker) is logged as a warning and overwritten: that segment is lost.
- `close_case_segment(pivot_id, now) -> None`: one conditional UPDATE
  `WHERE pk=… AND segment_started_at IS NOT NULL`: adds `now - segment_started_at`
  to `active_duration` (only when not null) and clears `segment_started_at`.
  Idempotent: a second close (abort racing the worker's own cancel) adds nothing.
  Runs on the DB row, so the stale in-memory pivots `abort_test_run` iterates are
  harmless. Callers mirror the new values on their in-memory pivot only if they
  render it afterwards (none of the broadcasts carry duration).
- `get_run_case_duration_stats(test_run) -> RunCaseDurationStats` (frozen
  dataclass: `average: timedelta | None`, `timed_count: int`, `untimed_count: int`):
  one `aggregate()` over `pivot_entries` filtered to counted statuses.
- `annotate_avg_case_duration(qs: QuerySet[TestRun]) -> QuerySet[TestRun]`, or the
  same `Avg(...)` added directly inside `_annotate_run_counts` (preferred: the
  existing join on `pivot_entries` is reused; a single multi-valued join means no
  fan-out). Annotation name `avg_case_duration`.

Counted statuses for the average: Passed and Failed with non-null
`active_duration` (Q3). `untimed_count` = Passed/Failed cases with null duration,
shown as "n cases without timing" when non-zero.

Wiring into existing code (`projects/services.py`):
- `_mark_pivot_in_progress`: call `open_case_segment` instead of writing
  `started_at`; save `segment_started_at`, `started_at`, `active_duration` with the
  status in the same `save(update_fields=…)`.
- `_finalize_pivot`: `close_case_segment` with the same `now` as `finished_at`.
- `_pause_run_for_help`: `close_case_segment` inside its transaction, after the
  generation/status checks pass (it already holds the run lock).
- `_mark_pivot_cancelled`: `close_case_segment` (no-op for Need help / Created
  cases, whose segment is already closed or never opened).
- `projects/services.py:2335` (reset), `run_rerun_services.py:140/189`: also clear
  `segment_started_at` and `active_duration` (set null); add both to the
  `update(...)` / `_reset_in_memory`.
- `case_help_services.mark_failed_after_help` / `mark_blocked_after_help`: no
  timing change (segment closed at pause); `finished_at` keeps the person's time.
- Step progress: `case_step_progress._current_activity` and its callers switch from
  `pivot.started_at` to `pivot.segment_started_at` (the "current worker" meaning it
  actually needs): `pivot_step_payload`, `case_step_view`, `_broadcast_case_steps`,
  `pivot_step_breakdown`. Parameter renamed `segment_started_at`.
- TestRail: `format_testrail_elapsed` takes `timedelta | None` instead of two
  datetimes; both push services pass `pivot.active_duration`; add
  `active_duration` to the `.only(...)` list in
  `testrail_checked_push_services.py:632`. An unknown duration sends no `elapsed`
  (as today for "" ).
- Teardown (Q4): with the recommended assumption, not counted. If counted, the
  `finally` of `execute_test_run_test_case` would instead close the segment after
  `_safe_teardown`, and `_finalize_pivot` would not.

Pure formatting: new `projects/duration_format.py` with
`format_duration(value: timedelta | None) -> str` ("14m 32s", "1h 02m", "—" for
None, "< 1s" for zero). `format_testrail_elapsed` reuses the h/m/s split (TestRail
needs its own "1h 2m 3s" format, so keep it as a thin wrapper). A template filter
`duration` in `projects/templatetags/project_extras.py` calls `format_duration`
(no logic in the filter). This replaces `timesince` for case durations, which
rounds to the largest units ("15 minutes") and hides seconds on short cases.

### Pages and forms
No new URL, form or permission. Existing views keep `@project_membership_required`.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_detail` | GET | `@project_membership_required` | — | `test_run_detail.html`: new key `case_duration` (`RunCaseDurationStats`); case table Duration column = `pivot.active_duration|duration`; new stat card "Avg per case" (`case_duration.average|duration`, `title` "Average over N passed/failed cases", plus "n without timing" note) | — |
| `projects:test_runs` | GET | `@project_membership_required` | — | `_test_run_table.html`: new column "Avg per case" from `run.avg_case_duration|duration` (annotation; context contract comment updated) | — |
| `projects:test_run_case_detail` | GET | `@project_membership_required` | — | header: "started <first start> · <active_duration|duration>"; when `finished_at - started_at` exceeds it by more than a minute, append "(<wall clock> incl. waiting for help)" computed in a service helper, not the template | — |

The stat card row already hides Duration while Need help is shown; the new card is
always visible. Mid-run the card shows the average of the cases finished so far
(server-rendered at page load; the reload on Done/Cancelled refreshes it).
All output is numbers formatted server-side: no `|safe`, no XSS surface.

### Background work and WebSockets
- No new task, queue, consumer or event. Segment open/close happen inside the
  existing `execute_test_run_case` task (`execution` queue) and the help/abort
  services.
- No controller protocol change; old controllers unaffected.

### Settings / infrastructure
None. One schema migration (two nullable columns, no table rewrite on Postgres 17),
one optional data migration.

### Data flow
1. Start / Re-run: case Created, `started_at`, `segment_started_at`,
   `active_duration` null.
2. Worker picks it up: `open_case_segment` → `started_at = segment_started_at = t0`,
   `active_duration = 0`.
3. Step fails for an environmental reason: `_pause_run_for_help` closes the segment
   (`active_duration += t1 - t0`, `segment_started_at = null`).
4. Person presses Continue an hour later; worker picks it up: `open_case_segment`
   → `segment_started_at = t2`; `started_at` and `active_duration` kept.
5. Verdict: `_finalize_pivot` sets `finished_at = t3` and closes the segment
   (`active_duration += t3 - t2`).
6. Run page: table shows `active_duration`; "Avg per case" = `Avg(active_duration)`
   over Passed/Failed; Runs tab gets the same value as an annotation on the
   existing counts query.

## Alternatives considered
- **Stop resetting `started_at` and keep `finished_at - started_at`** (one-line
  fix): gives wall clock, including Need help waits and overnight auto-fix time;
  breaks `_current_activity` ("Starting" detection); and still wrong for
  Mark failed/blocked. Rejected unless the user picks wall clock (Q1); even then a
  separate segment field would be needed for step progress.
- **Sum the checkpoint's `SubTaskAttempt.duration_seconds`**: no migration, and
  partly backfillable, but misses planning, evaluation, verdict, restore waits and
  cleanup, and needs JSON parsing per row, so no SQL aggregate for the average.
  Rejected; could serve as a backfill estimate if Q2 prefers estimates over "—".
- **Derive segments from `CaseHelpRequest.requested_at/resolved_at`**: the
  restart time after Continue is not recorded (gates can delay it), and Resume
  has no such row. Too fuzzy.
- **A `CaseExecutionSegment` table** (one row per segment): precise history, but
  more writes and a join for every average. Not needed for the request; the two
  columns are the simplest design that is exact.
- **Average in Python from loaded pivots**: N rows loaded per run, and impossible
  on the paginated Runs list without N+1. Rejected for a DB aggregate.

## Risks and mitigations
- **Migration ordering with `docs/plan/feat-failures-and-inbox-dashboard.md`.** That
  plan adds `projects.0034_run_case_status_finished_idx` (`AddIndexConcurrently`,
  `atomic = False`, index on `TestRunTestCase(status, -finished_at)`) and lists a
  future "failed by" field on `TestRunTestCase` (its idea 7; another planner may be
  adding it now). Both touch the same table. Whichever lands second renumbers:
  this plan assumes it goes after 0034, as `0035_case_active_duration` (and
  `0036_backfill_…`), depending on `0034`. If the "failed by" field ships as its
  own migration, put it in a different migration file from ours and chain them;
  do not merge them into one. Keep our schema migration atomic (plain `AddField`
  of nullable columns) and separate from their non-atomic concurrent index
  migration. Neither plan changes `finished_at` semantics, so their index and
  "Failed at" column are unaffected.
- **Populated table**: `AddField` nullable without default is metadata-only in
  Postgres. The backfill is one set-based UPDATE with `Exists` subqueries; on a
  large table run it off-peak; it is idempotent (`WHERE active_duration IS NULL`).
- **Deploy with a case running**: the old worker opened no segment; after the
  workers restart the case is redelivered or paused, its `started_at` is set and
  `active_duration` null, so it stays "unknown" (by design) until re-run. Prefer
  deploying with no Started run.
- **Double counting** on abort vs worker cancel: the conditional UPDATE on
  `segment_started_at IS NOT NULL` makes close idempotent.
- **Stale worker writes**: `_finalize_pivot` is already skipped for stale
  generations; abort closes the segment before a Resume can reopen it.
- **Step progress regression**: renaming the `_current_activity` input is the one
  behavioural seam; verify the "Starting" activity after a Resume (Verification 4).
- **N+1**: the list average is an annotation on the existing aggregate query; the
  detail stats are one aggregate. The case table already loads pivots; the new
  columns are plain fields (check no `.only()`/`.defer()` in
  `list_test_run_cases` drops them).
- No secrets, auth, controller or XSS surface touched.

## Implementation steps
1. Model fields + schema migration (files: `projects/models.py`,
   `projects/migrations/0035_case_active_duration.py`, `projects/admin.py`
   read-only display of the two fields).
2. Timing service + pure formatter + filter (files:
   `projects/case_duration_services.py`, `projects/duration_format.py`,
   `projects/templatetags/project_extras.py`).
3. Wire segment open/close and the resets (files: `projects/services.py`
   `_mark_pivot_in_progress`, `_finalize_pivot`, `_pause_run_for_help`,
   `_mark_pivot_cancelled`, reset at `:2335`; `projects/run_rerun_services.py`;
   docstring in `projects/run_reopen_services.reset_case_for_restart`).
4. Step progress switch to `segment_started_at` (files:
   `projects/case_step_progress.py`, `projects/services.py` `_broadcast_case_steps`,
   `projects/testrail_results_services.py` `pivot_step_breakdown`).
5. TestRail elapsed from `active_duration` (files: `projects/testrail_mapping.py`,
   `projects/testrail_results_services.py`, `projects/testrail_checked_push_services.py`).
6. Average: `_annotate_run_counts` annotation and `get_run_case_duration_stats`;
   view context (files: `projects/services.py`, `projects/case_duration_services.py`,
   `projects/views.py` `_build_test_run_detail_context`, `test_run_case_detail`).
7. Templates (files: `templates/projects/test_run_detail.html`,
   `templates/projects/includes/_test_run_table.html`,
   `templates/projects/test_run_case_detail.html`; CSS only if a class is missing in
   `static/css/punk-hazard.css`).
8. Optional backfill data migration, after Q2 is answered (files:
   `projects/migrations/0036_backfill_case_active_duration.py`).
9. `docs/changed/2026-10-28-case-duration.md` after review.

Steps 2, 4 and 5 are independent files and can go to parallel sub-agents after
step 1; step 3 and 6 both edit `projects/services.py`, so one agent owns both.

## Verification
- `pre-commit run --all-files` (black, isort, mypy strict); `python manage.py check`;
  `python manage.py makemigrations --check`; `python manage.py sqlmigrate projects 0035`
  shows two `ADD COLUMN … NULL` and nothing else.
- Manual, with a runner and vision worker:
  1. Run a short case straight through: run page Duration ≈ wall clock of the case
     (minus teardown); TestRail elapsed matches.
  2. Force a Need help (stop the app under test mid-case), wait ~3 minutes,
     Continue, let it pass: duration = time before the pause + time after Continue,
     not just the latter; case page shows the wall clock note with the wait.
  3. Mark failed from Need help: duration = time up to the pause.
  4. Resume a Blocked case: duration adds to the earlier one; while the resumed
     worker starts, the Steps card shows "Starting" (segment field drives it).
  5. Abort while a case runs, then Resume: no double counting (inspect
     `active_duration` in admin before/after).
  6. Re-run in place: duration starts from zero.
  7. Runs tab: "Avg per case" column; check query count unchanged with
     django-debug output or `connection.queries` in `manage.py shell` (read-only).
  8. Legacy rows: shown "—", excluded from the average, "n without timing" shown.
- Run `reviewer` and `verifier` agents (give the verifier the original request and
  this plan path).

## Open questions
- Q1 What should "time taken" mean: active execution time (excludes time waiting
  for a person / Night auto-fix / Blocked before Resume), or wall clock from first
  start to final verdict? (Assumption used in this draft: active execution time;
  wall clock shown as a secondary note on the case page only.)
- Q2 Existing cases: backfill the ones that provably ran in one segment (exact) and
  show "—" for the rest, or show "—" for all old cases, or estimate the multi-segment
  ones from per-step durations? (Assumption: backfill single-segment cases, "—"
  for the rest, excluded from averages.)
- Q3 Which cases count toward a run's average: Passed and Failed only, or also
  Blocked (stopped by a person mid-case) and Cancelled? (Assumption: Passed and
  Failed only.)
- Q4 Should the post-verdict teardown (closing apps a failed case left open, up to
  2 minutes) count as part of the case's time? (Assumption: no; the time ends at
  the verdict, as `finished_at` does today.)
- Q5 Where should the average appear besides the run page and the Runs tab: also
  the project overview's recent runs and the project list's latest run? (Assumption:
  run page and Runs tab only; the overview gets the annotation for free if wanted.)
- Q6 The run's own Duration is wall clock and, after a Re-run in place, counts from
  the original Start. Leave it, or also add a "Total case time" (sum of active
  durations) next to it? (Assumption: leave it; out of scope.)

## Changelog
- 2026-10-28: created

- 2026-10-28: User accepted the default (Assumption) answer to every open question. Implementation started on branch feature/failures-inbox-duration. Migration order across both plans: projects.0034 failed_by + night_digest_sent_at, 0035 concurrent status/finished_at index, 0036 case active_duration fields, 0037 backfill. One shared duration formatter: auto_tester/durations.py (format_duration); projects/run_durations.run_duration stays Start-to-Done wall clock (duration Q6 default).
- 2026-10-28 review round 1, finding not changed: a Celery redelivery after a killed worker overwrites the open segment, so that worker's time is lost (logged as a warning in `open_case_segment`). Accepted in this plan's Risks; recovering it needs a heartbeat on the segment, out of scope.
- 2026-10-28 review round 2: backfill rule tightened. Legacy `started_at` was reset on every pickup (help Continue included, no snapshot), so `requested_at >= started_at` let multi-segment cases through; any help request after the latest `rerun` snapshot (any at all if never re-run) now leaves the row null.
