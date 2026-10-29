# feat: Test sessions (testing hours, projects, tests, CSV export)

Status: Draft
Created: 2026-10-29
Request: A tester can start a session and stop it later. The session measures their
testing hours, the projects they worked in and the number of tests they did. For
any session they can download a CSV of the tests they did in it, with each test's
project, run, case, start and end, duration, result and who performed it. Build it
as a new self-contained Django app `test_sessions`. Keep edits to `accounts/` and
`projects/` small and isolated, because a parallel branch is changing permissions
there (user management, a superuser-only run help mode, read-only members).

## Context

What exists today, and what it means for "tests the tester did":

- **No run-level attribution.** `TestRun` (`projects/models.py`) has no
  `started_by`. `projects/services.py::start_test_run(test_run, *, help_mode)`
  takes no user. It is called from `projects/views.py::test_run_start` and
  `projects/views.py::_handle_setup_run_submit` (the onboarding wizard).
  **Start, the most common testing action, records no user anywhere.**
- **Per-case attribution that exists today**, all with a user and a timestamp:
  - `TestRunCaseResume.resumed_by` / `created_at` / `kind` (`resume`, `rerun`,
    `redesign`). Written by `run_resume_services._resume_pivots` (Resume a run or
    one case), `run_rerun_services.rerun_test_run_cases` (Re-run selected, in
    place) and `case_review_redesign_services.rerun_with_redesigned_plan`. Each row
    also snapshots the replaced `previous_status/result/finished_at`, but not the
    duration.
  - `CasePersonStep.reported_by`/`reported_at`/`reported_via` and
    `closed_by`/`closed_at`/`closed_via` (Human in the loop). The actor is
    `projects/person_step_actor.PersonStepActor`. A Test Runner answer has a user
    only when the runner's email matched an active member. All four web/runner
    actions end in `person_step_services._after_action`.
  - `CaseHelpRequest.resolved_by`/`resolved_at`, set only by a person through
    `case_help_services._resolve`, which Continue, Mark failed and Mark blocked
    share. Auto-fix resolutions have no user.
  - `CaseResultChange.decided_by`/`decided_at`, set by
    `case_result_change_services.apply_result_change`.
  - `TestRunTestCase.checked_by`/`checked_at` (the review mark; not testing work).
- **Reset and Re-run wipe history in place.** `projects/services.py::reset_test_run`
  rewrites every pivot to CREATED, clearing status, result, times and
  `active_duration`. It also deletes the run's `TestRunCaseResume`,
  `CaseResultChange` and `CasePersonStep` rows. `rerun_test_run_cases` clears the
  same execution fields on the selected pivots. The pivot row is reused, so any
  reader that joins to the live pivot later shows the *newer* execution.
  `CaseHelpRequest` rows survive a reset.
- **Timing.** On `TestRunTestCase` (see `docs/changed/2026-10-28-case-duration.md`):
  - `started_at`: the first start since the last from-scratch start.
  - `finished_at`.
  - `active_duration`: the sum of the execution segments. Resume keeps it and keeps
    accumulating.
  - `shown_duration`: the duration, shown only when the status is in
    `FINISHED_CASE_STATUSES`.

  The display helpers are `auto_tester/durations.format_duration` and the
  `duration` filter in `projects/templatetags/project_extras.py`.
- **Who performed the steps.** `TestRun.help_mode == "hitl"` means a person performs
  every step (attempts carry `performed_by="person"` inside the checkpoint JSON).
  Otherwise the AI agent performs them. The mode can change mid-run.
- **Deletion.** Projects are archived, never deleted from the UI. A run can be
  deleted only while WAITING (`delete_test_run`), so only before its first Start or
  after a Reset.
- **Prior art to reuse:**
  - `dashboard/failure_views.py` and `dashboard/services/failed_case_export.py`:
    an async keyset-paged `StreamingHttpResponse`, a BOM and a 50,000-row cap.
  - `dashboard/services/export_throttle.py`: 5 exports per minute, using the cache.
  - `auto_tester/csv_safe.neutralize_cell`.
  - `auto_tester/pagination.py` and `templates/partials/pagination.html`.
  - `dashboard/context_processors.attention_badge`: a lazy per-page query, the
    pattern for a nav indicator.
- **Cross-app dependency precedent.** `projects` already calls other apps' services
  directly: `projects/case_failed_notifications.py` imports
  `notifications.services.record_missed_failure`. The project uses no Django
  signals, and the "no lazy imports" rule rules out wiring receivers in
  `AppConfig.ready()`.
