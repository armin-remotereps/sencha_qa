# feat: Failed cases page, a slimmer dashboard, a per-user attention inbox, and failure insights

Status: In progress
Created: 2026-10-28
Request: Three UI and metric changes, then an extended scope.
(1) A new page that lists every failed run case across the user's projects,
filterable by date, project, run and more, plus a dashboard widget for it.
(2) On the dashboard only "Needs your attention" is useful: remove the other
sections (run summary stats, recent projects, recent activity). (3) "Needs your
attention" becomes a personal inbox: failures the user missed, chat replies they
missed (both dismissed by hand) and cases that need help (listed while open).
Extended scope accepted by the user: an unread badge in the nav and tab title;
live dashboard updates over a per-user Channels group; top failing cases
(failures grouped by test case); an infrastructure health page (help requests by
fault over time, Night auto-fix success rate); recording who made a case Failed
(agent / help / review) and filtering on it; bulk check / re-run / send to
TestRail from the failures page, reusing the existing services; CSV export of the
filtered list; a morning digest push after Night runs; saved filter presets.
Later addition: a "run finished" ntfy push to project members (its own kind and
checkbox on `/notifications/`) with passed / failed / blocked counts and the
run's duration, sent exactly once per finish.

## Context
- `dashboard/services.py` (legacy single module, 506 lines): `get_dashboard_data`
  builds `RunSummary`, `AttentionItem` list, `ProjectCard` list and `ActivityItem`
  list. Attention is computed on the fly from runs: `_need_help_run_attention_items`
  (PAUSED runs), `_finished_run_attention_items` (DONE runs in 7 days with
  failed/blocked cases), `_disconnected_machine_attention_items` (disconnected
  project with a WAITING run). No per-user read state. `dashboard/models.py` is empty.
- `dashboard/views.py::index`, `templates/dashboard/index.html`,
  `templates/dashboard/includes/{_attention,_run_summary,_recent_projects,_recent_activity}.html`.
  `partials/stat_card.html` is only included by `_run_summary.html`.
  `projects.services.PrimaryAction`/`resolve_primary_action` are also used inside
  `projects/services.py` (~line 543), so they stay.
- Notifications (`notifications/`): `NotificationPreference` (ntfy topic, on/off,
  `notify_need_help` / `notify_failed` / `notify_chat_reply`), `NotificationKind`
  and the kind-to-flag map in `services/notification_message.py`,
  `recipient_user_ids`, `enqueue_notification`, `send_notification` task on
  `notify`. Nothing is stored per notification.
- Failure push: `projects/case_failed_notifications.notify_case_failed(pivot_id, generation=)`,
  called from `projects/services.py::execute_test_run_test_case` after
  `_finalize_pivot` only for the agent's Failed verdict; returns early when
  `ntfy_configured()` is false. Person-made Failed: `case_help_services.mark_failed_after_help`
  and `case_result_change_services.apply_result_change` (no push).
- Help push: `projects/case_help_notifications.notify_case_needs_help`, from
  `_pause_run_for_help` (Attended) or after a Night auto-fix did not fix it.
- Help lifecycle (`CaseHelpRequest`): open while `resolution == ""` (one open per
  pivot). Opened in `projects/services.py::_pause_run_for_help`. Closed in
  `case_help_services._resolve` (Continue / Mark failed / Mark blocked),
  `case_help_services` ~590 (auto-fixed), and `projects/services.py` ~3298 / ~3430
  (cancel). Fields `fault` (`BlockedReason`, blank = the agent asked),
  `auto_fix_state` (`""` / running / fixed / not_fixed), `requested_at`.
- Reply push: `machine_setup/services/turn_reply_notification.notify_turn_reply(turn, notice=)`
  from `run_turn._TurnRunner._finish`; person-started COMPLETED/FAILED turns,
  not auto-fix, thread not archived, sender not viewing (`viewer_presence.is_viewing`).
- Run finish: `projects/services.py::_update_test_run_status_if_needed` does
  `refresh_from_db()` then an unconditional `save()` to DONE, with no lock. As
  `docs/plan/refactor-ntfy-notifications.md` (Q3) notes, two workers finishing at
  once could both see the transition, so a push sent from it could go out twice.
- Reused per-run services:
  - `projects/run_case_check_services.check_run_cases(*, test_run, pivot_ids, user) -> CheckOutcome`
    (atomic; locks; freezes TestRail evidence; raises `RunCaseCheckError`),
    `format_check_message`.
  - `projects/run_rerun_services.rerun_test_run_cases(*, test_run, pivot_ids, user, help_mode=None) -> RerunOutcome`
    (locks project and run, checks reopen blockers, raises `RunRerunError`),
    `format_rerun_message`.
  - `projects/testrail_checked_push_services.start_checked_push(*, test_run, links_base_url)`
    (run must be finished and have TestRail settings; sends every checked,
    unsent case of the run; raises `CheckedPushError` subclasses /
    `TestRailNotConfiguredError`).
- Channels: `projects/consumers.AuthenticatedConsumer` (auth, `_authorize`,
  group add/discard, `_send_initial_state`); routes in `projects/routing.py`,
  `machine_setup/routing.py`, `visual_engine/routing.py`, assembled in
  `auto_tester/asgi.py`. Broadcasts use `async_to_sync(layer.group_send)`.
- Beat: `CELERY_BEAT_SCHEDULE` in `auto_tester/settings.py` (DatabaseScheduler
  installs it); `CELERY_TIMEZONE = TIME_ZONE = "UTC"`; the sweep runs on `notify`.
- Lists: `projects/views.py` has private `_parse_page`, `_parse_per_page`,
  `ALLOWED_PER_PAGE`, `_get_elided_page_range`; `templates/partials/pagination.html`.
  `static/js/punk-hazard.js` has the `bulkSelect` Alpine component.
- `TestRunTestCase`: `status` indexed, `finished_at` not indexed; `logs`,
  `result` and `execution_checkpoint` can be large.
- Projects are shared by equal members; membership is checked per request
  (`docs/changed/2026-10-28-project-sharing.md`). Postgres 17, Django 5.2.11,
  Channels 4.3.2, Celery 5.6.2, django-celery-beat 2.8.1.
