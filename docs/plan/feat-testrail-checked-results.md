# feat: Mark run cases as Checked and send checked results to their source TestRail run

Status: Done
Created: 2026-10-22
Request: A human reviewer must be able to mark a run case's AI result as **Checked**. They can do it in bulk on the run page (select several cases, then click Check) or one case at a time on the run's case detail page. The run page also gets a **Send checked to TestRail** button. It adds a result to the **source** TestRail run (the run the case was imported from with a run link) for every checked case that has not been sent yet. Each result carries the case's screenshots as attachments. Cases with no source TestRail run cannot be sent; mixed runs send only the cases that can be sent and report the rest. This combines the user's two messages: the second replaced "select cases and send" with "check, then send the checked ones".

## Context

- **Whole-run push (spec/plan 018).**
  - `projects/testrail_results_services.py`, `projects/tasks.py::push_test_run_results` (queue `upload`), `projects/testrail_views.py::testrail_push_start/_suite`, `templates/projects/test_run_detail.html`, `includes/_testrail_push_dialog.html`.
  - It **creates a new TestRail run** with `add_run`, stores it in `TestRun.testrail_run_id`, and pushes every Passed/Failed/Blocked pivot.
  - It dedupes per pivot with `testrail_result_id`, `testrail_pushed_status_id`, `testrail_pushed_comment_hash` and `testrail_pushed_at`. Those four fields describe *that created run*.
  - It reports run-level status with `TestRun.testrail_push_status/_pushed_at/_push_error/_push_summary` and the WebSocket event `test_run.testrail_push` → `{"type": "testrail_push"}`. `reset_test_run` keeps the per-pivot tracking.
  - Screenshots and pushing into an existing run were non-goals.
- **Run-link import** (`projects/testrail_run_import_services.py`, `docs/changed/2026-10-22-testrail-run-import.md`):
  - It stores the run id only on the history row, `TestCaseUpload.testrail_run_id`.
  - `testrail_services._upsert_batch` creates new cases with `upload=<this upload>`. Existing cases are updated **without changing `upload`** (`projects/testrail_services.py:388-426`).
  - So `test_case.upload` is the *first* import that created the case. Re-importing the same cases from the next cycle's run R2 leaves them pointing at R1, and a case first created by a suite import never points at any run.
- **Gap: provenance.** No `TestRun` or `TestRunTestCase` records which TestRail run its cases came from.
  - Runs are built from arbitrary case selections (`services.create_test_run_with_cases`, `add_cases_to_test_run`, wizard `setup_run`).
  - `TestRun.testrail_run_id` is the run that the **018 push created**, not a source run. `rerun_cases` copies that push target, not a source.
- **Screenshots.**
  - `TestRunScreenshot` (`projects/models.py`): FK to the pivot, `image` (PNG on local `MEDIA_ROOT`, `screenshots/trtc_<pivot>/<ms>_<tool>.png`), `tool_name`, `created_at`. There is no step number.
  - `tool_name` values come from `agents/services/*`:
    - `take_screenshot`, `browser_take_screenshot`;
    - action "after" shots (`click`, `hover`, `drag`, …), from `verified_element_actions.confirm_action`;
    - `controller_omniparser`, an OmniParser *annotated* overlay used for debugging.
  - `reset_test_run` deletes them. The `upload-celery` container mounts the `MEDIA` volume (`docker-compose.yml`), so the upload worker can read the files.
- **TestRail client** (`projects/testrail_client.py`, httpx `~=0.28.1`).
  - JSON only. 429/`Retry-After` retry (max 3, capped at 30s) and the error envelope are handled in `_request_with_rate_limit_retry`.
  - **Confirmed bug for multipart:** `httpx.Client(headers={"Content-Type": "application/json"})` overrides the multipart content type. A `files=` POST goes out as `application/json` with no boundary. I checked this locally with `httpx.MockTransport` on 0.28.1.
- **Run page** (`views._build_test_run_detail_context`, `templates/projects/test_run_detail.html`): one `x-data="testRunLive"` root, a paginated case table (failures first), and a WebSocket at `/ws/projects/<id>/test-runs/<run_id>/` (`projects/consumers.py::TestRunConsumer`).
- **Case detail page** (`views.test_run_case_detail`, `templates/projects/test_run_case_detail.html`): header actions, one `testRunCaseLive` root.
- **Bulk selection:** `Alpine.data("bulkSelect", (allIds))` in `static/js/punk-hazard.js` (`selected`, `toggle`, `toggleAll`, `allSelected`, `count`, `clear`). It is used by `templates/projects/test_cases.html` with the `.bulk-bar` CSS and hidden `test_case_ids` inputs.
- **Conventions.** `projects/services.py` is a legacy module, so `projects/services/` cannot exist as a package. New projects use cases live in sibling modules `projects/<use_case>_services.py`, as `testrail_results_services.py` and `testrail_run_import_services.py` do. Domain errors go in `projects/exceptions.py`.
- **TestRail API facts** (docs and community sources; see Risks for what is unverified):
  - `POST add_attachment_to_result/{result_id}` takes multipart/form-data with the field `attachment`. The maximum upload is 256 MB. It returns `{"attachment_id": …}`, which is an int on Server and older Cloud and a UUID string on Cloud ≥ 7.1.
  - `add_results_for_cases/{run_id}` validates the whole request. A case that is not a test in the run makes the request fail with 400.
  - Closed (`is_completed`) runs are read-only, and TestRail cannot reopen them. Runs inside a test plan have their own run id, and `add_results_for_cases` and `get_run` work on them.
  - Cloud rate limit: 180 requests/min per instance (Professional), 300 (Enterprise), none on Server. A 429 carries `Retry-After` in seconds.