- **Celery beat.** `CELERY_BEAT_SCHEDULE` and `CELERY_TASK_ROUTES` live in
  `auto_tester/settings.py`. The `notify` queue hosts small periodic tasks.
- **Migrations.** Latest are `projects.0040`, `notifications.0004` and
  `dashboard.0001`. The parallel branch may add `projects.0041`. This plan adds
  **no** `projects` migration.

## Goals

- A tester starts and stops a session from the UI. There is at most one open
  session per user, enforced by the database.
- Each session shows:
  - wall-clock hours;
  - test time (the sum of the case durations);
  - distinct projects;
  - the number of tests;
  - a status breakdown and a per-project breakdown.
- A sessions list page and a session detail page, plus a nav entry with a live
  indicator while a session is open.
- A streamed CSV of a session's tests with these columns:
  - project, run, case title, TestRail id;
  - start and end, duration;
  - result status and verdict text;
  - who performed the steps, and the tester's own actions.
- Attribution that survives Reset, Re-run and later executions of the same pivot.
- Owner-only access. All aggregation is done in SQL with no N+1, and each page has
  a fixed query count.

## Non-goals

- A team or manager view of other users' sessions; superusers get read-only
  Django admin only (see Q4).
- Editing start or end times, or pausing a session.
- Attributing anonymous Test Runner answers, Night auto-fix work or any action with
  no signed-in user.
- Changing `TestRun`/`TestRunTestCase` schema or adding `started_by` to projects.
- Live WebSocket updates on session pages (reload shows the current values).
- Backfilling sessions for past work.

## Design

### Attribution: what "a test the tester did" means (recommended, see Q1)

A **session test** is one `TestSessionCase` row: one run case (pivot) attributed to
one session. The row is written **at action time** by a single service,
`test_sessions.services.record_case_activity.record_case_activity`. The action site
calls it inside the action's own transaction. If the acting user has an open
session, the pivot is upserted into it. If not, nothing is recorded.

Recorded actions (`CaseActivity`):

| Kind | Where | Discards the verdict? |
|---|---|---|
| `start` | Start a run (all its cases) | no (cases are CREATED) |
| `resume` | Resume run / Resume case | yes |
| `rerun` | Re-run selected cases in place | yes |
| `redesign_rerun` | Re-run with a redesigned plan | yes |
| `help` | Continue / Mark failed / Mark blocked after help | no |
| `person_step` | HITL report / overrule / mark failed / mark blocked (web, or runner with a named member) | no |
| `result_change` | Apply a result change | no (a correction of the same execution) |

A re-run that creates a *new* run (`projects/services.py::rerun_cases`) only
creates WAITING pivots. Its Start is then attributed as `start`.

Rules:

- **Unique per (session, pivot).** The same case acted on twice in one session
  counts once. The row keeps `first_activity`/`first_activity_at`,
  `last_activity`/`last_activity_at`, `activity_count` and `person_step_count`.
- **A row's outcome follows its case until the case's verdict is discarded.**
  While the row is *live* (`frozen_at IS NULL`), status, times, duration and result
  are read from the pivot. Before an action discards the verdict, the outcome is
  copied into the row's `frozen_*` columns and `frozen_at` is set. That covers
  Resume, Re-run in place, Redesign re-run (marked "yes" in the table) and Reset.
  This is done for every unfrozen row of that pivot in *other* sessions. The
  actor's own session row stays live, and is made live again if it was frozen.
  This keeps an export stable after a Reset, after someone else's re-run, and
  after the run is deleted.
- **Stopping a session freezes its rows whose case has finished.** A closed
  session is then fixed history. Rows still running at stop stay live, because
  they follow the same execution.
- **Session bookkeeping never fails a test action.** `record_case_activity` and
  `freeze_session_cases` run in a savepoint (`transaction.atomic()`). They catch
  `DatabaseError` only, log it with the pivot ids and return, so the action's
  outer transaction survives.

Why explicit attribution and not an inferred user + time window:

1. Start, the main testing action, has no user today. Inferring would still need
   a hook or a `TestRun.started_by` column (a `projects` migration that could
   conflict with the parallel branch).
2. Reset deletes the very rows inference would read: `TestRunCaseResume`,
   `CasePersonStep` and `CaseResultChange`.
3. Live pivots are reused by Reset and Re-run, so a time window cannot tell which
   execution a past session saw.
4. Each page would need a union of five sources over time ranges, instead of one
   indexed table.

The cost is about a dozen one-line calls in `projects` (listed in step 3).

### Data model (`test_sessions/models.py`, migration `test_sessions.0001_initial`)

`TestSession`:

- Fields:
  - `user` FK `AUTH_USER_MODEL` CASCADE, `related_name="test_sessions"`;
  - `name` CharField(120, blank) and `note` TextField(max_length=2000, blank);
  - `started_at` DateTimeField and `ended_at` DateTimeField(null) (null = open);
  - `closed_automatically` BooleanField(default False);
  - `created_at`, `updated_at`.
- Constraints:
  - `UniqueConstraint(fields=["user"], condition=Q(ended_at__isnull=True),
    name="test_session_one_open_per_user")`;
  - `CheckConstraint(condition=Q(ended_at__isnull=True) |
    Q(ended_at__gte=F("started_at")), name="test_session_ends_after_start")`.
    Django 5.2 uses `condition=`; `check=` is deprecated since 5.1.
- Index: `Index(fields=["user", "-started_at"], name="test_session_user_started_idx")`.
- Properties: `is_open`, `display_name` ("Session of 29 Oct 2026, 09:14" when
  unnamed).

`TestSessionCase`:

- Links:
  - `session` FK TestSession CASCADE, `related_name="cases"`;
  - `test_run_test_case` FK `projects.TestRunTestCase` SET_NULL null,
    `related_name="+"`;
  - `project` FK `projects.Project` SET_NULL null, `related_name="+"`;
  - `test_run` FK `projects.TestRun` SET_NULL null, `related_name="+"`.
- Identity snapshot, refreshed on each activity, so rows of deleted runs still
  export: `project_name`, `run_name`, `run_ref` (int), `pivot_ref` (int),
  `case_title`, `testrail_case_id`.
- Activity:
  - `first_activity`, `last_activity` (CharField, `CaseActivity.choices`);
  - `first_activity_at`, `last_activity_at`;
  - `activity_count`, `person_step_count` (PositiveIntegerField);
  - `run_help_mode` (CharField(16)), the run's mode at the last activity, which
    drives "Performed by".
- Frozen outcome (all null or blank while live): `frozen_at`, `frozen_status`,
  `frozen_failed_by`, `frozen_blocked_reason`, `frozen_started_at`,
  `frozen_finished_at`, `frozen_active_duration`, `frozen_result` (TextField).
- Constraints and indexes:
  - `UniqueConstraint(fields=["session", "test_run_test_case"],
    name="test_session_case_unique")`. Null pivots are distinct, which is
    intended for orphaned rows.
  - The FK index on `test_run_test_case` serves the freeze lookups.
  - `Index(fields=["session", "id"])` serves export keyset paging.

`CaseActivity` (TextChoices) lives in `test_sessions/case_activity.py`, a leaf
module with no imports from `projects`, so `projects` can import it cheaply.
`VERDICT_DISCARDING = frozenset({RESUME, RERUN, REDESIGN_RERUN})`.

Effective-outcome expressions live in `test_sessions/services/case_outcome.py`.
They are built once and reused by the pages and the export:

- `effective_status`: `Case(When(frozen_at__isnull=False,
  then=F("frozen_status")), default=F("test_run_test_case__status"))`.
- The same shape for `failed_by`, `blocked_reason`, `started_at`, `finished_at` and
  `result`.
- `effective_duration`: the frozen duration, else the live `active_duration` only
  when the live status is in `FINISHED_CASE_STATUSES` (mirrors `shown_duration`).
- When the pivot is null and the row is unfrozen, the status is null and is shown
  as "Not available".

### Services (`test_sessions/services/`)

| Module | Public API | Notes |
|---|---|---|
| `start_session.py` | `start_session(*, user, name, note) -> TestSession` | Insert. `IntegrityError` on the partial unique constraint maps to `SessionAlreadyOpenError`. |
| `stop_session.py` | `stop_session(*, user, session_id, note) -> TestSession` | Conditional UPDATE `ended_at=now` where open and owned, plus freeze of its finished rows, in one transaction. Already closed raises `SessionAlreadyClosedError`; not owned raises `SessionNotFoundError`. |
| `edit_session.py` | `edit_session(*, user, session_id, name, note)` | Name and note only. |
| `delete_session.py` | `delete_session(*, user, session_id)` | Owner only; rows cascade. |
| `session_queries.py` | `get_open_session(user)`, `get_session_for_user(user, session_id)`, `list_sessions(user, *, page, per_page) -> Page[SessionListRow]`, `get_session_summary(session) -> SessionSummary`, `list_session_cases(session, *, page, per_page) -> Page[SessionCaseRow]` | Every query is filtered on `user`, so another user's id raises 404. Row types are frozen dataclasses: the template contract, never ORM instances. |
| `record_case_activity.py` | `record_case_activity(*, user: CustomUser \| None, pivot_ids: Sequence[int], activity: CaseActivity) -> None`, `freeze_session_cases_of_run(test_run_id: int) -> None` | Explained below. |
| `case_outcome.py` | expression builders, status and label helpers | Pure query-expression code plus label mapping. |
| `session_export.py` | `stream_session_csv(session_id: int) -> AsyncIterator[str]`, `session_csv_page(...)` | Mirrors `failed_case_export.py`. |
| `export_throttle.py` | `check_session_export_throttle(user)` | Same fixed window as the dashboard one (5 per 60 s), with its own key. |
| `close_stale_sessions.py` | `close_stale_sessions(now) -> int` | The beat task's body (see Q2). |

