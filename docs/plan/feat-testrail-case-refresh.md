# feat: Refresh a single test case from TestRail

Status: Done
Created: 2026-10-30
Request: "Ability to refresh a single test from testrails." Today the only way to
pick up a case edited in TestRail is to re-import its whole suite or run. A
writable member should be able to press "Refresh from TestRail" on one test case
and have Punk Hazard fetch that case from TestRail now and overwrite the local
copy's text fields (title, template, type, priority, estimate, references,
preconditions, steps, expected), with a message saying what changed.

## Context

- `projects/testrail_client.py`: typed client. Has `iter_cases`,
  `iter_run_cases`, `get_case_types`, `get_priorities`, `get_run`, a 429 retry
  loop (`_request_with_rate_limit_retry`, up to `TESTRAIL_MAX_RATE_LIMIT_RETRIES`
  = 3 sleeps of up to 30 s each), `_parse_case` / `_case_from_fields`. No
  `get_case`. `TestRailCase` has no `suite_id`. `TestRailError.status_code` 0
  means no HTTP response.
- `projects/testrail_mapping.py`: `TestRailFieldMapper.from_vocabularies(...)`
  and `to_test_case_data(case) -> TestCaseData` (flattens "Test Case (Steps)"
  separated steps, truncates title/refs/estimate, maps type/priority by name);
  `parse_testrail_case_id` accepts "123" and "C123".
- `projects/testrail_services.py`: `build_testrail_client` (raises
  `TestRailNotConfiguredError`), private `_apply_data` (also stamps
  `updated_at`) and `_UPSERT_FIELDS`, `_ORDER_FIELDS`; `_upsert_batch` updates
  **every** row of the project with the same `testrail_id` string; Redis cache
  helpers for the TestRail project picker, invalidated by
  `_invalidate_picker_cache` on settings save/clear.
- `projects/testrail_run_import_services._fetch_run`: precedent for a
  synchronous TestRail call in a request (maps 400/404 to "not found", 401 to a
  credentials message, everything else re-raised as `TestRailError` whose
  `message` the view shows).
- `projects/models.py`: `TestCase` (text fields above, `testrail_id`,
  `testrail_suite_id` / `testrail_position` written by imports only,
  `testrail_source_run_id`). `TestRun.test_cases` M2M through
  `TestRunTestCase`; **no per-run snapshot of the case text**.
  `ACTIVE_RUN_STATUSES = (STARTED, PAUSED)`, `FINISHED_CASE_STATUSES`.
- What reads the live case text:
  - `projects/services.py` worker (~l. 3127): `build_task_description(pivot.test_case)`
    and `numbered_case_step_count` are read at the start of **every** execution
    of a pivot, including resume / help Continue / person-step confirmation.
  - `agents/services/orchestrator.py`: on a resume it keeps the checkpoint's
    plan and the checkpoint's `case_step_count` / `case_steps_testrail`, but the
    step executor and verdict get the *new* `task_description`.
  - `projects/case_step_refs.case_step_rows`: stores covered step texts on a new
    `CasePersonStep` only when the live case's numbered step count equals the
    plan's (`case_step_count` stale guard); a same-count reword shows new text.
  - Ask AI (`person_step_question_services`, l. 412), help and result chat
    briefs (`machine_setup/services/case_help_context.py`,
    `case_result_context.py`, `setup_tools.get_case`), notifications and
    dashboard titles all read the live row.
  - Run case page "Test case" card and run page rows display the live text,
    also for finished runs.
- Existing behaviour when a case changes under a run:
  - Suite and run-link imports (`tasks._run_testrail_import`,
    `testrail_run_import_services.import_testrail_run_cases`) overwrite rows in
    place with **no** active-run guard.
  - Test Cases list "Edit" (`views.test_case_edit`) has no guard either.
  - Run case "Edit test case" (`views.test_run_case_edit`) is refused only while
    *that* run is active (`services.can_edit_test_case_in_run`), not other runs.