## Goals

- A per-case **Checked** mark that records who checked the case and when. Only cases with a verdict (Passed, Failed, Blocked) can be checked.
- Check and Uncheck in bulk from the run page, and from the run's case detail page.
- Every new run case knows its **source TestRail run**, captured when the case is added to the PH run.
- **Send checked to TestRail** on the run page adds one result per checked, not-yet-sent case to its source run. It uses the same status mapping and comment as 018, plus a "Checked by" line, and attaches **all** of the case's screenshots (OmniParser overlays excluded), in a fixed order.
- Live progress and a server-rendered summary use the existing run WebSocket. Cases that cannot be sent are reported with a reason, not silently dropped.
- Retrying a failed send never duplicates a TestRail result.

## Non-goals

- A human overriding the AI verdict, for example "AI said Failed, I say Passed". Checked means "verdict reviewed and accepted".
- Adding missing cases to the source run (`update_run`), or recreating a closed source run.
- Changing or removing the 018 whole-run push (see Q1).
- Live updates of the Checked marks in other open tabs. Check and Uncheck are full-page POST/redirects.
- Selecting cases across pagination pages. Bulk selection works on the visible page, as on the Test Cases page.
- Choosing the source run by hand, or editing it.
- Sending logs, or OmniParser overlay images.

## Design

### Data model

One additive migration, `projects/migrations/0021_checked_results_source_push.py`.

`TestCase`:

| Field | Type | Notes |
|---|---|---|
| `testrail_source_run_id` | `PositiveIntegerField(null=True, blank=True)` | The TestRail run this case was most recently imported from with a run link. Written by run-link imports only, on both create and update. Suite imports and `TestCaseData` edits leave it alone, like `testrail_position`. |

`TestRunTestCase`:

| Field | Type | Notes |
|---|---|---|
| `testrail_source_run_id` | `PositiveIntegerField(null=True, blank=True)` | **Snapshot** of `test_case.testrail_source_run_id`, taken when the pivot is created. A later re-import from the next cycle's run cannot redirect an old PH run. |
| `checked_at` | `DateTimeField(null=True, blank=True)` | Null means not checked. |
| `checked_by` | `ForeignKey(AUTH_USER_MODEL, null=True, blank=True, on_delete=SET_NULL, related_name="+")` | Keeps the mark if the user is deleted. |
| `testrail_source_result_id` | `PositiveIntegerField(null=True, blank=True)` | The TestRail result added to the source run. Not null means sent, and this is the dedupe key. |
| `testrail_source_sent_at` | `DateTimeField(null=True, blank=True)` | |
| `testrail_source_attachment_count` | `PositiveSmallIntegerField(default=0)` | How many of the case's planned screenshots are attached. A retry resumes from this index. |

`TestRun`. Kept separate from the 018 fields so the two flows never overwrite each other's status or summary. Choices reuse `TestRailPushStatus`.

| Field | Type |
|---|---|
| `testrail_source_push_status` | `CharField(max_length=20, choices=TestRailPushStatus.choices, default=NOT_PUSHED)` |
| `testrail_source_push_started_at` | `DateTimeField(null=True, blank=True)`, used for stale-PUSHING detection |
| `testrail_source_pushed_at` | `DateTimeField(null=True, blank=True)` |
| `testrail_source_push_error` | `TextField(blank=True, default="")` |
| `testrail_source_push_summary` | `CharField(max_length=255, blank=True, default="")` |

**No data migration (Q3).** The migration only adds fields (`AddField`s), with no `RunPython`. Existing `TestCase` and `TestRunTestCase` rows keep `testrail_source_run_id = NULL`, so:

- runs created before deploy can never send checked results; the panel says so (see Pages);
- an existing case gets a source run the next time its TestRail run link is imported. Runs created *after* that re-import carry it. Note this in `docs/changed` as the upgrade step: "re-import the TestRail run link, then create the run".

**Why a pivot snapshot rather than a run-level field:** a PH run can mix cases from several TestRail runs, or cases with no run. Per-pivot provenance handles both without a picker dialog. Sending groups the checked pivots by source run id.

**Dedupe semantics: one result per execution.** A pivot is "sent" once `testrail_source_result_id` is set, and it is never sent again for the same execution. The 018 status+comment hash is not needed: a pivot's verdict, text and screenshots can only change through `reset_test_run`. Reset deletes the screenshots and clears the source tracking (see below), so a re-check and send after a reset appends a fresh result. The 018 per-pivot tracking fields are left untouched, so the two flows cannot corrupt each other's dedupe.