How `record_case_activity` works, inside a savepoint:

1. One query loads `(id, status, failed_by, blocked_reason, started_at,
   finished_at, active_duration, result, test_run_id, run name, run help_mode,
   project_id, project name, case title, testrail_id)` for `pivot_ids`.
2. If the activity is in `VERDICT_DISCARDING`, one `bulk_update` freezes the
   unfrozen rows of those pivots that are not in the actor's open session.
3. If `user` has an open session (one indexed query):
   - `bulk_create(ignore_conflicts=True)` inserts the new rows;
   - one UPDATE sets `last_activity`/`last_activity_at`/`run_help_mode`/snapshot
     names, adds 1 to `activity_count` (and `person_step_count` for
     `person_step`), and clears `frozen_*`.

That is 3 to 5 queries per action, independent of the case count.
`freeze_session_cases_of_run` does the same freeze for every pivot of a run (Reset)
in 2 queries.

**Import rule** (like the `machine_setup` one in `CLAUDE.md`): `test_sessions`
modules import only `projects.models`. Module-level imports from `projects.services`
or any other `projects` module are forbidden, because `projects` imports
`test_sessions.services.record_case_activity` and `test_sessions.case_activity`.
Status labels come from `TestRunTestCaseStatus` / `FailedBy` / `BlockedReason`
choices in `projects.models`.

### Pages and forms

New app URLs are included at the root of `auto_tester/urls.py` with
`app_name = "test_sessions"`. The `test-sessions/` prefix avoids confusion with
Django's own sessions. Every view is `@login_required` with
`AuthenticatedRequest`, thin, and maps `TestSessionError` subclasses to a message
plus a redirect, or to 404.

| URL (name) | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `test-sessions/` (`list`) | GET (`require_safe`) | login | `TestSessionStartForm` (shown when none is open) | `test_sessions/list.html`: `open_session` (SessionListRow \| None), `start_form`, `page_obj` (Page[SessionListRow]), `elided_page_range`, `per_page`, `allowed_per_page`, `query_params`, `active_nav="sessions"` | none |
| `test-sessions/start/` (`start`) | POST | login | `TestSessionStartForm` (`name` ≤120, `note` ≤2000, both optional) | redirect to the detail page | `SessionAlreadyOpenError`: message, redirect to the open one |
| `test-sessions/<int:session_id>/` (`detail`) | GET | owner | `TestSessionStopForm` (`note`), `TestSessionEditForm` (`name`, `note`) | `test_sessions/detail.html`: `session`, `summary` (SessionSummary: `wall_clock`, `test_time`, `test_count`, `project_count`, `status_counts`, `by_project` list), `page_obj` (Page[SessionCaseRow]), pagination keys, `stop_form`, `edit_form`, `export_url`, `active_nav="sessions"` | 404 when not owned |
| `test-sessions/<id>/stop/` (`stop`) | POST | owner | `TestSessionStopForm` | redirect to the detail page | already closed: info message; 404 |
| `test-sessions/<id>/edit/` (`edit`) | POST | owner | `TestSessionEditForm` | redirect to the detail page | form errors via `form_error_text`-style message |
| `test-sessions/<id>/delete/` (`delete`) | POST | owner | none (confirm dialog) | redirect to the list | 404 |
| `test-sessions/<id>/export.csv` (`export`) | GET | owner | none | `StreamingHttpResponse(text/csv; charset=utf-8)`, `Content-Disposition: attachment; filename="test-session-<id>-<YYYYMMDD>.csv"`, `Cache-Control: no-store` | throttled: message, redirect to the detail page; 404 |

`SessionCaseRow` holds:

- pivot id, project id and name, run id and name, case title, TestRail id;
- effective status, `failed_by` label, `blocked_reason` label;
- started, finished, duration;
- performed-by label, the tester's actions label, person step count;
- `is_live`, and `case_url` (None when the pivot is gone or the user is no longer
  a member).

The member check uses one `values_list` of the user's project ids per page.