- UI surfaces:
  - Test Cases list: `templates/projects/test_cases.html` +
    `includes/_test_case_table.html` row menu (Edit / Delete), per-row dialogs
    `_test_case_edit_dialog.html`, `_test_case_delete_dialog.html` (action URL
    rewritten from the row id). There is **no test case detail page** (title
    links to `#`).
  - Run case page: `views.test_run_case_detail` +
    `templates/projects/test_run_case_detail.html`: header actions (Check, Send
    to TestRail, Edit test case, Remove, Back) and the "Test case" card whose
    header holds "Open in TestRail" (being edited by other in-flight work; do not
    touch those lines). Context already has `testrail_case_url`.
  - Run detail page (`test_run_detail.html`) has its own edit dialog; not touched.
- Throttles: cache counter pattern in
  `person_step_question_services._check_throttle` and
  `project_member_services._check_invite_throttle` (`cache.add` + `cache.incr`).
- Versions: Django ~5.2.11, httpx ~0.28.1, django-redis ~6.0.0, celery ~5.6.2.
  TestRail API v2 `GET index.php?/api/v2/get_case/{case_id}` returns one case
  object (same fields as `get_cases` items, including `suite_id`,
  `section_id`, `custom_steps_separated`); per TestRail's error-handling docs,
  400 = entity does not exist (also a deleted case), 403 = no permission,
  401 = bad credentials, 429 = rate limited (with `Retry-After`).
- nginx `proxy_read_timeout 120` for the app.

## Goals

- One-click "Refresh from TestRail" for a single test case that has a parseable
  TestRail id, on the Test Cases list and on the run case page.