**Lifecycle rules**

- Check: allowed when the pivot status is `success`, `failed` or `blocked` and `checked_at` is null. The run may still be running, because a finished pivot does not change until a reset.
- Uncheck: allowed while checked and **not yet sent**. After sending, the mark is locked, because TestRail results are append-only and cannot be retracted from here.
- `reset_test_run` additionally clears the following. `testrail_source_run_id` is kept, since it is provenance and not a result.
  - `checked_at` and `checked_by`;
  - `testrail_source_result_id`, `testrail_source_sent_at` and `testrail_source_attachment_count`;
  - the five run-level `testrail_source_*` fields.
- `reset_test_run` refuses while `testrail_source_push_status == PUSHING`, because it deletes screenshots the worker may be uploading.
- `rerun_cases` copies `testrail_source_run_id` from each old pivot. A "Re-run failed" run can then send to the same source run, which is the main loop: run, re-run failures, check, send. Checked and sent state is not copied.
- `create_test_run_with_cases` and `add_cases_to_test_run` set the pivot snapshot from the test case. No extra query: the cases are already loaded.
- `_clone_test_case` (copy to another PH project) does **not** copy `testrail_source_run_id`, because the target project may use a different TestRail instance.

### Services

New modules, one use case each. Views, tasks and the consumer call only these.

**`projects/run_case_check_services.py`** (checking)

- `CHECKABLE_STATUSES` is `success`, `failed`, `blocked`.
- `list_checkable_pivot_ids(test_run) -> list[int]`: membership choices for the form. One `values_list` query.
- `check_run_cases(*, test_run, pivot_ids, user) -> CheckOutcome(checked: int, skipped: int)`:
  - one conditional `UPDATE` over `test_run.pivot_entries`, filtered by `id__in`, `status__in=CHECKABLE_STATUSES` and `checked_at__isnull=True`;
  - `skipped` is the number of ids selected minus the number updated;
  - raises `RunCaseCheckError` when nothing could be checked.
- `uncheck_run_cases(*, test_run, pivot_ids) -> CheckOutcome`: the same shape, filtered by `checked_at__isnull=False, testrail_source_result_id__isnull=True`.
- `format_check_message(outcome, *, verb: str) -> str`: a pure function in this module, for example "Checked 4 cases · 1 skipped (no verdict or already checked)".

**`projects/testrail_checked_push_services.py`** (sending)

- Dataclasses:
  - `CheckedPushPanel(configured, can_send, has_any_source, ready_count, ready_screenshot_count, no_source_count, sent_count, checked_count, source_runs: list[SourceRunLink(run_id, url)], status, summary, error, pushed_at)`. Built with one aggregate query plus one `distinct` `values_list` for the source run ids. **No TestRail calls on page load.**
  - `PlannedCheckedResult(pivot, case_id, source_run_id, status_id, comment, elapsed, screenshots: list[TestRunScreenshot])`.
  - `CheckedPushOutcome(sent, screenshots, already_sent, not_in_run: dict[int, int], closed_runs: list[int], no_source, no_case_id, missing_files)`.
- `get_checked_push_panel(test_run) -> CheckedPushPanel`.
- `start_checked_push(*, test_run, links_base_url) -> None`:
  - Validates, raising domain exceptions:
    - `RunNotFinishedError`: the run must be done or cancelled, consistent with 018;
    - `TestRailNotConfiguredError` (existing);
    - `NothingToSendError`: no checked, unsent pivot with a source run;
    - `CheckedPushInProgressError`.
  - Takes the PUSHING slot with a **conditional update**: `TestRun.objects.filter(pk=…).exclude(testrail_source_push_status=PUSHING).update(...)`. When it updates 0 rows, the push is already running and the error is raised; double submits cannot start two tasks.
  - A PUSHING status whose `testrail_source_push_started_at` is older than the task's hard time limit plus 60s counts as stale and may be taken over. This covers a worker killed mid-push.
  - Broadcasts, then dispatches with `celery_app.send_task("projects.tasks.push_checked_results_to_testrail", args=[test_run.id, links_base_url])`. This uses the same pattern as 018 and avoids the services→tasks import cycle.
  - If dispatch fails, it marks the run FAILED, as `start_testrail_run_import` does.
- `plan_checked_push(test_run, *, links_base_url) -> (list[PlannedCheckedResult], skip counters)`:
  - Fetches `pivot_entries.filter(checked_at__isnull=False).select_related("test_case", "checked_by")` with **one** `Prefetch("screenshots", queryset=TestRunScreenshot.objects.exclude(tool_name="controller_omniparser").order_by("created_at", "id").only("id", "test_run_test_case_id", "image", "tool_name", "created_at"), to_attr="testrail_screenshots")`. That makes three queries in total, with no slice and no cap (Q2).
  - **Deterministic order is what makes resume safe.** Screenshots are ordered by `(created_at, id)` ascending (the `id` breaks ties between shots saved in the same millisecond). The list for a pivot cannot change between attempts: screenshots are only added while the case executes and only deleted by reset, and reset is refused while a send is running. So `testrail_source_attachment_count` is always a valid index into that list.
  - Only the screenshot rows are loaded up front. Each image file is read into memory just before its upload and released afterwards, so memory stays at one screenshot no matter how many a case has.
  - It includes pivots that have a result id but `testrail_source_attachment_count < len(screenshots)`, so the attachments resume on the existing result.