`SessionListRow` holds: id, display name, started, ended, `is_open`,
`closed_automatically`, `wall_clock`, `test_count`, `project_count`, `test_time`.

Templates use only `static/css/punk-hazard.css` classes: `.page-header`, `.card`,
`.grid-4` for stat tiles, `.data-table` with `data-label`, `.badge-running`
("Live"), `partials/badge_case_status.html`, `partials/pagination.html`,
`partials/empty_state.html` and `partials/confirm_dialog.html` (delete). Edit and
Stop use dialogs per `templates/partials/README.md`. All user text (name, note,
verdict) is auto-escaped, with no `|safe`. Add a CSS class to `punk-hazard.css`
only if a stat tile needs one; no inline styles.

Nav: add a `◷ Sessions` link to `templates/partials/nav_links.html` for
`active_nav == "sessions"`, with a small `badge-running` "Live" chip when
`open_test_session` is truthy. Update the contract comments in that partial and in
`templates/layouts/app.html`. A new context processor,
`test_sessions.context_processors.open_test_session`, returns a `SimpleLazyObject`
of `get_open_session(user)`, which costs one indexed query only on pages that render
the nav.

CSV columns, in order:

1. Project
2. Run
3. Run ID
4. Run case ID
5. TestRail ID
6. Case
7. Started (UTC)
8. Finished (UTC)
9. Duration (`format_duration`)
10. Duration (seconds)
11. Result (status label)
12. Failed by / Blocked reason
13. Performed by ("AI agent" or "Person (Human in the loop)")
14. Your actions (for example "Started, Resumed")
15. Your person steps
16. First action (UTC)
17. Last action (UTC)
18. Snapshot ("Live" or "As of <frozen_at>")
19. Verdict

Every cell goes through `neutralize_cell`. The file starts with the BOM. Keyset on
`id` ascending, 1,000 rows per page, at most 50,000 rows. Logs, checkpoints and
screenshots are never included.

### Query counts (target; the implementer confirms with `CaptureQueriesContext`)

These exclude session, auth and messages, and the existing `attention_badge`.

- **List:**
  - 1 open session (the context processor; the view reuses the same lookup, or the
    view passes `open_test_session` itself);
  - 1 paginator count;
  - 1 page query with per-session aggregates. These are correlated subqueries so
    joins do not multiply: `Count` cases, `Count(project, distinct)`, and `Sum` of
    the effective duration via the pivot join.

  Total 3, constant in page size.
- **Detail:**
  - 1 session (owner-filtered);
  - 1 totals aggregate (count, distinct projects, test time, conditional counts per
    effective status);
  - 1 per-project GROUP BY;
  - 1 paginator count;
  - 1 page of rows (`values()` with the pivot, run and project joins already in the
    expressions);
  - 1 member project ids.

  Total 6, constant.
- **Export:** 1 session plus ceil(rows / 1000) page queries, plus one member-ids
  query if case URLs are included (they are not; the CSV has ids only).
- **Start / Stop / Edit / Delete:** 1 to 3. Stop adds 2 (read the finished live
  rows, then `bulk_update`).

### Background work and WebSockets

- New Celery task `test_sessions.tasks.close_stale_test_sessions`, on the `notify`
  queue and thin, which calls `close_stale_sessions(timezone.now())`.
  - Beat entry `test-sessions-close-stale` every 900 s, with `expire_seconds` 840.
  - It closes sessions open longer than `TEST_SESSION_MAX_HOURS`. It sets
    `ended_at = started_at + max`, `closed_automatically=True`, and freezes
    finished rows, the same way as Stop.
  - `TEST_SESSION_MAX_HOURS = 0` disables it. (Assumption; see Q2.)
- No WebSocket changes, no controller protocol changes, no `CLIENT_VERSION` or
  capability impact.

### Settings / infrastructure

- `auto_tester/settings.py`:
  - add `"test_sessions"` to `INSTALLED_APPS`;
  - add the context processor `test_sessions.context_processors.open_test_session`;
  - add `TEST_SESSION_MAX_HOURS: int = config("TEST_SESSION_MAX_HOURS", default=12,
    cast=int)`, validated as ≥ 0;
  - add the route and the beat entry.
- `example.env`: add `TEST_SESSION_MAX_HOURS=12` with a comment.
- `auto_tester/urls.py`: `path("", include("test_sessions.urls"))`.
- Deploy: `migrate`, then restart Daphne, the `notify` worker and beat (beat
  installs the new schedule).

### Data flow

1. The tester presses **Start session** on `/test-sessions/`. `start` calls
   `start_session` and inserts an open `TestSession`. The nav shows "Live".