- Parallel plan `docs/plan/fix-case-duration.md` (case duration bug + "average
  case time per run") may change `TestRunTestCase` timing fields
  (`started_at`/`finished_at`) and likely `_finalize_pivot`. This plan does not
  change those fields. See "Dependencies".

## Goals
- `/failures/`: paginated, filterable list of Failed run cases in the user's
  non-archived projects, with a "by case" grouping, bulk actions, CSV export and
  saved presets.
- Dashboard: only "Needs your attention" plus a compact "Failed cases" widget,
  updated live.
- Per-user inbox rows for missed failures and missed replies, written whether or
  not ntfy is configured or enabled, dismissed one by one or all at once.
- Help needs listed live from open `CaseHelpRequest` rows.
- Attention count in the nav and tab title on every app page.
- `TestRunTestCase.failed_by` records who made a case Failed.
- `/insights/machines/`: help requests by fault over time and Night auto-fix
  results.
- One morning digest push per user summarising Night runs.
- A run-finished push, sent exactly once each time a run (or its re-run / resume)
  reaches Done, gated by a new `notify_run_finished` checkbox.
- Fixed, small query count on every page; no N+1; bulk writes for many recipients.

## Non-goals
- Flaky case detection, pass-rate trends, ntfy click-to-dismiss, retention
  cleanup of dismissed rows (user excluded them).
- No duration column or duration metrics (owned by `fix-case-duration.md`).
- No backfill of inbox rows or `failed_by` for rows that existed before deploy.
- No change to how ntfy delivers messages, to existing push kinds, or to the
  controller / vision worker protocols.
- No in-app inbox or attention entry for a finished run (see "Run finished
  notification", Q18).
- No notification for a cancelled run (a person did it) or a run that pauses for
  help (that already has the Need help push).

## Resolved decisions (Q1-Q9 of the first draft, defaults accepted 2026-10-28)
- D1 Missed replies clear only with Dismiss; opening the thread does not clear them.
- D2 A missed failure is hidden while its case is not Failed (re-run, changed
  result) and comes back if the case fails again; checking (reviewing) the case
  does not clear it; otherwise manual dismiss.
- D3 Only the agent's Failed verdict creates missed-failure rows (as the push).
- D4 Inbox rows are written regardless of ntfy on/off and the kind checkboxes.
- D5 The "test machine is disconnected" attention item is dropped.
- D6 Needs help also lists requests while the Night auto-fix is trying, with an
  "Auto-fix is trying" note.
- D7 Failures page filters: date range, project, run, title/TestRail id, review
  state, plus `failed_by` (now in scope); default window: all time.
- D8 Failures page is Failed only (no Blocked toggle).
- D9 A "Failures" link is added to the main nav.

## Design

### Data model
All migrations are additive. Shared models and migrations are Phase 1.

`notifications` (migration `notifications.0004_inbox_and_digest`):
- `MissedFailure`: `user` FK CASCADE, `test_run_test_case` FK `projects.TestRunTestCase`
  CASCADE, `occurred_at`, `dismissed_at` null. `UniqueConstraint(user, test_run_test_case)`
  named `missed_failure_user_case`; partial `Index(user, -occurred_at)` where
  `dismissed_at IS NULL` named `missed_failure_open_idx`.
- `MissedReply`: `user` FK CASCADE, `conversation` FK `machine_setup.SetupConversation`
  CASCADE, `outcome` (`MissedReplyOutcome`: reply / paused / failed), `preview`
  CharField(140, plain text, model-written), `occurred_at`, `dismissed_at`.
  `UniqueConstraint(user, conversation)` named `missed_reply_user_conversation`;
  same partial open index.
- `NotificationPreference.notify_night_digest` and
  `NotificationPreference.notify_run_finished`, BooleanField default True each
  (existing users get both, as with chat replies).
- `NotificationKind.NIGHT_DIGEST = "night_digest"` (priority 3, tag `sunrise`,
  flag `notify_night_digest`) and `NotificationKind.RUN_FINISHED = "run_finished"`
  (priority 3, tag `checkered_flag`, flag `notify_run_finished`). Code only, no
  DB change.

One row per (user, case) and (user, conversation); a new event re-opens the row
with `bulk_create(update_conflicts=True, unique_fields=..., update_fields=...)`.
Two tables, not one generic table: dedupe in one table needs partial unique
constraints, which that upsert cannot target (Postgres needs the index predicate
in `ON CONFLICT`, Django cannot emit it; Django ticket #34943).

`projects` (two migrations, kept apart because one must be non-atomic):
- `0034_run_case_failed_by`: `TestRunTestCase.failed_by` CharField(16, choices
  `FailedBy`: `agent` "AI verdict", `help` "Marked failed (help)", `review`
  "Changed after review"; blank = not recorded), default `""`. On Postgres 11+
  adding a column with a constant default is metadata only (no table rewrite).
  `TestRun.night_digest_sent_at` DateTimeField null (claim marker for the digest).
- `0035_run_case_status_finished_idx` (`atomic = False`):
  `AddIndexConcurrently(TestRunTestCase, Index(fields=["status", "-finished_at"], name="run_case_status_finished_idx"))`,
  so running workers are not blocked while it builds.
- `failed_by` means something only while `status == FAILED`. Readers always
  filter on `status=FAILED` too, so a stale value left after a re-run or a
  result change is never shown, and the code that resets cases does not need to
  touch it.

`dashboard` (migration `dashboard.0001_initial`):
- `FailedCaseFilterPreset`: `user` FK CASCADE, `name` CharField(60), `query`
  CharField(1000) holding the encoded query string of the cleaned filters (never
  page or per_page), `created_at`. `UniqueConstraint(user, name)`; at most
  `PRESET_LIMIT = 20` per user (checked in the service). Loading a preset is just
  a link to `/failures/?<query>`; the form validates it again, so a project the
  user has since left is dropped as an invalid choice.

### Services

#### notifications (inbox write, read, dismiss, live, digest)
- `services/record_missed_failure.py`: `record_missed_failure(*, pivot_id: int, project_id: int, occurred_at: datetime) -> list[int]`:
  one query for active member ids, one bulk upsert; returns the user ids (for
  the live broadcast). Never raises (logs ids only).
- `services/record_missed_reply.py`: `record_missed_reply(*, user_id: int, conversation_id: int, outcome: MissedReplyOutcome, preview: str, occurred_at: datetime) -> None`.
- `services/inbox.py` (read): `get_inbox(user, project_ids, *, limit) -> Inbox`
  (`failures`, `replies`, `failure_total`, `reply_total`);
  `count_inbox(user, project_ids) -> tuple[int, int]`. Read-time filters:
  - failures: own, not dismissed, `test_run_test_case__status=FAILED`, run's
    project in `project_ids`; `select_related` case + run + project, `.only(...)`,
    verdict via `Substr(result, 1, 200)`;
  - replies: own, not dismissed, `conversation__archived_at IS NULL`, project in
    `project_ids`.
  Item URLs come from `machine_setup/services/conversation_url.py` (extracted from
  `turn_reply_notification._path`) and `reverse("projects:test_run_case_detail")`.
- `services/dismiss_inbox.py`: `dismiss_missed_failure(user, item_id)`,
  `dismiss_missed_reply(user, item_id)` (one scoped UPDATE; 0 rows raises
  `InboxItemNotFoundError` in `notifications/exceptions.py`),
  `dismiss_all(user, kind, *, up_to) -> int` (only rows with `occurred_at <= up_to`).
  Each calls `broadcast_inbox_changed([user.pk])` on commit.
- `services/inbox_broadcast.py`: `broadcast_inbox_changed(user_ids: list[int]) -> None`
  and `broadcast_project_attention_changed(project_id: int) -> None` (one member
  id query, then one `group_send` per user to `attention_user_<id>`). Event
  `{"type": "attention.changed"}` with no payload, so nothing user-specific or
  secret goes onto the channel layer. Never raises; called from
  `transaction.on_commit`.
- `services/night_digest.py`: `send_night_digests(now) -> int`:
  1. Claim: `TestRun.objects.filter(help_mode=NIGHT, night_digest_sent_at__isnull=True)`
     `.filter(Q(status=DONE) | Q(status=PAUSED))`, with the run started after
     `now - NIGHT_DIGEST_LOOKBACK_HOURS` (so old runs are not digested on the
     first deploy), select the ids, then a conditional
     `UPDATE ... SET night_digest_sent_at=now WHERE id IN (...) AND night_digest_sent_at IS NULL`.
     Only the rows this call updated are digested, so two beat ticks or workers
     never send twice. The digest is a scheduled read and relies on this claim,
     not on the DONE event (the run-finished push relies on the conditional DONE
     transition above).
  2b. Each run line shows its duration via the same `run_duration` helper as
     the run-finished push.
  2. One aggregate over the claimed runs' cases: passed / failed / blocked /
     need help per run (`values("test_run_id").annotate(Count(filter=...))`).
  3. Recipients: `NotificationPreference` rows with `enabled` and
     `notify_night_digest` that are members of those projects (one query
     `values_list("user_id", "user__projects__id")` restricted to the project ids).
  4. One message per user covering all their claimed runs ("Night runs: 3 runs,
     41 passed, 5 failed, 1 needs help"), path `/failures/?failed_by=agent&date_from=<yesterday>`
     (or `/` when only help is pending). One `enqueue_notification` per user
     (task args stay user ids + text, no topics).
  A PAUSED Night run is digested once while still waiting ("1 needs help"); it is
  not digested again when it later finishes (marker already set). See Q11.
  Nothing is sent when ntfy is not configured, but the runs are still claimed so
  turning ntfy on later does not flood the user with old runs.
- `tasks.py`: `send_night_digest` task on `notify` (thin: calls the service).
- `context_processors.py` is not here; see dashboard.

#### Shared prerequisite: exactly-once DONE transition (projects)
Today `_update_test_run_status_if_needed(test_run)` (`projects/services.py`
~3462) reads the run, checks that no case is Created / In progress / Need help,
then calls `save()` to set DONE unconditionally. It is called from the `finally`
of `execute_test_run_test_case` (~3024) by every case worker, with no lock. Two
workers that finish their cases close together can both pass the check and both
"finish" the run. That is harmless for the row (the second save writes the same
status) but would send two pushes. `run_reopen_services` (~164) and Start
(~2433) move the run back to STARTED and bump `execution_generation`, so a run
can legitimately finish more than once (once per Start, Resume or Re-run).

Change, kept inside the same function (one call site):
- Signature becomes `_update_test_run_status_if_needed(test_run: TestRun, *, generation: int) -> bool`;
  the caller already has `generation`.
- Keep the early returns and the "all cases settled" check as they are.
- Replace the `save()` with a conditional
  `TestRun.objects.filter(pk=test_run.pk, status=TestRunStatus.STARTED, execution_generation=generation).update(status=DONE, finished_at=now, updated_at=now)`.
  Postgres row locking makes the second concurrent UPDATE re-check its WHERE
  after the first commits, so exactly one caller gets `rowcount == 1` per
  generation. A stale worker from an older generation matches nothing.
- Only when the row count is 1: set the in-memory fields, `broadcast_test_run_status`
  (as today), and `transaction.on_commit(lambda: notify_run_finished(run_id, generation=generation))`.
  It returns True so the call site can tell what happened (nothing else uses it).
- This is the single "run finished" event point. The run-finished push uses it.
  The morning digest does not have to (it has its own claim marker), but can
  read the same `finished_at`.

#### Run finished notification
- `projects/run_finished_notifications.py` (new, mirrors
  `case_failed_notifications.py`): `notify_run_finished(run_id: int, *, generation: int) -> None`.
  Never raises; returns early when ntfy is not configured.
  1. Load the run with `select_related("project")` and `.only(...)`, filtered on
     `status=DONE, execution_generation=generation` (a newer Start or Re-run
     owns it otherwise).
  2. One aggregate over its cases: passed / failed / blocked / cancelled
     (`Count(filter=Q(status=...))`).
  3. Duration: `run_duration(test_run) -> timedelta | None` (see Dependencies).
     Formatted with a small pure helper (`auto_tester/durations.py::format_duration`,
     e.g. "1 h 12 min", "42 min", "< 1 min").
  4. Recipients: `recipient_user_ids(project_id, NotificationKind.RUN_FINISHED)`.
  5. Message: title `Run finished · <project>` (or `Run finished with failures · <project>`
     when failed > 0), body `<run name>: 12 passed, 3 failed, 1 blocked · 42 min`
     (zero counts dropped, cancelled shown only when > 0), path
     `projects:test_run_detail`. `enqueue_notification` on commit.
- `notifications/forms.py` `NotificationKindsForm` gains `run_finished`
  ("Run finished"), `notifications/services/preferences.update_kinds` and
  `preference_view` carry the flag, `templates/notifications/settings.html`
  shows the checkbox.
- Inbox / attention: **no entry** (decision, Q18). The attention list holds
  things that need the user to act. A finished run with failures already puts
  one missed-failure row per failed case in every member's inbox, and an all-pass
  run needs no action. A per-run row would duplicate those rows or be noise.
  The live socket is still nudged (`broadcast_project_attention_changed`) only
  because the failed widget counts change, not for a new attention item.
- Night runs: every run, Attended or Night, gets the run-finished push when it
  finishes. The morning digest stays a separate once-a-day summary with its own
  checkbox (Q17 asks whether that is what the user wants).

#### projects (failed_by writes, help broadcast hooks)
- `projects/services.py::_finalize_pivot`: set `failed_by=AGENT` with the Failed
  status (add to `update_fields`). **Shared hunk with `fix-case-duration.md`**,
  see Dependencies.
- `case_help_services.mark_failed_after_help`: `failed_by=HELP`.
- `case_result_change_services.apply_result_change`: `failed_by=REVIEW` when the
  new status is Failed.
- `failed_by` values and labels live in `projects/models.py` (`FailedBy` TextChoices).
- Live attention hooks (`broadcast_project_attention_changed(project_id)` on
  commit) where a help request opens or closes: `_pause_run_for_help` (existing
  `_after_commit`), `case_help_services._resolve` and the auto-fixed branch
  (~590), and the two cancel updates in `projects/services.py` (~3298, ~3430).
  `projects` already imports `notifications.services`, so no new dependency
  direction.

#### machine_setup
- `services/conversation_url.py` (new): `conversation_path(conversation) -> str`.
- `services/turn_reply_notification.notify_turn_reply`: eligibility first
  (requester, not auto-fix, COMPLETED/FAILED, not viewing, thread not archived),
  then `record_missed_reply` + `broadcast_inbox_changed([user_id])`, then the
  existing ntfy gates for the push. `_title_and_body` also yields the outcome.
- `projects/case_failed_notifications.notify_case_failed`: load and re-check
  the pivot first, `record_missed_failure` + `broadcast_inbox_changed(ids)` on
  commit, then the ntfy gates; switch `_enqueue` to `enqueue_notification`.

#### dashboard (package replaces the legacy module)
`dashboard/services.py` becomes `dashboard/services/`:
- `member_projects.py`: `member_project_ids(user)`, `member_project_choices(user)`
  (non-archived).
- `attention.py`: `get_attention(user, project_ids) -> Attention` (help items:
  open `CaseHelpRequest` rows, newest first, limit 10, with `fault` label and
  `auto_fix_state`; plus `get_inbox`; plus `rendered_at`);
  `count_attention(user) -> int` for the badge (member ids + one help count +
  `count_inbox`: 4 queries).
- `dashboard_data.py`: `get_dashboard_data(user) -> DashboardData(has_projects, attention, failed_widget)`.
- `failed_case_filters.py`: `FailedCaseFilters` frozen dataclass and
  `failed_case_queryset(project_ids, filters) -> QuerySet[TestRunTestCase]`
  (status FAILED, scope, filters, `.only(...)`, no large columns). It is the
  single source for list, grouping, export, widget and bulk-action scope.
- `failed_cases.py`: `list_failed_cases(user, filters, page, per_page) -> Page`,
  `failed_case_run_choices(user, project_id)` (runs with a Failed case, `Exists`,
  newest 200).
- `failed_case_groups.py`: `list_failing_test_cases(user, filters, page, per_page) -> Page[FailingCaseRow]`:
  `values("test_case_id", "test_case__title", "test_case__testrail_id", "test_run__project__name")`
  `.annotate(failures=Count("id"), runs=Count("test_run_id", distinct=True), last_failed=Max("finished_at"), latest_pivot_id=Max("id"))`
  ordered by `-failures, -last_failed`. The latest run case is linked by
  `Max("id")`, because pivots are created when cases are added to a run, so the
  highest id is the newest run. The run filter is ignored in this view, since
  grouping a single run makes no sense.
- `failed_case_export.py`: `iter_failed_case_rows(user, filters) -> Iterator[list[str]]`:
  `values_list` with full `result`, `.iterator(chunk_size=1000)`, capped at
  `EXPORT_ROW_LIMIT = 50_000`; cells pass through `auto_tester/csv_safe.py::neutralize_cell`
  (prefixes `'` to values starting with `= + - @ \t \r`, against CSV formula injection).
- `failed_case_bulk.py`: `apply_failed_case_bulk(*, user, action, pivot_ids, links_base_url) -> BulkOutcome`:
  1. Resolve the selected ids through `failed_case_queryset(member ids, no filters)`
     restricted to `id__in` (one query: `id, test_run_id`); foreign or
     non-Failed ids are dropped and counted as skipped.
  2. Load the runs with `select_related("project")` via `in_bulk` (one query).
  3. Per action, delegating to the existing services (no duplicated rules):
     - `check`: `check_run_cases` per run; `RunCaseCheckError` per run is
       collected as a skip.
     - `rerun`: allowed only when every selected case is in one run (a run holds
       the project's machine, so a second run in the same project would be
       refused anyway); calls `rerun_test_run_cases(test_run=, pivot_ids=, user=)`
       keeping the run's help mode; `RunRerunError` becomes the message.
     - `send`: `start_checked_push` once per affected run. This sends every
       checked, unsent case of each run, as the run page does; the message says
       so. Errors per run are collected.
  `BulkOutcome` holds per-run messages built with `format_check_message` /
  `format_rerun_message` and a skip count. Each run is its own transaction (the
  reused services own their transactions and locks); a failure in one run does
  not undo the others, and the outcome says which ones failed.
- `failed_case_presets.py`: `list_presets(user)`, `save_preset(user, name, query)`
  (upsert by name; raises `PresetLimitError` past 20), `delete_preset(user, preset_id)`
  (`PresetNotFoundError`). Exceptions in `dashboard/exceptions.py` (new).
- `failed_case_widget.py`: `get_failed_case_widget(project_ids, now)`: 7-day
  total and not-checked count (1 aggregate), top 5 projects (1), latest 5 (1).
- `machine_health.py`: `get_machine_health(user, project_id: int | None, days: int) -> MachineHealth`:
  - help requests in the window per day x fault: `TruncDate("requested_at")`
    + `values("day", "fault").annotate(Count)` (1 query), pivoted in Python
    into a fixed day grid (blank fault labelled "Agent asked");
  - totals per fault (derived, no extra query);
  - Night auto-fix: requests with `auto_fix_state` in (fixed, not_fixed) per
    state (1 query) giving the success rate, plus count still running;
  - resolution mix: `values("resolution").annotate(Count)` (1 query).
  `days` is one of 7 / 30 / 90. `CaseHelpRequest` has no `requested_at` index;
  the table is small (one row per help event) and the window filter joins through
  the pivot to the run's project, so no index for now (see Risks).
- `dashboard/context_processors.py`: `attention_badge(request) -> dict[str, object]`
  returns `{"attention_count": SimpleLazyObject(...)}` for authenticated users
  only. It is lazy, so pages that never render it (auth pages) make no queries.
  Registered in `TEMPLATES` settings.

`auto_tester/pagination.py` (new request utility, no ORM): `parse_page`,
`parse_per_page`, `ALLOWED_PER_PAGE`, `elided_page_range`, moved from
`projects/views.py` (which then imports them). `auto_tester/csv_safe.py` (pure).

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/` `dashboard:index` | GET | `@login_required` | - | `dashboard/index.html`: `dashboard` (`has_projects`, `attention`, `failed_widget`), `active_nav="dashboard"` | - |
| `/attention/` `dashboard:attention_fragment` | GET | `@login_required` | - | `dashboard/includes/_attention.html` alone (HTML fragment for the live swap), `Cache-Control: no-store` | - |
| `/failures/` `dashboard:failed_cases` | GET | `@login_required`, member non-archived scope | `FailedCaseFilterForm` (GET) incl. `view` (cases / by_case) | `dashboard/failed_cases.html`: `form`, `page_obj`, `elided_page_range`, `per_page`, `allowed_per_page`, `query_params`, `view`, `presets`, `preset_form`, `bulk_form`, `selectable_ids`, `active_nav="failures"` | invalid filters: form errors, the invalid field ignored |
| `/failures/export.csv` `dashboard:failed_cases_export` | GET | same | same filter form | `StreamingHttpResponse`, `text/csv`, `Content-Disposition: attachment; filename="failed-cases-YYYYMMDD.csv"` | invalid form: redirect to `/failures/` with an error message |
| `/failures/bulk/` `dashboard:failed_cases_bulk` | POST | same; scope re-checked per id in the service | `FailedCaseBulkForm` (`action`: check / rerun / send; `pivot_ids`: `TypedMultipleChoiceField`-like int list, max 100; `return_query`) | redirect to `/failures/?<return_query>` with per-run messages | invalid form: error message |
| `/failures/presets/` `dashboard:preset_save` | POST | same | `FailedCasePresetForm` (`name`, `query`) | redirect | `PresetLimitError`: message |
| `/failures/presets/<int:preset_id>/delete/` `dashboard:preset_delete` | POST | own preset | - | redirect | 404 |
| `/insights/machines/` `dashboard:machine_health` | GET | `@login_required` | `MachineHealthFilterForm` (`project` from member choices, `days` 7/30/90) | `dashboard/machine_health.html`: `form`, `health`, `active_nav="insights"` | invalid: defaults |
| `/notifications/inbox/failures/<int:item_id>/dismiss/` `notifications:dismiss_failure` | POST | own row | - | redirect to `dashboard:index` | 404 |
| `/notifications/inbox/replies/<int:item_id>/dismiss/` `notifications:dismiss_reply` | POST | own row | - | redirect | 404 |
| `/notifications/inbox/dismiss-all/` `notifications:dismiss_all` | POST | `@login_required` | `InboxDismissAllForm` (`kind`, `up_to`) | redirect with "N dismissed" | invalid: message |
| `/notifications/` (existing) | GET/POST | existing | `NotificationKindsForm` gains `night_digest` and `run_finished` | existing template, two more checkboxes | - |

`return_query` (bulk) is re-encoded from the cleaned filter form on the server
before redirecting, so it cannot redirect off-site: it is only ever a query
string on a `reverse()`d path.

`FailedCaseFilterForm` (`dashboard/forms.py`), all optional, choices passed in by
the view from services (the form never queries):
- `date_from`, `date_to` (`finished_at`, UTC, `date_to` inclusive); reversed
  range is an error.
- `project` (`TypedChoiceField(coerce=int)`, member choices = authorization).
- `run` (choices for the selected project only; disabled until one is chosen;
  ignored in the by-case view).
- `q` (title `icontains`, or TestRail id for `C123` / `123`).
- `review`: any / not checked / checked, not sent / sent to TestRail.
- `failed_by`: any / AI verdict / marked failed (help) / changed after review /
  not recorded.
- `view`: cases (default) / by_case.

Failures page UI:
- Header: filter card; preset chips (link + delete) and "Save these filters";
  "Export CSV" (same query string); "Cases | By test case" tabs (`osTabs`-like
  links, not JS tabs).
- Cases view: `.data-table` with checkbox column (`bulkSelect`, ids of the page),
  Case (title + TestRail id, link), Project, Run (link), Failed (`finished_at`),
  Failed by (badge), Verdict (200 chars, escaped), Review (Checked / Sent).
  A sticky bulk bar appears when something is selected: Check, Re-run (enabled
  only when the selection is from one run; the service re-checks), Send checked
  to TestRail. Re-run and Send go through `partials/confirm_dialog.html`.
- By test case view: Case, Project, Failures, Runs, Last failed, link to the
  latest failed run case. No bulk actions.

Dashboard:
- Header subtitle: "N items need your attention".
- "Needs your attention" card: groups Needs help (no dismiss; "Help this case";
  fault label; "Auto-fix is trying"), Missed failures and Missed replies (Open,
  Dismiss, "Dismiss all", "and N more"); empty state "Nothing needs you right now".
  The card root is the swap target for live updates.
- "Failed cases" widget: 7-day failed / not checked, top projects (links to
  `/failures/?project=<id>&date_from=<7d>`), latest 5, "View all failures",
  link "Machine health".
- Removed: `_run_summary.html`, `_recent_projects.html`, `_recent_activity.html`
  and `partials/stat_card.html` once a grep confirms no other user.

Nav and badge:
- `partials/nav_links.html`: Dashboard (badge `attention_count` when > 0, with
  `aria-label` "N items need attention"), Projects, Failures (`active_nav="failures"`),
  Insights (`/insights/machines/`, `active_nav="insights"`), Notifications.
  Contract comments in `nav_links.html`, `sidebar.html`, `layouts/app.html` updated.
- `templates/base.html` `<title>`: prefix `(N) ` when `attention_count` > 0. The
  page title block stays the same; the prefix sits outside it.
- `base.html` also renders a `data-attention-count` attribute and
  `x-data="attentionLive"` on the app shell (`layouts/app.html` only, so auth
  pages open no socket).

All model text (verdict, ask, reply preview, titles, preset names) is rendered
escaped; no `|safe`. All POSTs carry `{% csrf_token %}`.

### Background work and WebSockets
- New consumer `dashboard/consumers.AttentionConsumer(AuthenticatedConsumer)`
  at `ws/attention/` (new `dashboard/routing.py`, added to `auto_tester/asgi.py`):
  - `_authorize`: authenticated user; group `attention_user_<user.pk>` (set in
    `_authorize` before the base class adds the group);
  - `_send_initial_state`: nothing (the page already rendered the count);
  - `attention_changed` handler: recomputes `count_attention(user)` (4 queries,
    via `sync_to_async`) and sends `{"type": "attention", "count": N}`. The count
    is computed per socket for its own user, so no user's data reaches another
    user's socket. Bursts are coalesced on the server: the handler skips
    recomputing if it already did within the last 2 s and schedules one trailing
    send. This bounds the cost when a run fails many cases in a row.
  - Client frames are ignored.
- `static/js/punk-hazard.js`: `Alpine.data("attentionLive")` opens
  `punkHazard.wsUrl("/ws/attention/")`, reconnects with backoff, updates the nav
  badge and the `(N) ` title prefix on every message, and on the dashboard
  (when `#attention-card` exists) fetches `/attention/` and replaces the card's
  HTML. The fragment is server-rendered and escaped; the swap replaces the
  element, and Alpine re-initialises it via `Alpine.initTree`.
- Producers of `attention.changed` (all on commit, never raising): missed failure
  recorded (member ids), missed reply recorded (sender), dismiss / dismiss all
  (self, for other tabs), help opened / resolved / cancelled (project members).
- Celery: `notifications.tasks.send_night_digest` on `notify`, routed in
  `CELERY_TASK_ROUTES`, scheduled in `CELERY_BEAT_SCHEDULE` with
  `celery.schedules.crontab(hour=NIGHT_DIGEST_HOUR, minute=0)` (UTC, per
  `CELERY_TIMEZONE`), `expire_seconds` 3600. It reuses `send_notification` for
  delivery.
- No controller actions or protocol changes.

### Settings / infrastructure
- New env vars (read in `settings.py` via decouple, added to `example.env`):
  - `NIGHT_DIGEST_HOUR` (int 0-23, default 7, UTC);
  - `NIGHT_DIGEST_LOOKBACK_HOURS` (int, default 24).
- `TEMPLATES[...]["context_processors"]` gains `dashboard.context_processors.attention_badge`.
- Deploy: `migrate`. `projects.0035` is concurrent and non-atomic; if it fails,
  drop the INVALID index and re-run. Restart Daphne/web, the `execution`
  worker, the `notify` worker and beat (to install the new schedule).

### Data flow
1. Agent verdict Failed → `_finalize_pivot` (sets `failed_by=agent`) →
   `notify_case_failed` → `record_missed_failure` (members) → on commit:
   `attention.changed` to each member + ntfy push if enabled.
2. Person Mark failed / applies a result change to Failed → `failed_by=help/review`.
   No inbox row (D3).
3. Setup turn ends, sender not viewing → `record_missed_reply` → live event →
   optional push.
4. Help opens or closes → `attention.changed` to the project's members → each
   socket recomputes its own count → the badge, the title and (on the dashboard)
   the card update.
5. `/failures/` GET → choices (1-2) + presets (1) → form → list (count + page) or
   grouped (count + page). Export streams the same queryset. Bulk POST → scope
   resolve (1) + runs (1) → existing per-run services → redirect.
6. 07:00 UTC beat → `send_night_digest` → claim Night runs → one aggregate →
   recipients → one push per user.
7. Last case of a run settles → `_update_test_run_status_if_needed(run, generation=)`
   → conditional UPDATE STARTED→DONE (one winner per generation) → on commit
   `notify_run_finished` → counts (1) + recipients (1) → one ntfy batch.

### Dependencies
- `fix-case-duration.md`: both plans edit `projects/services.py::_finalize_pivot`
  (this plan adds `failed_by` and an `update_fields` entry; that plan may change
  `finished_at`/`started_at` handling). Whichever lands second rebases its hunk;
  give that one function to a single sub-agent at a time. The failures page shows
  `finished_at` only, as "Failed" time. It shows no duration, and a duration
  column or metric is left to that plan. If that plan changes what `finished_at`
  means, the date filter and the index here follow it automatically, since they
  filter on the field and do not compute anything from it.
- Run duration (run-finished push and digest): `TestRun.started_at` is set once
  at the first Start, and Resume / Re-run keep it (`run_reopen_services` only
  clears `finished_at`). So `finished_at - started_at` after a re-run spans the
  idle gap, and any run includes the time it sat in Needs help.
  `fix-case-duration.md` is defining the case duration and "average case time
  per run". This plan therefore consumes one function,
  `run_duration(test_run) -> timedelta | None`, and owns no definition of it:
  - if that plan provides it (preferred, e.g. in `projects/run_durations.py`),
    import it;
  - if this plan lands first, add a stub with that exact signature in
    `projects/run_durations.py` returning `finished_at - started_at`
    (None if either is missing), marked in a comment for that plan to replace.
  The message only formats what it gets back, so changing the definition later
  needs no change here.
- `_update_test_run_status_if_needed` / `execute_test_run_test_case` `finally`:
  if `fix-case-duration.md` also changes run-level `finished_at` handling, its
  change must keep the conditional UPDATE as the single write of DONE.
- Migration numbers: this plan claims `projects.0034` / `0035`. If
  `fix-case-duration.md` adds a projects migration first, renumber at
  integration (`makemigrations --check` catches conflicts).

## Alternatives considered
- Computing "missed" on the fly: cannot remember a manual dismiss.
- One generic `InboxItem` table: needs partial unique constraints that the upsert
  cannot target.
- Storing help needs as inbox rows: every resolution path would have to close
  them; reading open `CaseHelpRequest` rows is always correct.
- `/failures/` in `projects`: it is a cross-project, per-user read view like the
  dashboard and shares its member scope; it reads `projects` only via services
  and querysets in the dashboard service layer.
- Live updates sending the rendered list or counts in the event: per-user data on
  a broadcast to many members would need per-recipient rendering on the producer
  (a Celery worker). A payload-free event plus a per-socket recount keeps the
  producer cheap and data private. Sending the HTML over the socket instead of a
  fragment GET would have the consumer render templates; the GET reuses the
  normal view.
- Morning digest as just the run-finished push for Night runs (held until
  morning or sent at once): sending at once is already what run-finished does,
  and holding it needs a send queue anyway. A beat with a claim marker sends one
  summary per user, covers runs still waiting for help, and is independent of
  the DONE event. Q17 lets the user drop either one.
- Run-finished push from a Celery beat sweep of recently finished runs: polling
  delay and its own claim marker; the conditional DONE UPDATE is the natural
  exactly-once point and costs nothing extra.
- Locking the run row (`select_for_update`) in `_update_test_run_status_if_needed`:
  works too, but a conditional UPDATE is one statement, holds no lock across the
  "all settled" check, and also filters stale generations.
- A run-finished inbox row: duplicates the per-case missed failures (Q18).
- Bulk re-run across several runs: a run holds the project's machine, so all but
  one would be refused; restricting to one run gives a clear message up front.
- Backfilling `failed_by` from help requests and result changes: ambiguous after
  re-runs (an old Mark failed and a newer agent verdict on the same case); blank
  "not recorded" is honest.

## Risks and mitigations
- Index creation on a populated pivot table: concurrent, non-atomic migration.
- Inbox, broadcast or digest failing must never break a verdict, a turn or a help
  action: all wrapped "never raises", logged with ids only, run on commit.
- `notify_turn_reply` reorder must keep the push rules identical; verify manually.
- Live event storms (night run failing 100 cases, 5 members, 5 tabs each):
  payload-free events plus a 2 s server-side coalesce bound the recount cost to
  about one per socket every 2 s.
- Context processor cost: 4 small indexed queries per app page, lazy; acceptable.
  If profiling shows otherwise, cache per user for 30 s (follow-up).
- XSS: fragment swap inserts server-escaped HTML only; preset names, verdicts and
  previews escaped; CSV cells neutralised against formula injection.
- IDOR: every list, export, bulk and preset query scoped to member project ids or
  `user=request.user`; bulk re-resolves ids server-side; dismiss scoped by user.
- Bulk actions are partial by nature (per run); the outcome message lists each
  run's result. Re-run and Send ask for confirmation.
- Bulk Send sends all checked cases of the affected runs, not only the selected
  ones (existing service contract); the confirm text says so. See Q10.
- Digest: claim marker prevents double sends; the lookback stops a first-deploy
  flood; topics never leave the worker.
- CSV export of up to 50 000 rows streams with `iterator()`, so memory stays
  bounded; the full `result` text is included (see Q12).
- `failed_by` stale after resets: readers always also filter on `status=FAILED`.
- DONE transition change touches the hot path of every case worker: the
  conditional UPDATE keeps the old semantics (same early returns, same check) and
  only adds `status=STARTED` and the generation to the WHERE. A run that stays
  STARTED because the check raced is not possible: the last worker to settle a
  case always runs the check after its own case is saved. Verify with two
  parallel cases finishing together (manual step 15).
- Run-finished noise on big projects (one push per run per member, plus the
  per-case Failed pushes): separate checkbox; ntfy.sh rate limits as noted in
  the ntfy change note.

## Implementation steps (phases and file ownership)
Phase 1 is done first by one agent; the parts of Phase 2 run in parallel; Phase 3
is integration by the orchestrator. No two parts own the same file.

### Phase 1: shared models, migrations, enums, utilities (one agent)
Files: `notifications/models.py` (+ `MissedReplyOutcome`),
`notifications/migrations/0004_inbox_and_digest.py`, `notifications/admin.py`
(read-only admin for the two inbox models, no topic data),
`notifications/services/notification_message.py` (`NIGHT_DIGEST` kind, delivery,
flag), `notifications/exceptions.py` (`InboxItemNotFoundError`),
`projects/models.py` (`FailedBy`, `failed_by`, `night_digest_sent_at`, index),
`projects/migrations/0034_run_case_failed_by.py`,
`projects/migrations/0035_run_case_status_finished_idx.py`,
`dashboard/models.py`, `dashboard/migrations/0001_initial.py`,
`dashboard/exceptions.py`, `auto_tester/pagination.py`, `auto_tester/csv_safe.py`,
`notifications/services/inbox_broadcast.py` (signatures + implementation; every
part calls it), `machine_setup/services/conversation_url.py`,
`dashboard/services/__init__.py` + `dashboard/services/member_projects.py`
(D, E and F all use it). A module and a package cannot share a name, so Phase 1
moves the old `dashboard/services.py` unchanged to
`dashboard/services/legacy_dashboard.py` and points the import in
`dashboard/views.py` at it. That keeps the tree importable and mypy clean
between phases. Part D deletes `legacy_dashboard.py`.

### Phase 2 (parallel)
- **A. Inbox write + failed_by writes + help hooks** (owner: one agent).
  Files: `notifications/services/record_missed_failure.py`,
  `notifications/services/record_missed_reply.py`,
  `projects/case_failed_notifications.py`,
  `machine_setup/services/turn_reply_notification.py`,
  `projects/services.py` (`_finalize_pivot`, `_pause_run_for_help` after-commit,
  the two cancel updates, the conditional DONE transition in
  `_update_test_run_status_if_needed` and its call site; coordinate both
  `_finalize_pivot` and the DONE transition with `fix-case-duration`),
  `projects/case_help_services.py` (`mark_failed_after_help`, `_resolve`,
  auto-fixed branch), `projects/case_result_change_services.py`.
- **B. Inbox read/dismiss + notifications UI** (owner: one agent).
  Files: `notifications/services/inbox.py`, `notifications/services/dismiss_inbox.py`,
  `notifications/forms.py` (`InboxDismissAllForm`, `night_digest` and
  `run_finished` checkboxes),
  `notifications/views.py`, `notifications/urls.py`,
  `notifications/services/preferences.py` / `preference_view.py` (digest flag),
  `templates/notifications/settings.html`.
- **C. Night digest + run finished** (owner: one agent; the two share the
  duration helper and the message style).
  Files: `notifications/services/night_digest.py`, `notifications/tasks.py`,
  `projects/run_finished_notifications.py`, `projects/run_durations.py` (stub
  only if `fix-case-duration.md` has not provided it), `auto_tester/durations.py`,
  `auto_tester/settings.py` (env vars, route, beat entry, context processor
  registration), `example.env`.
  The DONE transition change in `projects/services.py` belongs to part A, which
  owns that file. A calls `notify_run_finished` with the signature above; C
  implements it.
- **D. Dashboard home, attention, badge, live socket** (owner: one agent; needs
  B's `get_inbox`/`count_inbox` signatures from this plan).
  Files: `dashboard/services/{__init__,member_projects,attention,dashboard_data,failed_case_widget}.py`
  (delete `dashboard/services/legacy_dashboard.py`), `dashboard/context_processors.py`,
  `dashboard/consumers.py`, `dashboard/routing.py`, `auto_tester/asgi.py`,
  `templates/dashboard/index.html`, `templates/dashboard/includes/_attention.html`,
  `templates/dashboard/includes/_failed_widget.html`, removed includes,
  `templates/base.html`, `templates/layouts/app.html`,
  `templates/partials/{nav_links,sidebar,topbar}.html`, `static/js/punk-hazard.js`
  (`attentionLive`).
- **E. Failures page** (owner: one agent; uses D's `member_projects`, so it
  starts after D has written that module or Phase 1 includes it; recommended:
  move `member_projects.py` into Phase 1).
  Files: `dashboard/services/{failed_case_filters,failed_cases,failed_case_groups,failed_case_export,failed_case_bulk,failed_case_presets}.py`,
  `dashboard/forms.py`, `dashboard/failure_views.py` (new module, so it does not
  collide with D's `dashboard/views.py`), `templates/dashboard/failed_cases.html`,
  `templates/dashboard/includes/_failed_case_{table,groups,bulk_bar,presets}.html`,
  `projects/views.py` (switch to `auto_tester/pagination.py` only).
- **F. Machine health page** (owner: one agent).
  Files: `dashboard/services/machine_health.py`, `dashboard/health_views.py`,
  `dashboard/health_forms.py`, `templates/dashboard/machine_health.html`.
- CSS: `static/css/punk-hazard.css` is shared; to avoid two agents editing it,
  D owns it and E/F list the classes they need in their hand-off (badge,
  bulk bar, bar chart cells); the orchestrator adds them in Phase 3 if D has
  finished.

### Phase 3: integration (orchestrator)
`dashboard/urls.py` (index, attention fragment, failures, export, bulk, presets,
machine health), CSS classes requested by E/F, imports and routing seams,
`pre-commit run --all-files`, `manage.py check`, `makemigrations --check`.

## Verification
- `pre-commit run --all-files`, `python manage.py check`,
  `python manage.py makemigrations --check`, `python manage.py sqlmigrate projects 0035`
  shows `CREATE INDEX CONCURRENTLY`.
- Manual (dev DB; users A and B share a project; only A has ntfy on):
  1. Agent fails a case: both get a missed failure; only A gets a push; both
     dashboards and nav badges update without a reload; the tab title shows `(N)`.
  2. A dismisses: gone for A (also in A's second tab), still there for B. Re-run
     and fail again: back for A. Re-run and pass: hidden for B.
  3. Mark failed from help: no inbox row; failures page shows "Marked failed (help)".
     Apply a result change to Failed: "Changed after review".
  4. Setup turn ends while the sender's tab is hidden: missed reply, live badge;
     visible tab: no row; reset thread: row hidden.
  5. Help opens in Attended and Night: appears live for both, "Auto-fix is
     trying" in Night; Continue / Mark blocked / cancel removes it live.
  6. Dismiss all with an older `up_to`: a newer failure stays. POST dismiss on B's
     item as A: 404.
  7. `/failures/`: every filter including `failed_by`; invalid range; foreign
     project id (form error); run of another project dropped; pagination keeps
     filters; By test case view counts match; mobile layout.
  8. Bulk: check across two runs (two messages); re-run with cases from two runs
     (refused with message); re-run from one finished run (starts); send for a run
     without TestRail settings (error collected); a forged pivot id from a project
     A is not in (skipped).
  9. CSV: filtered export matches the page; a title starting with `=` is
     prefixed with `'`; opens in a spreadsheet.
  10. Presets: save, apply, delete; 21st refused; preset with a project A has left
      shows the form error and lists unfiltered by project.
  11. `/insights/machines/`: counts per fault and day match `CaseHelpRequest` rows;
      auto-fix rate matches fixed / (fixed + not fixed).
  12. Digest: set `NIGHT_DIGEST_HOUR` to the next hour (or call the service in
      `manage.py shell` against a throwaway DB); one push per user summarising
      the Night runs; a second call sends nothing; a user with the digest
      checkbox off gets none.
  13. Query counts with `connection.queries`: dashboard, `/failures/`, by-case,
      health and the context processor stay constant for 1 vs 20 projects/items.
  14. A user who leaves a project stops seeing its inbox rows, failures, presets'
      project filter and health data.
  15. Run finished: a two-case run whose cases finish close together (or two
      concurrent `manage.py shell` calls of `_update_test_run_status_if_needed`
      on a throwaway DB) produces exactly one DONE write and one push; the
      message shows the counts and duration; Re-run failed then finish sends a
      second push; a cancelled run sends none; a run paused for help sends none
      until it finishes; the checkbox off means no push for that user.
- Run `reviewer` and `verifier` in parallel on the diff.

## Open questions
- Q10 Bulk "Send to TestRail": the existing service sends every checked, unsent
  case of each affected run, not only the selected ones. Is that acceptable, or
  should Send first check the selected cases and then send ("Check and send")?
  (Assumption: send as the run page does; the confirm dialog says so; no implicit
  check.)
- Q11 Morning digest: which hour and time zone, and what does it cover? Should a
  Night run still waiting for help be included once ("1 needs help"), and should
  Night runs that finished in the daytime be included in the next morning's
  digest? (Assumption: 07:00 UTC via `NIGHT_DIGEST_HOUR`; Night runs that
  started in the last 24 h and are Done or waiting for help; each run digested
  once.)
- Q12 CSV export: include the full verdict text and the run case log, or the
  verdict only? (Assumption: full verdict, no logs; at most 50 000 rows.)
- Q13 Live updates: open the attention socket on every app page (badge and title
  live everywhere), or only on the dashboard? (Assumption: every app page, one
  socket per tab.)
- Q14 Machine health: who should see it? The main nav ("Insights") or only a link
  from the dashboard widget? Is a per-project breakdown table needed besides the
  project filter? (Assumption: nav link; project filter only; no per-project
  table.)
- Q15 Bulk re-run help mode: keep each run's current help mode, or offer the
  Attended / Night choice in the bulk bar as the run page does? (Assumption: keep
  the run's mode.)
- Q16 `failed_by` for rows that existed before deploy: show "Not recorded", or
  backfill a best guess (Mark failed help requests → help, applied result changes
  → review, otherwise AI verdict)? (Assumption: "Not recorded", no backfill.)
- Q17 Morning digest vs run-finished push: keep both (the run-finished push for
  every run as it finishes, Night runs included, and a separate 07:00 summary of
  the night's runs, each with its own checkbox)? Or make the digest replace the
  run-finished push for Night runs (no push at 3 a.m.; Night runs are only
  reported in the morning)? Or drop the digest now that run-finished exists?
  (Assumption: keep both, separate checkboxes, both on by default.)
- Q18 Should a finished run also appear in the dashboard's Needs your attention
  list, and if so for which runs (all, or only those with failures / blocked
  cases)? (Assumption: no entry; its failed cases already appear one by one as
  missed failures.)
- Q19 Should a Re-run or Resume that finishes send its own run-finished push?
  And should the push be on by default for existing users? (Assumption: yes to
  both; the counts cover the whole run, not only the re-run cases.)
- Q20 What should "run duration" mean in the message: wall time from Start to
  Done, time spent running cases (no Needs help pauses, no idle gap before a
  Re-run), or whatever `fix-case-duration.md` defines? (Assumption: whatever
  that plan's `run_duration` returns; until it exists, Start to Done wall time.)

## Changelog
- 2026-10-28: created (Draft).
- 2026-10-28: Q1-Q9 resolved with the draft defaults (now D1-D9). Scope extended
  with: nav/tab attention badge, live attention socket, by-test-case grouping,
  machine health page, `failed_by` field and filter, bulk check / re-run / send
  (reusing the per-run services), CSV export, morning Night digest (beat with a
  claim marker instead of changing the DONE transition), saved filter presets.
  Plan reorganised into phases with file ownership. New open questions Q10-Q16.
  Noted the `_finalize_pivot` overlap with `fix-case-duration.md`. Status stays
  Draft until Q10-Q16 are answered.
- 2026-10-28: added the run-finished push (`RUN_FINISHED` kind,
  `notify_run_finished` checkbox, counts + duration) and its shared prerequisite:
  the DONE transition becomes a conditional UPDATE filtered on STARTED and the
  generation, so a run finishes exactly once per generation. The digest stays a
  separate summary with its own claim marker. No inbox entry for finished runs.
  Run duration comes from a `run_duration` function that `fix-case-duration.md`
  is expected to provide; a stub is added if this plan lands first. New open
  questions Q17-Q20.

- 2026-10-28: User accepted the default (Assumption) answer to every open question. Implementation started on branch feature/failures-inbox-duration. Migration order across both plans: projects.0034 failed_by + night_digest_sent_at, 0035 concurrent status/finished_at index, 0036 case active_duration fields, 0037 backfill. One shared duration formatter: auto_tester/durations.py (format_duration); projects/run_durations.run_duration stays Start-to-Done wall clock (duration Q6 default).
- 2026-10-28 review round 1, findings not changed:
  - Pagination helpers moved to `auto_tester/pagination.py` (verifier: unrelated): kept, planned in Phase 1 and reused by the failures page.
  - New read-only admin views for the inbox models and duration fields (verifier: unasked): kept, read-only and harmless.
  - Trailing blank line removed in `projects/case_result_change_services.py`: black formatting, kept.