- `push_checked_results(test_run, *, links_base_url) -> CheckedPushOutcome`, called by the task:
  1. Plan, then group by `source_run_id`. Groups are processed in ascending run id order and use one `build_testrail_client` context.
  2. For each group, `client.get_run(id)`:
     - 400/404 → all of the group goes to `not_in_run` with reason "missing";
     - `is_completed` → `closed_runs`, skip the group.
  3. `client.get_run_case_ids(id)` (new, paginated `get_tests`). Drop the cases whose TestRail case id is not in the run and count them in `not_in_run`. Without this one invalid case would make TestRail reject the whole batch.
  4. Take the remaining pivots that have no result id, in batches of 100, and call `add_results_for_cases`. Immediately record `testrail_source_result_id` and `testrail_source_sent_at` with one `bulk_update` per batch in `transaction.atomic()`. A later failure therefore never re-adds the result.
  5. For each pivot with a result id, upload the screenshots from index `testrail_source_attachment_count` onwards with `client.add_attachment_to_result`. After each upload, increment the count with a single-row `update()`.
     - A missing file on disk is skipped and counted in `missing_files`; the count still advances.
     - A `TestRailError` aborts the push. The task marks it failed, and a re-send resumes.
- `mark_checked_push_succeeded(test_run, outcome)` / `mark_checked_push_failed(test_run, message)`: stamp the five run fields, then broadcast.
  - The status is FAILED when nothing was sent **and** at least one group was closed or missing. Otherwise it is PUSHED, with the skips in the summary.
- `_broadcast_checked_push(test_run)`: sends `{"type": "test_run.testrail_source_push", "push_status": …}` to `_test_run_group(test_run.id)`.
- Reused from `testrail_results_services`:
  - `build_case_detail_url`;
  - `_pivot_result_text`, renamed to the public `pivot_result_text`, with a one-line update at its single call site;
  - `build_testrail_run_url`.

**Pure helpers in `projects/testrail_mapping.py`** (no ORM)

- `build_checked_result_comment(*, outcome_label, run_name, result_text, case_url, checked_by_label, checked_at, screenshot_count, screenshot_total) -> str`. It wraps `build_result_comment` and adds two lines:
  - "Checked by {name} on {YYYY-MM-DD HH:MM UTC}.";
  - "{n} screenshot(s) attached." when n > 0.
- `format_checked_push_summary(outcome) -> str`, for example "7 results sent to R123, R456 · 18 screenshots · 2 already sent · 1 not in R123 · 1 without a source run". It is truncated to 255 characters.
- `format_checked_push_confirm_body(ready_count, run_ids, screenshot_count) -> str`. `screenshot_count` is the total number of screenshots that will be attached; the panel gets it from one `Count` aggregate that excludes `controller_omniparser`.
- `screenshot_attachment_filename(case_id, index, image_name) -> str`, for example `C123_02_click.png`.

**Changes to existing services**

- `projects/testrail_services.py`:
  - `upsert_test_cases_from_testrail` gains `source_run_id: int | None`. New cases get it. Updated cases get it written **only when it is not None**, so suite imports pass None and leave it unchanged. Add it to the update field list conditionally.
  - `import_testrail_run_cases` passes `upload.testrail_run_id`; the suite import task passes None.
- `projects/services.py`:
  - `create_test_run_with_cases` and `add_cases_to_test_run` set `testrail_source_run_id=tc.testrail_source_run_id` on each new pivot.
  - `rerun_cases` reads `values_list("test_case_id", "testrail_source_run_id")` and copies it.
  - `reset_test_run`: the clears and the PUSHING guard described above. Replace the per-pivot `save()` loop's field list with the added fields. This is in scope because it is the same loop; do not refactor it further.
  - `list_test_run_cases` adds `"checked_by"` to `select_related`, for the "Checked by" tooltip.
  - `fetch_test_run_state` needs no change: the consumer reads the new run field from the returned `test_run`.

**Exceptions (`projects/exceptions.py`).** All carry a user-safe `.message`, matching `TestRailRunImportError`.

- `RunCaseCheckError`.
- `CheckedPushError` base, with `RunNotFinishedError`, `NothingToSendError` and `CheckedPushInProgressError`.

**Client (`projects/testrail_client.py`)**

- Remove `Content-Type` from the `httpx.Client` defaults. Pass `headers={"Content-Type": "application/json"}` on GET explicitly; `json=` sets it on POST.
- Generalise `_request_with_rate_limit_retry` / `_send_request` to take either a JSON body or a multipart file tuple. The 429 retry must resend the same bytes, so attachments are passed as **bytes** (read one file at a time, a few MB each), never as a file handle that a retry would find already consumed.
- `add_attachment_to_result(result_id, *, filename: str, content: bytes, content_type: str = "image/png") -> str`. It returns `attachment_id` as a string because Cloud returns UUIDs. The value is used only for logging.
- `get_run_case_ids(run_id) -> set[int]`: paginates `get_tests/{run_id}` and reads only `case_id`.