2. The tester starts a run. `test_run_start` calls `start_test_run(test_run,
   help_mode=…, user=request.user)`. Inside its transaction, it calls
   `record_case_activity(user, pivot_ids, START)`, which upserts one
   `TestSessionCase` per case.
3. The run executes. The rows stay live and read status and duration from the
   pivots.
4. A case pauses. The tester answers a HITL step, which calls `_after_action` and
   records `person_step`. They Continue after help, which calls `_resolve` and
   records `help`. Or they Resume, which records `resume` and freezes any other
   session's row for that pivot first.
5. A colleague later resets the run. `reset_test_run` calls
   `freeze_session_cases_of_run` before wiping, so the tester's rows keep their
   outcomes.
6. The tester presses **Stop**. `stop_session` sets `ended_at` and freezes
   finished rows.
7. The detail page shows the aggregates. **Export CSV** streams `session_csv_page`
   chunks through `database_sync_to_async`.

## Alternatives considered

- **Infer by user + time window from existing rows** (resume snapshots, person
  steps, help resolutions, result changes). Rejected:
  - Start has no user.
  - Reset deletes the source rows.
  - Reused pivots make old sessions show new results.
  - It needs a five-way union on every page.
- **Add `TestRun.started_by`.** Rejected for now:
  - it needs a `projects` migration, which risks a numbering clash with the
    parallel branch;
  - it attributes runs, not the cases a tester resumed or answered;
  - it still loses history on Reset.
- **A Django signal from `projects`, received in `test_sessions`.** Inverts the
  dependency. Rejected:
  - the project uses no signals;
  - connecting receivers needs an import in `AppConfig.ready()`, which breaks the
    no-lazy-imports rule;
  - direct calls into another app's service already have precedent
    (`notifications.record_missed_failure`).
- **An append-only event table** (one row per action) next to or instead of the
  per-case row. Not needed for the requested numbers or export. The per-case row
  with first/last/count is enough. Add an event log later if a timeline is wanted.
- **Snapshot only at Stop.** Too late: a Reset or re-run during an open session
  would already have wiped the outcome.
- **Reuse `dashboard.services.export_throttle`.** It is named and keyed for
  Failures, and sharing it would couple two apps for 10 lines. A shared
  fixed-window limiter in `auto_tester/` is a reasonable follow-up refactor, but
  it is not part of this plan.

## Risks and mitigations

- **Hook edits in files the parallel branch also edits** (`projects/views.py`,
  services). Each hook is one call at a named spot. Merge conflicts are trivial.
  Re-check the hook list after rebasing onto that branch.
- **A bookkeeping failure breaking a test action.** The savepoint plus a narrow
  `DatabaseError` catch and a log mitigate it. Never let `record_case_activity`
  raise domain errors.
- **Freeze ordering.** The freeze must run *before* the verdict is wiped, inside the
  same transaction, after the action's locks are taken. Each hook location below is
  placed accordingly. The verifier must check every one of them.
- **Extra work inside Start for big runs** (for example 500 cases). It is still 3
  to 5 set-based queries, with no per-case loop, while the project and run locks
  are held.
- **Double counting across sessions.** A case resumed in a second session counts in
  both sessions. Its `active_duration` accumulates since the last from-scratch
  start, so the second session's duration includes the first session's segments.
  This is documented on the detail page ("Duration is the case's total time").
  Alternatives are in Q3.
- **Information after leaving a project.** Live rows show the current outcome of
  the same execution (for example, a result change applied later). Stopping a
  session freezes finished rows, so only rows unfinished at stop stay live. Case
  links are hidden when the user is no longer a member.
- **XSS.** Names, notes, case titles and verdicts are rendered auto-escaped.
- **CSV injection.** `neutralize_cell` is applied to every cell.
- **Export load.** The row cap, keyset paging, an async iterator under ASGI, and
  the per-user throttle.
- **Migrations.** One new-table migration, `test_sessions.0001` (depends on
  `projects.0040` and the user model). Nothing is altered or dropped, so there is
  no data-loss risk. It does not conflict with a future `projects.0041`.

## Implementation steps

1. **Shared foundation (orchestrator, before the sub-agents start).** Files:
   - `test_sessions/__init__.py`, `apps.py`, `case_activity.py`, `models.py`,
     `exceptions.py`, `admin.py` (read-only `TestSessionAdmin` with a
     `TestSessionCase` inline, typed `ModelAdmin[TestSession]`);
   - `migrations/0001_initial.py`;
   - `auto_tester/settings.py`: `INSTALLED_APPS` only.

   Run `makemigrations test_sessions` and review the SQL with `sqlmigrate`.