- Reuse the import mapping and field write path exactly (same result as a
  re-import of that case's suite would give for the text fields).
- Clear outcome message: which fields changed, or already up to date; clear
  error messages for every failure.
- Never corrupt an in-flight execution (saved plan vs. case text mismatch).

## Non-goals

- Bulk "Refresh selected" (follow-up; see Alternatives).
- Per-run snapshots of case text (finished runs keep showing the live text, as
  after any import or edit today).
- Changing TestRail order fields, `testrail_source_run_id` or `testrail_id`.
- Creating cases from TestRail, or an import-history row for a refresh.
- Changes to the run detail page, the "Open in TestRail" link, or TestRail
  result comments.
- Any controller protocol, WebSocket or migration change.

## Design

### Data model

No change, no migration. The refresh writes the nine `TestCaseData` text fields
plus `updated_at` (via the shared apply function). Not written:

- `testrail_id`: kept as stored. An XML-era "C123" stays "C123"; the
  "C123" vs "123" non-dedup is the accepted import behaviour.
- `testrail_suite_id` / `testrail_position`: kept. A single `get_case` cannot
  rank a case within its suite. If TestRail's `suite_id` differs from the
  stored non-null `testrail_suite_id` (the case moved to another suite), the
  message says so and suggests re-importing that suite to fix the order. The
  stored value is not overwritten, because the position is still the old
  suite's.
- `testrail_source_run_id`: kept. Refresh is about case content, not which run
  to send results to.

**Rows sharing a `testrail_id`**: the refresh updates every `TestCase` of the
project whose `testrail_id` equals the clicked row's stored value exactly. This
is the same rule `_upsert_batch` uses, so the next import cannot "un-refresh" a
sibling, and two copies of the same TestRail case never diverge silently. The
message names the count when more than one row changed (Open question 4).

### Services

New module `projects/testrail_case_refresh_services.py` (one use case; the
existing flat `projects/testrail_*_services.py` convention):

- `can_refresh_from_testrail(project: Project, test_case: TestCase) -> bool`:
  pure, no query. True when `project.has_testrail_settings` is set and
  `parse_testrail_case_id(test_case.testrail_id)` is not None. Used by the
  views to build template context.
- `refresh_test_case_from_testrail(*, project: Project, test_case: TestCase) -> TestCaseRefreshOutcome`.
  Order of work:
  1. Configuration and id: `TestRailNotConfiguredError` without settings;
     `TestCaseNoTestRailIdError` when the id does not parse.
  2. Throttle: per project, `REFRESH_THROTTLE_LIMIT = 30` per
     `REFRESH_THROTTLE_WINDOW_SECONDS = 60` (cache counter, same pattern as
     `person_step_question_services._check_throttle`). Raises
     `TestCaseRefreshThrottledError`. Per project rather than per user,
     because the TestRail API budget is per TestRail account, which is per
     project here.
  3. In-flight guard (cheap, before calling TestRail): one `EXISTS` query for a
     `TestRunTestCase` whose `test_case` is in the sibling set (same project,
     same `testrail_id`), whose `test_run.status` is in `ACTIVE_RUN_STATUSES`,
     and whose `status` is not in `FINISHED_CASE_STATUSES` (that is
     CREATED / IN_PROGRESS / NEED_HELP / AWAITING_PERSON in a Running or
     Needs-help run). Raises `TestCaseRefreshBusyError` (Open question 1).
  4. Fetch: `build_testrail_client(project, max_rate_limit_retries=0)` and
     `client.get_case(case_id)`. Get the mapper from
     `testrail_services.get_testrail_field_mapper(project, client)` (cached
     vocabularies, see below). Map `TestRailError` by `status_code`:
     - 400 / 404 → `TestRailCaseNotFoundError` ("C123 does not exist in TestRail
       any more, or this account cannot see it.")
     - 401 → credentials message (same wording as the run import; lift
       `_CREDENTIALS_MESSAGE` to one shared constant or duplicate the one
       sentence; implementer's choice, keep it a single constant if moved)
     - 403 → "The TestRail account has no access to C123."
     - 429 → "TestRail is rate limiting requests. Try again in a minute."
     - 0 → "Could not reach TestRail. Try again in a moment."
     - other → "TestRail returned an error: <exc.message>"
     All of these are raised as `TestCaseRefreshError` subclasses / the base
     with a safe `message`. Credentials and API keys never appear in messages
     or logs. Log the status code and project id only.
  5. Write: `data = mapper.to_test_case_data(case)`. Then, inside
     `transaction.atomic()`:
     - `select_for_update()` the sibling rows (one query).
     - Re-run the in-flight guard; a run may have been resumed while TestRail
       answered.
     - Compute `changed_fields` per row by comparing the row with `data` on
       `TESTRAIL_CASE_FIELDS`.
     - Apply `apply_testrail_case_data` only to rows that differ.
     - `bulk_update(rows, [*TESTRAIL_CASE_FIELDS, "updated_at"])` when any
       row changed.

     No write and no `updated_at` bump when nothing changed.
  6. Return `TestCaseRefreshOutcome` (frozen dataclass):
     - `testrail_case_id: int`
     - `changed_fields: tuple[str, ...]`, the union in `TESTRAIL_CASE_FIELDS`
       order
     - `rows_updated: int`
     - `suite_moved: bool`
- `format_refresh_message(outcome: TestCaseRefreshOutcome) -> str`, pure. Field
  labels: Title, Template, Type, Priority, Estimate, References, Preconditions,
  Steps, Expected result. Examples:
  - "Refreshed C123 from TestRail: title and steps changed."
  - "Refreshed C123 from TestRail in 2 test cases: steps changed."
  - "C123 is already up to date with TestRail."
  - suffix " C123 is now in another TestRail suite; re-import that suite to
    update its order."

  Expected query count for a refresh: view lookup 1, guard 1, lock 1, guard
  again 1, `bulk_update` 1 (0 when unchanged). TestRail calls: 1 with a warm
  vocabulary cache, 3 when it is cold.

Changes to `projects/testrail_services.py`:

- Rename `_apply_data` → `apply_testrail_case_data` and `_UPSERT_FIELDS` →
  `TESTRAIL_CASE_FIELDS` (public; `_upsert_batch` updated to the new names).
  This keeps one write path for imports and refresh.
- `build_testrail_client(project, *, max_rate_limit_retries: int = TESTRAIL_MAX_RATE_LIMIT_RETRIES) -> TestRailClient`
  passes the value through. Existing callers are unchanged.
- `get_testrail_field_mapper(project: Project, client: TestRailClient) -> TestRailFieldMapper`:
  - Caches `{type_id: name}` / `{priority_id: name}` under
    `testrail_vocab:<project_id>` for `TESTRAIL_VOCAB_CACHE_TTL = 3600` s.
  - Reads the cache defensively (same pattern as `_fetch_testrail_projects`).
  - On a miss it calls `get_case_types` + `get_priorities`.
  - `_invalidate_picker_cache` also deletes the key, so changing credentials
    or URL drops it.

  Imports keep calling the vocabularies directly; switching them is optional
  and not part of this plan.

Changes to `projects/testrail_client.py`:

- `TestRailCase.suite_id: int | None`, parsed leniently with `_order_int`
  (present in `get_case`/`get_cases`; tests from `get_tests` lack it → None).
  Only `_case_from_fields` constructs `TestRailCase`, so this is additive.
- `get_case(case_id: int) -> TestRailCase` → `_parse_case(self._get_dict(f"get_case/{case_id}"))`.
- `TestRailClient.__init__(..., max_rate_limit_retries: int = TESTRAIL_MAX_RATE_LIMIT_RETRIES)`,
  used by `_request_with_rate_limit_retry` instead of the module constant.

New exceptions in `projects/exceptions.py`:

- `TestCaseRefreshError(Exception)`, with a `message` attribute (same shape as
  `TestRailRunImportError`).
- Subclasses: `TestCaseNoTestRailIdError`, `TestRailCaseNotFoundError`,
  `TestCaseRefreshBusyError`, `TestCaseRefreshThrottledError`.

`TestRailNotConfiguredError` stays where it is (`testrail_services`) and is
reused.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/projects/<project_id>/test-cases/<test_case_id>/refresh-testrail/` name `projects:test_case_refresh_testrail` | POST (`@require_POST`) | `@project_membership_required` (read-only → 403); case looked up with `get_test_case_for_project` (other project → 404) | `TestCaseRefreshForm(next: CharField, hidden, optional, max 2000)` | Redirect to `next` if same-host (`testrail_views._safe_next`), else `projects:test_case_list` | `messages.error(exc.message)` for `TestRailNotConfiguredError`, `TestCaseRefreshError` (all subclasses); success → `messages.success(format_refresh_message(...))`; "already up to date" → `messages.info` |

The view lives in `projects/testrail_views.py`, next to the other TestRail
actions. `views.py` is already 1,680 lines. The view is thin: validate the
form, look up the case, call the service, and map errors.

Test Cases list (`views.test_case_list` → `test_cases.html`):

- New context keys:
  - `testrail_configured` (`project.has_testrail_settings`, no query)
  - `refresh_next` (`request.get_full_path()`, so filters and page survive the
    redirect)
- `_test_case_table.html` takes `testrail_configured`. Its row menu gets
  "Refresh from TestRail" between Edit and Delete when `can_write`,
  `testrail_configured` and `tc.testrail_id` are all set. The menu item
  dispatches `open-refresh-case` with the case id and testrail id.
  Unparseable ids are rare (manual text) and get the service's error message.
  That avoids a template filter.
- New `templates/projects/includes/_test_case_refresh_dialog.html`: one
  alertdialog shared by all rows (same shape as `_test_case_delete_dialog.html`:
  `x-data="{ ...dialog(), caseId: 0, label: '' }"`, action rewritten from the
  id, hidden `next`).
  - Body: "Replace this case's title, steps and other fields with the current
    version from TestRail? Local edits to this case are lost. Runs that include
    this case show the new text; a Resume continues the earlier plan, use
    Re-run to plan from the new text."
  - The label is bound with `x-text`, never `x-html`.
  - Contract comment at the top.
- Included in `test_cases.html` inside `{% if project_can_write %}`.

Run case page (`views.test_run_case_detail` → `test_run_case_detail.html`):

- New context key `can_refresh_testrail` =
  `can_refresh_from_testrail(project, pivot.test_case)`.
- One button in the **header actions** (next to "Edit test case"), not in the
  "Test case" card header. Other in-flight work edits the "Open in TestRail"
  link there.
  - Shown when `project_can_write and can_refresh_testrail`.
  - Shown in every run status; the service refuses in-flight cases with a
    clear message, which also covers the same case in another active run.
- The button opens a `partials/confirm_dialog.html` include:
  - `dialog_id="refresh-case"`
  - `action_url` = refresh URL for `pivot.test_case.id`
  - `next=case_detail_url`
  - `confirm_class="btn-primary"`
  - Same body text as on the list page.
- Update the template's context contract comment.

### Background work and WebSockets

None. The refresh runs synchronously in the request (see Alternatives). There is
no Celery task, no consumer and no controller action. A running case already
on the page picks up nothing live; the redirect reloads the page.

### Settings / infrastructure

None. The throttle limit, the vocabulary TTL and the zero-retry choice are
module constants, not env vars, matching `QUESTION_THROTTLE_LIMIT`. No change
to `example.env`.

### Data flow

1. User presses Refresh from TestRail (list row menu or run case header) and
   confirms; the browser POSTs `next` + CSRF.
2. `@project_membership_required` resolves the project and refuses read-only
   members. The view validates `TestCaseRefreshForm` and loads the case
   (`get_test_case_for_project`, 404 otherwise).
3. `refresh_test_case_from_testrail` runs: settings and id check, throttle,
   in-flight guard, `get_case` (plus vocabularies on a cold cache) without 429
   sleeps, mapping, then a locked re-check and `bulk_update` of the changed
   sibling rows.
4. The view adds a success, info or error message and redirects to `next`.
5. From then on, every reader of the live row sees the new text: run pages,
   chats, Ask AI, the next execution of a not-yet-started case, Re-run and new
   runs.

## Alternatives considered

- **Celery task on `upload` (with a history row and live progress)**: rejected.
  A refresh is one, or at most three, small GETs. A task needs pending state,
  a refresh-in-progress UI and a way to report the result back (socket or
  polling). The run-link import already makes a synchronous `get_run` call in
  the request, so there is precedent. The real risk with a synchronous call is
  the client's 429 retry loop sleeping up to 90 s. Passing
  `max_rate_limit_retries=0` for this call removes that risk; worst case is
  3 calls × the 30 s client timeout, still under nginx's 120 s, and in
  practice it is one fast call with a warm cache.
- **Refresh via `get_cases` of the suite, filtered**: rejected. It costs
  pages of data for one case.
- **Write the refresh through `upsert_test_cases_from_testrail`**: rejected. It
  needs an upload and always writes the order fields, which would clear
  `testrail_position` because `positions` is empty for one case. Reusing only
  `apply_testrail_case_data` and `TESTRAIL_CASE_FIELDS` keeps a single field
  write path without that side effect.
- **No vocabulary cache (3 calls every time)**: simpler but triples TestRail
  load for a repeated action. The cache is about 20 lines next to the existing
  picker cache, and settings changes already invalidate it.
- **Per-run snapshot of case text**: correct for history but a large change
  (migration, every reader switched). Not needed for this request, and imports
  have the same behaviour today.
- **Bulk "Refresh selected"**: natural follow-up for the list bulk bar. It
  needs batching against TestRail (N calls) and therefore a Celery task. Out
  of scope here.
- **Only the clicked row**: rejected in favour of matching the import's
  sibling rule (Open question 4).

## Risks and mitigations

- **Mid-execution mismatch** (saved plan from the old text, executor and verdict
  with the new text; `case_step_count` guard drops person-step step texts when
  the count changed): refused for cases that are not finished in an active run
  (the guard is checked twice, the second time under row locks). There is a
  small race left: a Start or Resume can commit between the locked re-check and
  the worker reading the text. The worker reads the text when it starts
  executing, and a case whose execution has not started has no plan yet, so
  the race is harmless.
- **Finished cases then Resumed**: Resume continues the earlier plan against the
  new text. The confirm dialog says to use Re-run instead. The same thing
  happens after an import or Edit today.
- **Finished runs display the new text** (no snapshot): stated in the dialog;
  imports behave the same way (Open question 2).
- **Local edits overwritten**: a confirm dialog in front of the action.
  Unchanged cases are not written.
- **TestRail load / 429**: per-project throttle (30/min), zero retries, cached
  vocabularies.
- **Secrets**: the API key is only used inside the client. Messages contain the
  case id and TestRail's error text, never credentials. The base URL is the
  stored project URL (no user-supplied host, no SSRF surface).
- **XSS**: titles and ids are rendered with auto-escape, and Alpine binds use
  `x-text`; no `|safe`.
- **N+1**: none. There is a fixed number of queries per refresh, and the list
  page adds no queries (`has_testrail_settings` is a property).
- **Renaming `_apply_data` / `_UPSERT_FIELDS`**: private, only used in
  `testrail_services.py`. Check with `grep` that nothing else imports them.

## Implementation steps

Steps 1–3 touch different files and can run in parallel. The interfaces are
fixed above. Step 4 depends on 1–3. Steps 5 and 6 touch different files and
can run in parallel after 4. Each step is one commit.

1. **Client**: `TestRailCase.suite_id`, `get_case`, and the
   `max_rate_limit_retries` constructor argument.
   (files: `projects/testrail_client.py`)
2. **Shared TestRail services**: public `apply_testrail_case_data` /
   `TESTRAIL_CASE_FIELDS` (rename + update `_upsert_batch`),
   `build_testrail_client(..., max_rate_limit_retries=)`,
   `get_testrail_field_mapper` with the vocabulary cache, and the extra key in
   `_invalidate_picker_cache`.
   (files: `projects/testrail_services.py`)
3. **Domain exceptions**: `TestCaseRefreshError` and its four subclasses.
   (files: `projects/exceptions.py`)
4. **Refresh service**: `can_refresh_from_testrail`,
   `refresh_test_case_from_testrail`, `TestCaseRefreshOutcome`,
   `format_refresh_message`, throttle, guard, error mapping.
   (files: `projects/testrail_case_refresh_services.py`, new)
5. **Endpoint**: `TestCaseRefreshForm`, view `test_case_refresh_testrail`, URL.
   (files: `projects/forms.py`, `projects/testrail_views.py`, `projects/urls.py`)
6. **UI**: list context (`testrail_configured`, `refresh_next`) and run case
   context (`can_refresh_testrail`); table row menu item; new refresh dialog
   include; list page include and contract comment; run case header button,
   confirm dialog include and contract comment.
   (files: `projects/views.py` (`test_case_list`, `test_run_case_detail` only),
   `templates/projects/includes/_test_case_table.html`,
   `templates/projects/includes/_test_case_refresh_dialog.html` (new),
   `templates/projects/test_cases.html`,
   `templates/projects/test_run_case_detail.html` (header actions + dialog
   include only; do not touch the "Test case" card header))
7. **Docs**: `docs/changed/2026-10-30-testrail-case-refresh.md`; set this
   plan's status to Done. (files: those two)

## Verification

Static checks:

- `pre-commit run --all-files` (black, isort, strict mypy).
- `python manage.py check`.
- `python manage.py makemigrations --check --dry-run` must report no changes.
- `grep -rn "_apply_data\|_UPSERT_FIELDS"` must return nothing outside the
  renamed definitions.

Manual checks (dev server, a project with TestRail settings):

1. Edit a case's steps in TestRail. Refresh it from the list row menu. Expect
   "Refreshed C… from TestRail: steps changed.", the list page with the same
   filters and page, and the new steps in the Edit dialog. Press again:
   "already up to date", and `updated_at` unchanged.
2. Run case page of a finished run: the header button refreshes, and the
   "Test case" card shows the new text after the redirect.
3. Two rows with the same `testrail_id` (copy via a second run import or a
   manual create with the same id): both update, and the message says
   "in 2 test cases".
4. Case in a Running run, not yet finished (or Need help / Your turn): refused
   with the busy message; nothing is written. After the run finishes, the
   refresh works.
5. Error paths:
   - Remove the TestRail settings: not-configured message, and the menu item
     and button are hidden.
   - Set a case's `testrail_id` to "abc": the no-id message.
   - Delete the case in TestRail (or use a non-existent id): the not-found
     message.
   - Use a wrong API key: the credentials message.
   - Unreachable URL (stop the network / bad host): the "Could not reach"
     message, returned quickly.
6. Read-only member: no menu item or button. A crafted POST answers 403. A
   POST for a case id from another project answers 404. A `next` pointing at
   another host redirects to the list.
7. Send 31 refreshes in a minute: the 31st gets the throttle message.
8. Import a suite after a refresh: the position and suite fields are still set,
   and the text is identical (same mapper).

Then run the `reviewer` and `verifier` agents in parallel, with the original
request and this plan path.

## Open questions

- **Q1. A case that is in a run still in progress: refuse, warn, or allow?**
  The saved plan was made from the old text, and Continue / Resume / person
  confirmation would run it against the new text.
  - (a) Refuse while the case is not finished in a Running / Needs-help run
    (CREATED, IN_PROGRESS, NEED_HELP, AWAITING_PERSON). Allow otherwise.
  - (b) Allow always, like imports and the list Edit today, with a warning.
  - (c) Refuse whenever the case is in any active run, even when its pivot
    there is finished.

  Recommendation: (a).
  Assumption used in this draft: (a). Note that it also refuses a case that
  has not started yet in a Running run. Doing so is simpler and safe, but such
  a case could technically be refreshed.

- **Q2. Finished runs show the refreshed text, because there is no per-run
  snapshot. Is that acceptable?**
  - (a) Accept, and say so in the confirm dialog. This matches imports and
    Edit.
  - (b) Add a per-run snapshot of the case text (migration and every reader;
    a separate plan).

  Recommendation: (a).
  Assumption used in this draft: (a).

- **Q3. Where should the action appear?**
  - (a) Test Cases list row menu, plus the run case page header.
  - (b) List only.
  - (c) Also the run detail page rows.

  Recommendation: (a). There is no test case detail page, and the run case
  page is where a tester notices outdated text. (c) adds a third dialog to an
  already crowded page, so leave it for later.
  Assumption used in this draft: (a).

- **Q4. When several test cases in the project share the same TestRail id,
  update only the clicked one, or all of them?**
  - (a) All rows with exactly the same stored `testrail_id`, as an import
    does.
  - (b) Only the clicked row.

  Recommendation: (a). Otherwise the next import overwrites the siblings
  anyway, and copies diverge silently. "C123" and "123" are still not treated
  as the same, which is the accepted import behaviour.
  Assumption used in this draft: (a).

- **Q5. Bulk "Refresh selected" from the list's bulk bar?**
  - (a) Out of scope now; follow-up plan (Celery `upload` task, since it is
    N TestRail calls).
  - (b) Include now.

  Recommendation: (a).
  Assumption used in this draft: (a).

## Changelog

- 2026-10-30: created (Draft).
- 2026-10-30: user chose the recommendation on Q1–Q5 (all (a)); Ready, then In progress.
- 2026-10-30: the run case page reuses `_test_case_refresh_dialog.html` (one body text, one place) instead of `partials/confirm_dialog.html`.
- 2026-10-30: review round 1 fixes: the vocabulary cache is refetched when the case uses a type/priority id it lacks (a renamed name with the same id still waits for the TTL or a settings save; accepted); throttle counted after the busy check; docstring no longer claims full serialisation with run Start/Resume (they lock runs, not cases; window is milliseconds; accepted); Resume caveat restored in the dialog; run case dialog include gated on `project_can_write`.
- 2026-10-30: rejected: list rows offer Refresh for any non-empty `testrail_id` rather than only parseable ones (an unparseable id gets the clear service message; a per-row parse would need a template filter or annotation); Refresh stays visible on the run case page while the run is active (the refusal is per case, finished cases there may be refreshed).