### Pages and forms

| URL (name) | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/projects/<id>/test-runs/<run_id>/cases/check/` (`projects:test_run_cases_check`) | POST | `@project_membership_required`, `@require_POST`, CSRF | `RunCaseSelectionForm(pivot_ids: TypedMultipleChoiceField(coerce=int, MultipleHiddenInput), next: hidden)`. Choices come from `list_checkable_pivot_ids` (membership validation, as in `TestRunCreateForm`); max 500 ids. | Redirects to a validated `next` (the case detail page) or else the run detail page; `messages.success` with the outcome. | Run not in project → 404. Invalid form or `RunCaseCheckError` → `messages.error` + redirect. |
| `/projects/<id>/test-runs/<run_id>/cases/uncheck/` (`projects:test_run_cases_uncheck`) | POST | same | same form; choices = the run's checked, unsent pivot ids | same | same |
| `/projects/<id>/test-runs/<run_id>/testrail/send-checked/` (`projects:testrail_send_checked`) | POST | same | none (no input) | Redirects to the run detail page; `messages.info("Sending N checked results to TestRail…")`. | `CheckedPushError`, `TestRailNotConfiguredError` → `messages.error(exc.message)` + redirect. |

The views are thin:

- Check and Uncheck live in `projects/views.py`, next to the other run actions, so they reuse `_next_or_default`.
- Send lives in `projects/testrail_views.py`, next to the 018 push, so it reuses `_redirect_to_run`. `links_base_url = request.build_absolute_uri("/")`, as in 018.

**Run detail context** (`_build_test_run_detail_context`):

- adds `testrail_checked_push` (`CheckedPushPanel`);
- adds `checkable_pivot_ids` (the ids on *this page* that can be checked, for `bulkSelect`), computed from the page's pivots in Python with no extra query;
- adds `testrail_checked_push_confirm_body` (str).

**Run detail template** (`templates/projects/test_run_detail.html`):

- Case results card:
  - Wrap it in a nested `x-data="bulkSelect([...checkable ids])"` scope inside `testRunLive`.
  - Add a leading checkbox column. Rows that cannot be checked get a disabled checkbox with an `aria-label` that explains why.
  - Add a **Checked** column showing one of three states:
    - "Checked" (`badge-passed`-style, with a `title` "by {name}, {time}");
    - "Sent to R{id}";
    - "—".
  - A `.bulk-bar` (`x-show="count > 0"`) with "{count} selected", and **Check** and **Uncheck** buttons. Each is its own `<form>` that posts the hidden `pivot_ids` from `selected`, using the same pattern as `_test_case_bulk_dialogs.html`. A **Clear** button follows.
- Header actions: an include, `templates/projects/includes/_testrail_checked_push.html`:
  - a **Send checked to TestRail (N)** button (`btn-secondary`), visible for finished runs when the project has TestRail settings and `ready_count > 0`;
  - otherwise the button is disabled, with a title that gives the reason;
  - when **no** case in the run has a source run (`CheckedPushPanel.has_any_source` is False; every run created before deploy is like this), there is no button. Instead a muted line says: "Results can't be sent back: this run's cases weren't imported from a TestRail run link when the run was created. Re-import the run link, then create a new run.";
  - the button opens a `partials/confirm_dialog.html` (`dialog_id="send-checked-testrail"`, `confirm_class="btn-primary"`).
  - Confirm body: "N results will be added to TestRail run R123 (and R456), with S screenshots. Results cannot be removed from TestRail afterwards."
- A status line under the 018 status line, from the same include:
  - pushing: "Sending checked results to TestRail…";
  - pushed: the time and summary, plus "Open run R{id}" links;
  - failed: the error.
  - It also always shows "{checked} of {total} checked · {ready} ready to send · {no_source} without a source TestRail run" when TestRail is configured and any case has a source run.
- Alpine `testRunLive`:
  - add `testrailSourcePushStatus`, seeded from the server and from `current_state.testrail_source_push_status`;
  - handle the `testrail_source_push` message the same way as `testrail_push`: when the status settles, reload once after 1 second.
- CSS: reuse `.bulk-bar`, `.badge-*` and `.data-table`. Add a `.cell-check` width rule in `static/css/punk-hazard.css` only if the column needs it; no inline styles.

**Case detail** (`views.test_run_case_detail` and its template):

- Add to the context:
  - `can_check` (the status is checkable and the case is not checked);
  - `can_uncheck` (checked and not sent);
  - `source_run_url`.
- Header actions:
  - **Mark as checked**, a POST form to `test_run_cases_check` with `pivot_ids=<pivot.id>` and `next=<this page>`;
  - when the case is checked, a "Checked by X · time" badge and an **Uncheck** form;
  - when it is sent, "Sent to TestRail R{id}", linking to the run.
  - The buttons render server-side. When `status` changes over the case WebSocket from in progress to a verdict, the page shows Check only after a reload. That is acceptable: Check is meant for finished cases.

All user data (names, titles, summaries, errors) is rendered with autoescape. No `|safe`.

### Background work and WebSockets

- Celery task `projects.tasks.push_checked_results_to_testrail(test_run_id: int, links_base_url: str)`:
  - `queue="upload"`, `max_retries=0`, `acks_late`, `reject_on_worker_lost`;
  - `soft_time_limit=3600`, `time_limit=3660`. With every screenshot attached, one request per screenshot dominates. For example, 100 cases × 30 screenshots ≈ 3,100 requests, about 17 minutes at Cloud Professional's 180 per minute, plus upload time.
  - A larger send that hits the soft limit is **not** an error to fix by raising limits further. It stops cleanly as FAILED ("Timed out; send again to resume"), and the next Send continues from the stored result ids and attachment counters. The stale-PUSHING window in `start_checked_push` is derived from `time_limit`; define both from one module constant so they cannot drift apart.
  - Register it in `CELERY_TASK_ROUTES`.
  - The body mirrors `push_test_run_results`:
    - fetch the run;
    - call `push_checked_results`, then `mark_checked_push_succeeded`;
    - on `TestRailError` or `TestRailNotConfiguredError`, call `mark_checked_push_failed(exc.message)`;
    - on `SoftTimeLimitExceeded`, mark failed with "Timed out; send again to resume";
    - on any other exception, log it and use a generic message.
- Consumer `TestRunConsumer`:
  - new handler `test_run_testrail_source_push(event)` → `{"type": "testrail_source_push", "push_status": …}`;
  - `current_state` gains `testrail_source_push_status`.
- No new consumer and no new WebSocket route. **Controller protocol unchanged**: no `CLIENT_VERSION` or capability change.

### Settings / infrastructure

- No new setting (Q2: no screenshot cap).
- Restart the `upload` worker on deploy (new task route).
- Run `migrate` (`0021`).

### Data flow

1. A reviewer opens a finished run, selects rows, and clicks **Check**. POST `test_run_cases_check` → form validates membership → `check_run_cases` runs one UPDATE → redirect with a message.
2. Or, on the case detail page, **Mark as checked** posts the same form with one id and returns to the case page.
3. The run page panel shows "N ready to send" to R… The reviewer clicks **Send checked to TestRail** and confirms. POST `testrail_send_checked` → `start_checked_push` takes the slot (conditional update), broadcasts `pushing` and dispatches the task → redirect.
4. The worker loads the client and handles each source run in turn:
   - `get_run`, then `get_run_case_ids` (filter);
   - `add_results_for_cases` in batches of 100, recording the result ids;
   - `add_attachment_to_result` per screenshot, recording the count.
   - It then stamps PUSHED or FAILED and the summary, and broadcasts.
5. The run page's WebSocket receives `testrail_source_push` and reloads once. The status line shows the summary and the TestRail run links. The sent rows show "Sent to R…".

## Alternatives considered

- **Extend the 018 push to "checked only, into the source run"** (replace). Rejected for now. The 018 push creates a new TestRail run for suite-imported cases, which have no source run, and the user did not ask to remove it. Merging the two would overload `TestRun.testrail_run_id` and the per-pivot tracking with two different meanings. The two flows share only pure helpers and the client. Q1 asks whether to hide or remove 018.
- **Run-level source run chosen in a dialog** (one `TestRun.testrail_source_run_id`, preselected from imports). Rejected: it needs a picker and TestRail calls on page load, it handles mixed runs badly, and the user's wording ("the source test run") implies it is known, not chosen.
- **Live lookup through `test_case.upload.testrail_run_id`** with no new fields. Rejected: `upload` is the *first* import. The next cycle's run R2 would never be targeted, and suite-first cases would never have a source.
- **A provenance table `TestCase`↔TestRail run (many-to-many).** More exact, but the pivot snapshot already answers the only question asked ("where did this run's case come from"), with one integer.
- **Reuse the 018 status+comment hash for dedupe.** Unneeded: results cannot change without a reset, and reset clears the tracking. A result id plus an attachment counter is simpler and also makes partial failures resumable.
- **Cap screenshots at the latest N per case.** Fewer requests, but the user wants the full evidence trail in TestRail (Q2). Every screenshot is attached; OmniParser overlays are still excluded because they are debugging images, not evidence.
- **Backfill source runs for existing runs from the creating upload.** Rejected (Q3). The guess is wrong whenever the same cases were later re-imported from a newer TestRail run, and a wrong append-only result cannot be undone.
- **Check and Uncheck over the WebSocket or a JSON endpoint.** Rejected: the project has no REST API, and a POST/redirect is enough.

## Risks and mitigations

- **Old runs cannot send (by decision, Q3).** There is no backfill, so runs and cases from before deploy have no source run. Mitigation: the run page explains this and the fix (re-import the run link, then create a new run). The confirm dialog still names every target run with a link.
- **Append-only target.** A mistaken send cannot be undone from Punk Hazard. Mitigation: explicit confirm, an Uncheck that is locked after sending, and dedupe by result id.
- **Partial failure mid-send.** Result ids are recorded per batch and the attachment counter per upload, so a re-send resumes without duplicate results or attachments. A worker killed after `add_results_for_cases` returns but before the `bulk_update` could duplicate that batch on retry. The window is tiny; this is accepted and documented.
- **Stuck PUSHING after a hard kill.** Stale detection uses `testrail_source_push_started_at` plus the time limit. The 018 push has the same gap; leave it alone.
- **Rate limits and volume.** With no screenshot cap, uploads dominate the request count. The existing client retries a 429 three times, capped at 30s each, and a large send on Cloud Professional may still exhaust that and fail. The send is resumable (result ids plus attachment counters, in a fixed screenshot order), so the user clicks Send again. Do not add pacing unless the manual check shows failures.
- **Large attachments.** Desktop PNGs are a few MB each, far below TestRail's 256 MB limit. Reading one file at a time keeps worker memory flat.
- **Upload queue occupancy.** A long send (up to an hour) holds one `upload` worker slot, which delays TestRail imports and prompt refinement on a single-concurrency worker. This is acceptable at the expected volumes; note it in `docs/changed`.
- **Multipart header bug.** This is a confirmed behaviour change in the shared client (the default `Content-Type` is removed). The import and 018 paths must be smoke-checked after it, because GETs now set the header per request.
- **Closed or missing source run, or case not in run.** The whole group is skipped with a reason in the summary, and nothing is half-sent. `update_run` is never called on the user's run (it is also invalid for runs inside plans).
- **Reset racing a send.** `reset_test_run` refuses while PUSHING.
- **N+1.** The plan uses three queries; the panel uses two. The run page adds `checked_by` to `select_related`. `get_case_position` is unaffected.
- **Privacy.** "Checked by" puts the reviewer's name (or their email when the name is blank) into the company's TestRail. This is acceptable and in line with the intent, but it is mentioned in Q4.
- **XSS.** New template output is escaped. Error messages come from TestRail (`exc.message`) and are rendered escaped, as in 018.
- **Unverified API detail.** The exact behaviour of `add_attachment_to_result` on the user's instance (field name, id type). The client returns the id as `str` and never relies on it.

## Implementation steps

Steps 1–2 are shared and run first, in order. After them, **A** and **B** run in parallel; they never edit the same file. Step 5 integrates.

1. **Model, migration, exceptions** (files: `projects/models.py`, `projects/migrations/0021_checked_results_source_push.py`, `projects/exceptions.py`, `projects/admin.py` (read-only display of the new pivot fields)). Review the migration SQL (`sqlmigrate`) before applying it.
2. **Provenance and lifecycle in existing services** (files: `projects/testrail_services.py`, `projects/testrail_run_import_services.py`, `projects/tasks.py` (suite import call site passes `source_run_id=None`), `projects/services.py` (`create_test_run_with_cases`, `add_cases_to_test_run`, `rerun_cases`, `reset_test_run`, `list_test_run_cases`)).
   - Also add **interface stubs** with final signatures and dataclasses, so A and B build against them:
     - `projects/run_case_check_services.py`: `CHECKABLE_STATUSES`, `CheckOutcome`, function signatures;
     - `projects/testrail_checked_push_services.py`: `CheckedPushPanel`, `SourceRunLink`, `get_checked_push_panel`, `start_checked_push`.
3. **A, checking and the UI** (files: `projects/run_case_check_services.py` (body), `projects/forms.py` (`RunCaseSelectionForm`), `projects/views.py` (two views; run detail and case detail context, including the `testrail_checked_push*` keys from B's stub), `projects/urls.py` (check, uncheck and send routes; the send route points at B's view name `testrail_views.testrail_send_checked`), `templates/projects/test_run_detail.html` (checkbox column, bulk bar, Checked column, include of B's partial, Alpine state and WebSocket handler for `testrail_source_push`), `templates/projects/test_run_case_detail.html`, `static/css/punk-hazard.css` (only if needed)).
4. **B, sending** (files: `projects/testrail_client.py`, `projects/testrail_mapping.py`, `projects/testrail_results_services.py` (rename `pivot_result_text` only), `projects/testrail_checked_push_services.py` (body), `projects/tasks.py` (new task), `auto_tester/settings.py` (task route only), `projects/consumers.py`, `projects/testrail_views.py` (`testrail_send_checked`), `templates/projects/includes/_testrail_checked_push.html` (button, status line, confirm dialog; context contract documented in its header comment)).
   - Note: `projects/tasks.py` is touched in step 2 (one call-site argument) and in B, so step 2 must land before B starts.
5. **Integrate** (orchestrator): wire the URL names, include and context keys, and the WebSocket event name. Run `pre-commit run --all-files` and `python manage.py check`, then `makemigrations --check --dry-run`.
6. **Docs:** `docs/changed/2026-10-22-testrail-checked-results.md`; set this plan to Done.

## Verification

No automated tests (project rule). Manual checks against the dev stack (`docker compose up db redis -d`, Daphne, an `upload` worker), with a TestRail sandbox:

1. `python manage.py sqlmigrate projects 0021` shows only `ADD COLUMN`s. `migrate`. Then open an old finished run: there is no send button, and the "can't be sent back" explanation is shown.
2. Import run R1 with a run link (or re-import it). Create a PH run from those cases and run it, or reuse a finished one. Every pivot shows source R1 (Django admin).
3. Run page: select 3 rows → Check → message "Checked 3 cases". The rows show Checked with a tooltip. Select a running or not-started row: its checkbox is disabled. Uncheck one → it clears.
4. Case detail: Mark as checked → it returns to the case page with the badge. Uncheck works.
5. Send checked to TestRail → the confirm dialog names R1 → the status reads Sending… then Pushed, with a summary.
   - In TestRail, each checked case in R1 has a new result with the right status (Passed 1, Failed 5, Blocked 2), the comment includes "Checked by", and every non-OmniParser screenshot of the case is attached, in the same order as the case detail page, and opens.
   - No `controller_omniparser` overlays are attached.
6. Click Send again with nothing new: the button is disabled (0 ready). Check another case and send: exactly one new result.
7. A mixed run (add a manually created case): it is counted "without a source run" and not sent.
   - A case whose TestRail case was removed from R1: it is counted "not in R1", and the other cases are still sent.
8. Close R1 in TestRail, check a case, send: FAILED, with "run R1 is closed".
9. Partial failure: stop the network (or use a bad API key) mid-send, then fix it and send again. Verify there are no duplicate results and the attachments resume at the next screenshot, with none repeated or missing. Use a case with 20 or more screenshots.
10. Reset the run while a send is in progress → refused. After the send, Reset clears the Checked and Sent state. Re-run, check and send → a new result is appended.
11. Re-run failed: the new run's pivots carry the source run, and checking and sending targets R1.
12. Regression: the 018 Push to TestRail still works (create run, push), and the suite import and run import still work. The client header change touches every GET.
13. `pre-commit run --all-files`, `mypy .`, and `python manage.py check`, all clean.
14. Run the `reviewer` and `verifier` agents in parallel on the diff (give `verifier` the original two user messages and this plan).

## Open questions

None open. All four were answered by the user on 2026-10-22 and are recorded here as decisions.

- **Q1. The existing "Push to TestRail" (018, which creates a new TestRail run with all results).** Decided: **keep it unchanged** next to the new button. The two flows track their state in separate fields.
- **Q2. Which screenshots go to TestRail.** Decided: **all screenshots of each checked case**, excluding the OmniParser overlay images (`tool_name="controller_omniparser"`). No cap and no setting. They are ordered by `(created_at, id)` so the attachment counter resumes correctly, and the task time limit is raised to 3600/3660s; a send that still times out resumes on the next Send.
- **Q3. Existing runs after deploy.** Decided: **no backfill**. The migration only adds fields. Only runs created after deploy, from cases imported (or re-imported) with a TestRail run link, carry a source run. Older runs show no send button, with an explanation.
- **Q4. Checked rules and TestRail content.** Decided as proposed:
  - Uncheck is allowed until the case is sent, then locked.
  - Reset clears Checked and Sent.
  - Checking needs a Passed, Failed or Blocked verdict; the run need not be finished.
  - Sending needs a finished run.
  - The TestRail comment includes "Checked by <full name or email> on <date>".

## Changelog

- 2026-10-22: created (Draft).
- 2026-10-22: questions resolved by the user, Status set to Ready.
  - Q1: keep the 018 push unchanged.
  - Q2: attach all screenshots, excluding OmniParser overlays. `TESTRAIL_RESULT_SCREENSHOT_LIMIT` dropped. Unsliced prefetch ordered by `(created_at, id)`. Task limits raised to 3600/3660s, with the stale-PUSHING window tied to the same constant.
  - Q3: no backfill. The migration is additive only. `CheckedPushPanel.has_any_source` added, and old runs show an explanation instead of the button.
  - Q4: rules confirmed as assumed.
  - Split points: B no longer touches `example.env`, and `settings.py` only gets the task route.
- 2026-10-22: implemented (Done). Review/verify found and fixed, over three rounds:
  - unsendable checked cases stayed "ready": new `TestRunTestCase.testrail_source_skip_reason` (`CheckedPushSkipReason`), shown as "Not sent: …", cleared on check, uncheck and reset;
  - reset/send race and stuck stale PUSHING: `reset_test_run` and `uncheck_run_cases` lock the run row and refuse only while a send is live (`projects/checked_push_limits.py`); `start_checked_push` fails a stale slot first; the slot UPDATE also requires a finished run;
  - redelivered task after a takeover: the slot token (`testrail_source_push_started_at`) is passed to the task, which no-ops if it no longer matches;
  - redis `visibility_timeout` (default 3600s) below the task limit: set to 4h in settings;
  - smaller items: `only()` in the planner, comment counts only existing files, no `get_tests` for resume-only groups, `get_source_run_url` service, `runCaseChecks` Alpine component, "Checked by" kept after sending, unused form field removed.
  - Deviations from the plan: the confirm body lives on `CheckedPushPanel.confirm_body` (no separate context key); the header include is split into `_testrail_checked_push_actions.html` and `_testrail_checked_push_status.html`.
  - No findings rejected.