2. **Recording service (part A).** File:
   `test_sessions/services/record_case_activity.py`, exposing
   `record_case_activity` and `freeze_session_cases_of_run`.
3. **Hooks in `projects` (part B, after step 2's signature is fixed).** One call
   each:
   - `projects/services.py::start_test_run`: add the keyword-only parameter
     `user: CustomUser`. After the status `save`, call
     `record_case_activity(user=user, pivot_ids=pivot_ids,
     activity=CaseActivity.START)`.
   - `projects/views.py::test_run_start`: pass `user=request.user`, and type the
     request as `AuthenticatedRequest`.
   - `projects/views.py::_handle_setup_run_submit`: same.
   - `projects/services.py::reset_test_run`: call
     `freeze_session_cases_of_run(test_run.pk)` right after the status check, before
     the pivot loop.
   - `projects/run_resume_services.py::_resume_pivots`: after `_lock_and_check`,
     before the loop, call `RESUME`.
   - `projects/run_rerun_services.py::rerun_test_run_cases`: after `selected_ids`,
     before the snapshot and UPDATE, call `RERUN`.
   - `projects/case_review_redesign_services.py::rerun_with_redesigned_plan`: before
     `reset_case_for_restart(locked, now)`, call `REDESIGN_RERUN` for
     `[locked.pk]`.
   - `projects/case_help_services.py::_resolve`: call `HELP` for
     `[request.test_run_test_case_id]` with `user`.
   - `projects/person_step_services.py::_after_action`: add an `actor:
     PersonStepActor` parameter and pass it from its four callers. Call
     `PERSON_STEP` with `actor.user`, which may be None for an anonymous runner
     answer, so nothing is recorded.
   - `projects/case_result_change_services.py::apply_result_change`: call
     `RESULT_CHANGE` with `user`.
4. **Session use cases and queries (part C).** Files under `test_sessions/services/`:
   `start_session.py`, `stop_session.py`, `edit_session.py`, `delete_session.py`,
   `session_queries.py`, `case_outcome.py`, `close_stale_sessions.py`. This part
   depends only on step 1 and must reuse the freeze helper from step 2 (import it;
   do not reimplement it).
5. **Export (part D).** Files: `test_sessions/services/session_export.py` and
   `export_throttle.py`. It depends on `case_outcome.py` from part C, so either
   run it after C or have C publish the expression builders first.
6. **Views, forms, URLs, templates and nav (part E).** Files:
   - `test_sessions/forms.py`, `views.py`, `urls.py`, `context_processors.py`;
   - `templates/test_sessions/list.html`, `detail.html` (and `includes/` if needed);
   - `templates/partials/nav_links.html`, plus the contract comment in
     `templates/layouts/app.html`;
   - `static/css/punk-hazard.css` (only if a new class is required);
   - `auto_tester/urls.py`.
7. **Task, beat and settings (part F).** Files: `test_sessions/tasks.py`,
   `auto_tester/settings.py` (context processor, `TEST_SESSION_MAX_HOURS`, route,
   beat entry), `example.env`.
8. **Integration (orchestrator).** Wire the imports, run
   `pre-commit run --all-files`, `python manage.py check` and
   `python manage.py makemigrations --check --dry-run`.
9. **Docs.** Add the import rule and a short "Test sessions" bullet to `CLAUDE.md`
   architecture, and write `docs/changed/<date>-test-sessions.md` when review is
   clean.

Parallel split: after step 1 and the signature of step 2 are fixed, A+B, C+D, E
and F can each go to their own sub-agent, as they share no file.
`auto_tester/settings.py` is touched in steps 1 and 7, so F owns every settings
edit after step 1. E needs the service signatures and row dataclasses from C, so
publish those from step 1 as stubs, or start E after C.

## Verification

There is no automated test suite. Verify as follows:

- `pre-commit run --all-files` (isort, black, strict mypy), `python manage.py
  check`, `python manage.py makemigrations --check --dry-run`, and `python manage.py
  sqlmigrate test_sessions 0001`. Confirm the partial unique index and the check
  constraint are in the SQL.
- A smoke script on a **throwaway database** (`manage.py shell` against a
  `*_smoke` DB, never dev data) that checks:
  1. A second open session for the same user raises `SessionAlreadyOpenError`;
     stop, then start, succeeds.
  2. `start_test_run(user=…)` with an open session creates one row per case. With
     no open session it creates none.
  3. Set pivots to finished with durations, then check that the summary's
     `test_count`, `project_count`, `test_time`, status counts and wall clock match
     hand-computed values.
  4. Reset the run. The rows are frozen, the outcome is unchanged, and a new Start
     by another user in their session leaves the first session's export unchanged.
  5. Resume by user B. A's row is frozen and B's row is live. Resume by A again:
     A's row is live again.
  6. A person step and a help resolution increment `activity_count` and
     `person_step_count`. An anonymous runner answer records nothing.
  7. Stop freezes finished rows only.
  8. Fetching or exporting another user's session returns 404 through the test
     client.
  9. Query counts with `CaptureQueriesContext` for list, detail and the export
     pages, matching the targets above with 1 and 50 rows.
  10. The CSV: header order, BOM, a `=cmd` title gets neutralised, and paging
      across more than 1,000 rows.
  11. Force a `DatabaseError` inside `record_case_activity` (for example by patching
      the model manager in the script). The Start still commits.
  12. `close_stale_sessions` closes a session older than the max with
      `ended_at = started_at + max`.
- Browser smoke on a unique port (`python -m daphne -b 0.0.0.0 -p 8013
  --websocket-max-message-size 33554432 --websocket-max-frame-size 33554432
  auto_tester.asgi:application`):
  - the nav link and the Live chip;
  - start and stop dialogs, edit, delete confirm;
  - the empty states;
  - pagination;
  - the CSV download opens in a spreadsheet;
  - the mobile `data-label` layout.
- Run the `reviewer` and `verifier` agents in parallel. Give the verifier the
  original request and this plan, and ask it to check every hook location's
  ordering relative to the verdict wipe.

## Open questions

- **Q1. What counts as a test the tester did in a session?**
  - The options are:
    - (a) cases in runs they started, resumed or re-ran;
    - (b) HITL person steps they answered;
    - (c) help requests they resolved;
    - (d) result changes they applied.
  - Recommendation: all four, recorded explicitly at action time into
    `TestSessionCase`, so each case counts once per session and the row lists the
    tester's actions.
  - Trade-offs:
    - Explicit recording needs about a dozen one-line calls in `projects` (step 3).
      In return it survives Reset and Re-run, attributes Start (which has no user
      today), and keeps every page to one indexed table.
    - Inferring from existing rows by user and time window needs no `projects`
      edits except a Start hook, but loses everything on Reset and shows newer
      results for reused cases.
    - Narrowing to (a)+(b) ("executions and steps I performed") drops 2 hooks and
      makes "number of tests" mean only execution, not review work.
  - Assumption in this draft: all four, explicit.
- **Q2. Do open sessions close by themselves?**
  - Recommendation: yes. A beat task closes sessions open longer than
    `TEST_SESSION_MAX_HOURS`, with a "Closed automatically" badge, so a forgotten
    session does not report 60 hours.
  - Trade-offs:
    - No auto-close is simpler, but the hours are wrong whenever someone forgets.
    - Closing after N hours *without recorded activity* is fairer but misses manual
      exploration that records nothing.
  - Assumption in this draft: auto-close at `started_at + 12 h`, configurable, 0
    disables it. There is no other maximum length.
- **Q3. Which hours?**
  - Recommendation: show both. **Session time** is the wall clock from start to
    stop (or now) and is the headline "testing hours". **Test time** is the sum of
    the case durations (`active_duration`; finished cases only).
  - Trade-offs:
    - Test time can exceed the wall clock when the tester runs several projects at
      once.
    - A case resumed across two sessions counts its whole accumulated duration in
      both. Splitting by segment would need per-segment timestamps that `projects`
      does not store.
  - Assumption in this draft: both are shown, and the export has a duration per
    case.
- **Q4. Editing, notes, and superuser access.**
  - Recommendation:
    - the owner can rename, add or edit a note (including at stop) and delete
      their own sessions;
    - start and end times are not editable;
    - session pages and exports are owner-only (another user's id returns 404);
    - superusers get read-only Django admin, with no team page and no export of
      others.
  - Justification: the project has no roles yet; the parallel branch is introducing
    user management and superuser powers, and a team or manager view belongs with
    that work. Live rows read project data the superuser may not be a member of.
  - Trade-off: if hours are used for reporting to a manager, a superuser team view
    (all users' sessions, filter by user, export) is the natural next step. It
    could also allow correcting an auto-closed end time.
  - Assumption in this draft: as recommended.
- **Q5. The same case done twice in one session** (run, Reset, run again).
  - Recommendation: count it once and show its latest outcome, with an
    `activity_count`.
  - Trade-off: one row per execution would count it twice and keep both outcomes,
    at the cost of an execution key and more complex freezing.
  - Assumption in this draft: once per session.

## Changelog

- 2026-10-29: created (Draft).
