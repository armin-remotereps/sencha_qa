# feat: Import test cases from a TestRail run URL (and retire XML upload)

Status: Done
Created: 2026-10-22
Request: A project member pastes a TestRail test run link (e.g.
`https://acme.testrail.io/index.php?/runs/view/123&group_by=cases:section_id`) or a
run ID (`R123` / `123`) on the Import page. Punk Hazard checks that the link points
at the project's configured TestRail instance, fetches the run's tests from the
TestRail API, and imports the cases behind them. It reuses the existing API import
pipeline: mapping, upsert by TestRail case ID, history row, and live progress.

In the same change, the TestRail XML upload flow is removed completely. Remaining
import paths are the TestRail suite import (account picker) and the new run import.
Old XML history rows stay in the database.

## Context

### Credentials

`Project.testrail_url`, `testrail_email` and `testrail_api_key_encrypted` (Fernet,
`projects/secrets.py`) live on `Project` in `projects/models.py`.
`has_testrail_settings` is true when all three are set. The URL is stored with no
trailing slash, query or fragment (`TestRailSettingsForm.clean_testrail_url`).
`build_testrail_client(project)` in `projects/testrail_services.py` decrypts the
key, or raises `TestRailNotConfiguredError`.

### Client

`projects/testrail_client.py` provides:

- `TestRailClient`, built on httpx. Its `_paginate` follows `_links.next`, 250 per page.
- 429 retry: up to 3 times, honouring `Retry-After`, capped at 30 s.
- `TestRailError(message, status_code)`.
- `get_run(run_id)`, returning `TestRailRun(id, name, is_completed, url)`. There is no `get_tests`.

### Mapping and upsert

- `TestRailFieldMapper` (`projects/testrail_mapping.py`) maps a `TestRailCase` to
  `TestCaseData`.
- `upsert_test_cases_from_testrail` (`projects/testrail_services.py`) matches
  `TestCase.testrail_id == str(case.id)`:
  - Existing rows are updated in place and keep their `upload` FK.
  - Missing rows are bulk-created with `upload=<this import>`.
  - It reports `(processed, updated)` per batch of 50.
- Dedup matching is not changed by this plan. XML-era rows stored as `"C123"` may
  duplicate when the same case is imported via the API. The user accepted this (Q1).

### History row and progress

- Each import is a `TestCaseUpload` with `source=TESTRAIL_API`,
  `testrail_project_id` and `testrail_suite_id`. The label goes in
  `original_filename` (max 255).
- Task `projects.tasks.import_testrail_cases` runs on the `upload` queue. It
  broadcasts `upload.progress` to `upload_<project_id>`.
- The Import page (`uploadManager()`) and the wizard step 3 (`caseSetupWizard`)
  consume those events.
- Cancel and delete (`projects/services.py`: `cancel_upload_processing`,
  `delete_upload`) remove only the rows that import created.

### Push-back

`projects/testrail_results_services.py`:

- `suggest_testrail_target` uses the uploads' `testrail_project_id` and
  `testrail_suite_id`.
- `_ensure_testrail_run` creates its own TestRail run. It stays unchanged (Q2).

### XML flow being removed (all usages checked with `git grep`)

- `projects/services.py`:
  - `create_upload`, `start_upload_processing` (lazy-imports `process_xml_upload`)
  - `validate_testrail_xml`, `parse_testrail_xml`, `bulk_create_test_cases_from_parsed`
  - private helpers: `_parse_xml_root`, `_has_case_elements`, `_parse_single_case`,
    `_extract_custom_fields`, `_text`, `_decoded_text`, `_to_test_case`,
    `_split_into_batches`
  - `is_valid_xml_filename`
  - the section banners "XML PARSING SERVICES" and "XML PARSING HELPERS"
  - imports used only by these: `import html`, `import xml.etree.ElementTree as ET`,
    `from django.core.files.uploadedfile import UploadedFile`, and `ParsedTestCase`
    from the `projects.models` import list
- `projects/models.py`: dataclass `ParsedTestCase`. Only `projects/services.py` uses it.
- `projects/tasks.py`: `_process_upload_file` (lazy-imports the parser) and task
  `process_xml_upload`.
- `projects/views.py`:
  - `_handle_xml_upload`, used by `upload_create` and the POST branch of `setup_cases`
  - `upload_create`
  - imports `create_upload`, `is_valid_xml_filename`, `start_upload_processing`,
    `validate_testrail_xml`
  - `upload_create_url` in the `test_case_import` context
- `projects/urls.py`: route `upload_create`. It is referenced only by
  `views.test_case_import`.
- `auto_tester/settings.py`: `CELERY_TASK_ROUTES["projects.tasks.process_xml_upload"]`.
- Templates:
  - The XML card in `templates/projects/import.html`.
  - `templates/partials/dropzone.html`, used only by `import.html` and
    `wizard/step_cases.html`.
  - The dropzone and form in `templates/projects/wizard/step_cases.html`.
- CSS: `.dropzone`, `.dropzone strong` and `.dropzone.is-active` in
  `static/css/punk-hazard.css`. `ui-prototype/` is a separate static prototype and
  is not touched.
- Docs: `CLAUDE.md` line 7 ("upload TestRail XML test cases").
- No XML library exists in `requirements.txt`. There is nothing to drop there.

### XML-adjacent code that stays (still needed)

- **`TestCaseUpload` and its fields** (`file`, `source`, the `UploadSource.XML`
  choice, and `default=UploadSource.XML`). Old rows still carry them, and
  `delete_upload` still deletes a leftover file of a failed or cancelled XML row.
  - Changing `default` would only generate a state migration for no runtime
    benefit. All remaining creators set `source` explicitly, so it is left alone.
- **`TestCase.is_converted`.** It holds stored data, and `_clone_test_case` and
  the admin use it.
- **`parse_testrail_case_id`** (accepts `C123`). Old XML-imported cases still
  push back.
- **`cancel_upload_processing`, `delete_upload`, `get_upload_for_project`,
  `list_uploads_for_project`, `list_completed_uploads_for_project`, and the
  `upload_cancel`/`upload_delete` views and routes.** They are shared with API
  imports.
- **`FILE_UPLOAD_MAX_MEMORY_SIZE` / `DATA_UPLOAD_MAX_MEMORY_SIZE`** in
  `settings.py`. These are general request limits and are not XML-specific.
  Lowering them could break other POSTs.
- **The `.claude/agents/*.md` mentions of XML.** They are generic review checklists.

### Wizard impact (found while scoping the removal)

The onboarding wizard step 3 (`setup_cases`, `templates/projects/wizard/step_cases.html`)
currently supports only XML upload. After removal, its "Import from TestRail" option
is rebuilt on the run import:

- **TestRail configured:** it shows the new run-import card, posting with
  `next=<wizard URL>` so the user stays in the wizard.
- **Not configured:** it shows a link to the project Settings tab to connect TestRail.
- **In both cases:** it shows a link to the Import page for importing a whole suite.

The wizard is not resumable and reads real state, so the existing upload list, WS
progress and `case_count` gate work unchanged.

### Conventions

`CLAUDE.md` wants `<app>/services/<use_case>.py`. However, `projects/services.py`
is a legacy module, so a `projects/services/` package can't sit beside it. Existing
TestRail use cases are sibling modules (`projects/testrail_services.py`,
`projects/testrail_results_services.py`). This plan follows them with
`projects/testrail_run_import_services.py`. `projects/exceptions.py` is new.

### TestRail API facts relied on

Docs pages return 403 to fetches. These facts were checked against the
`tolstislon/testrail-api` client source.

- **`get_run/{run_id}`** returns `id`, `name`, `project_id`, `suite_id`, `plan_id`,
  `is_completed` and `url`.
  - An unknown or invisible run returns 400 ("Field :run_id is not a valid test run.").
  - No access returns 403. Bad credentials return 401.
- **`get_tests/{run_id}&limit=250&offset=…`** is paginated with
  `{offset, limit, size, _links, tests}` (TestRail 6.7+). Each test carries
  `case_id`, `title`, `template_id`, `type_id`, `priority_id`, `estimate`, `refs`
  and the case's `custom_*` fields.
- **`get_cases`** has no case-ID filter, and `get_case/{id}` costs one request
  per case.

## Goals

- Paste a run URL or ID on the Import page, or in wizard step 3, and import every
  case in that run. Accepted inputs:
  - full run URL (any scheme, trailing `&…` or `#…`)
  - scheme-less URL
  - `R123`, `r123`, `123`
- Reject URLs for another host or base path. Network calls go only to the stored
  base URL.
- Synchronous validation with immediate feedback for:
  - bad format
  - plan or test URL
  - host mismatch
  - not configured
  - run not found or no access
  - auth failure
  - the same run already importing
- Async import on the `upload` queue, reusing the history row, WS progress, cancel
  and delete.
- Dedup by TestRail case ID, exactly as the suite import does today.
- Record `testrail_run_id` (plus `testrail_project_id` and `testrail_suite_id`
  from `get_run`) on the upload.
- Remove the XML upload flow and all dead XML code. Keep the history rows and model
  fields.

## Non-goals

- Pushing results back into the source TestRail run (Q2: no). Push-back is unchanged.
- An auto-created Draft run (Q3: no).
- Plan import (`plans/view/…`) and single-test import (`tests/view/…`). These get a
  specific error.
- Importing TestRail per-test statuses or results.
- Changing dedup matching (`C123` vs `123`), adding a unique constraint, or cleaning
  up duplicates.
- Making "View test cases" list updated (pre-existing) cases (Q5).
- Deleting old XML history rows or their files, removing model fields, or a
  destructive migration.
- Converting `projects/services.py` into a package.

## Design

### Data model

`TestCaseUpload` gains one field:

- `testrail_run_id = PositiveIntegerField(null=True, blank=True)`

Plus a property `is_testrail_run_import -> bool`. Run imports use
`source=TESTRAIL_API`, so `is_testrail_api` and "N new · M updated" work unchanged.

Migration `0016_testcaseupload_testrail_run_id`:

- It is an additive nullable column: metadata-only `ADD COLUMN` on Postgres, safe
  on a populated table, no data loss.
- The `ParsedTestCase` deletion is a plain dataclass, not a model, so it has no
  migration impact.

Admin: show `testrail_run_id` in `TestCaseUploadAdmin`.

### Client (`projects/testrail_client.py`)

- Extend `TestRailRun` with `project_id: int`, `suite_id: int | None` and
  `plan_id: int | None`, parsed in `_parse_run`. The only constructor is
  `_parse_run`, and `add_run`/`update_run` responses carry these fields too.
- Add `iter_run_cases(run_id) -> Iterator[TestRailCase]`. It paginates
  `get_tests/{run_id}&limit={TESTRAIL_PAGE_SIZE}` on key `"tests"`.
  - It maps tests through `_parse_test_as_case`: `id = case_id`, with the same
    `custom_*` rules as `_parse_case` via a shared private helper.
  - It skips tests that have no `case_id`.

### Domain exceptions (`projects/exceptions.py`, new)

All have a `.message` attribute, like `TestRailError` and `TestRailNotConfiguredError`.

- `TestRailRunReferenceError`: not a run URL or ID, or a plan or test URL. Each case
  has its own message.
- `TestRailHostMismatchError(TestRailRunReferenceError)`
- `TestRailRunNotFoundError`
- `TestRailRunImportInProgressError`

### Services (`projects/testrail_run_import_services.py`, new)

**`parse_testrail_run_reference(raw: str, *, testrail_base_url: str) -> int`** is
pure (no ORM, no network). Steps:

1. Strip the input. `^[Rr]?(\d+)$` with a value > 0 returns the ID.
2. Otherwise, add `https://` if no scheme is present, then `urlsplit`.
3. Compare the lowercased hostname and the effective port with the base. Ignore
   http vs https and default ports. A mismatch raises `TestRailHostMismatchError`,
   naming both hosts; the message is autoescaped on render.
4. The path must equal `<base path>/index.php`.
5. The query must match `^/runs/view/(\d+)(?:[&/].*)?$`:
   - `^/plans/view/` raises a "test plan" message.
   - `^/tests/view/` raises a "single test" message.
   - Anything else raises a generic message.

**`build_testrail_run_import_label(run: TestRailRun) -> str`** returns
`"TestRail run R123: <name>"`, at most 255 characters.

**`start_testrail_run_import(*, project, user, raw_reference) -> TestCaseUpload`**:

1. Raise `TestRailNotConfiguredError` when settings are missing.
2. Parse the reference.
3. Run the in-flight guard as one `exists()` query on
   `(project, testrail_run_id, status in [PENDING, PROCESSING])`, raising
   `TestRailRunImportInProgressError`.
4. Call `get_run` once, inside the request. Map TestRail errors:
   - 400/404 → `TestRailRunNotFoundError`
   - 401 → a credentials message
   - anything else → re-raise `TestRailError`
5. Create the upload with `source=TESTRAIL_API`, `testrail_run_id`,
   `testrail_project_id=run.project_id`, `testrail_suite_id=run.suite_id`,
   `status=PROCESSING` and the label.
6. Dispatch with `celery_app.send_task("projects.tasks.import_testrail_run_cases", args=[upload.id])`,
   then store `celery_task_id`. Dispatching by name keeps the module from
   importing `projects.tasks`.

**`import_testrail_run_cases(upload, *, notify_progress: Callable[[], None]) -> TestRailUpsertCounts`**
is called by the task:

1. Build the vocabularies and mapper.
2. `list(client.iter_run_cases(run_id))`, deduplicated by case ID, keeping order.
3. If the run is empty, raise `TestRailError("TestRail run R… has no tests.", 0)`.
4. Save `total_cases` and call `notify_progress()`.
5. Run `upsert_test_cases_from_testrail(batch_size=50)`. The callback saves
   `processed_cases` and `updated_cases`, then calls `notify_progress()`.

The service owns counter writes; the task owns the broadcast.

Reused unchanged: `build_testrail_client`, `TestRailFieldMapper` and
`upsert_test_cases_from_testrail`.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/projects/<id>/test-cases/import/` `projects:test_case_import` (changed) | GET | `@project_membership_required` | none | `projects/import.html`: `project`, `uploads`, pagination keys, `testrail_picker`. **`upload_create_url` removed** | n/a |
| `/projects/<id>/test-cases/import/testrail/run/` `projects:testrail_run_import` (new) | POST, `@require_POST`, CSRF | `@project_membership_required` | `TestRailRunImportForm` | Redirect to a validated `next` (same-host check via `url_has_allowed_host_and_scheme`), else `projects:test_case_import`. Success message: "Importing TestRail run R123 “<name>”." | Each error is a `messages.error` and the same redirect. Invalid form → "Paste a TestRail run URL or ID.". Otherwise the `.message` of `TestRailNotConfiguredError`, `TestRailRunReferenceError` (incl. host mismatch), `TestRailRunNotFoundError`, `TestRailRunImportInProgressError` or `TestRailError` |
| `/projects/<id>/uploads/create/` `projects:upload_create` | POST | — | — | **removed** | 404 after removal |
| `/projects/<id>/setup/cases/` `projects:setup_cases` (changed) | **GET only** (`@require_GET`; POST now 405) | `@project_membership_required` | none | `projects/wizard/step_cases.html`: `project`, `uploads`, `case_count`, `wizard_step`, `wizard_mode` (unchanged keys) | n/a |

- **Form.** `TestRailRunImportForm` (`projects/forms.py`) has two fields:
  - `run_reference = CharField(max_length=2000, strip=True)`, rendered as a
    `TextInput` with class `input` and `autocomplete=off`.
  - `next = CharField(required=False, max_length=2000, widget=HiddenInput)`.
- **View.** `testrail_run_import(request: AuthenticatedRequest, project: Project) -> HttpResponse`
  in `projects/testrail_views.py`. It runs form → service → message → redirect and
  does no ORM work.
  - It has a small private `_safe_next(request, default) -> str` built on
    `url_has_allowed_host_and_scheme`.
  - That duplicates `views._next_or_default` (four lines). Importing a private
    helper across view modules was judged worse, and moving it to a shared request
    util is unrelated refactoring.
- **New include: `templates/projects/includes/_testrail_run_import_card.html`.**
  - Context contract: `project`, `csrf_token`, and optional `next_url`.
  - Configured state is read from `project.has_testrail_settings`, so the wizard
    doesn't need picker state.
  - Title "Import a TestRail run".
  - When not configured: the "Connect a TestRail account under Settings" hint.
  - When configured, a `.toolbar` form containing:
    - a visually hidden label "TestRail run URL or ID"
    - an `.input` with placeholder `{{ project.testrail_url }}/index.php?/runs/view/123`
    - a hidden `next` when `next_url` is set
    - an "Import run" `.btn-primary`
    - the `.hint` "Paste a run link from TestRail, or an ID like R123. Cases already
      in this project (same TestRail ID) are updated in place."
  - `punk-hazard.css` classes only. No inline styles, no `|safe`.
- **`import.html`.**
  - Header subtitle becomes "Bring cases in from a connected TestRail account: a
    whole suite, or just the cases in one run."
  - Cards, in order:
    1. the account picker (`_testrail_import_card.html`)
    2. the new run card (with `project` and `csrf_token`, `only`)
    3. import history
  - The XML card and the `dropzone` include are removed.
  - The context-contract comment is updated (drop `upload_create_url`; it no
    longer mentions the dropzone).
- **`_testrail_import_card.html`.** Drop "without exporting XML" from the
  not-configured hint.
- **`_import_history_table.html`.** Add an optional muted "Open in TestRail" link
  when `upload.testrail_run_id` is set:
  `{{ project.testrail_url }}/index.php?/runs/view/{{ upload.testrail_run_id }}`.
  `project` is already in context, so no extra query is needed. Old XML rows render
  unchanged: label, status, counts and Delete.
- **`wizard/step_cases.html`.**
  - The import radio's description becomes "Paste a TestRail run link and Punk
    Hazard imports its cases."
  - The import branch replaces the hint, the XML form and `dropzone` with three
    things:
    1. `{% include "projects/includes/_testrail_run_import_card.html" with project=project csrf_token=csrf_token next_url=request.get_full_path only %}`
    2. a `.hint` link to `projects:test_case_import` reading "Import a whole
       suite instead"
    3. the existing upload-status list and the `caseSetupWizard` WS script,
       unchanged
  - Update the header comment to drop the "Uploading a file is a real POST to this
    same view" paragraph. Uploads now arrive via the run-import POST and redirect
    back through `next`; the "WS only updates listed uploads" reasoning still holds.

### Background work and WebSockets

- **New task: `projects.tasks.import_testrail_run_cases(upload_id)`** on queue
  `upload`, with the same options as `import_testrail_cases` (`max_retries=0`,
  `acks_late`, `reject_on_worker_lost`, `soft_time_limit=600`, `time_limit=660`).
  - It is thin: `_fetch_upload` → `_mark_processing` → service call, with a named
    local `notify` that calls `_send_upload_progress(upload)` → `_mark_completed`.
  - On `TestRailError` or `TestRailNotConfiguredError` it calls
    `_handle_failure(upload, exc.message)`; on anything else, the generic message.
- **Removed: `process_xml_upload` and `_process_upload_file`.**
  - Kept helpers: `_send_upload_progress`, `_fetch_upload`, `_mark_processing`,
    `_handle_failure` and `GENERIC_UPLOAD_FAILURE_MESSAGE`. The API tasks still use
    them.
  - `_mark_completed` keeps `upload.file.delete(save=False)`. It is a no-op for API
    rows (an empty FileField); it is harmless and avoids a behaviour change, so it
    is not touched.
- **WebSockets:** no new events; the existing `upload.progress` is reused.
- **Controller:** not involved. No protocol, `CLIENT_VERSION` or capability change.

### Settings / infrastructure

- `auto_tester/settings.py` `CELERY_TASK_ROUTES`: add
  `"projects.tasks.import_testrail_run_cases": {"queue": "upload"}` and remove the
  `process_xml_upload` entry.
- No env var changes. `example.env` is unchanged.
- **Deploy note:** restart the `upload` worker. XML upload tasks still queued at
  deploy time will hit an unregistered-task error, leaving their rows stuck in
  "Importing…". Deploy when none are in flight, or Cancel/Delete such rows from the
  Import page afterwards (cancel works on any row).

### Data flow

1. The member pastes into the run card (Import page or wizard), which POSTs to
   `projects:testrail_run_import`.
2. The decorator resolves `project`, and the form validates the input.
3. `start_testrail_run_import`:
   - checks settings
   - parses and checks the host (pure)
   - runs the in-flight guard (1 query)
   - calls `get_run` (1 HTTP call to the stored base URL)
   - creates the upload (1 insert, 1 update for the task ID)
   - dispatches the task
4. The view flashes and redirects to `next` or the Import page. The new row shows
   "Importing…".
5. The worker:
   - fetches the vocabularies (2 requests) and `get_tests` (ceil(N/250) requests)
   - sets `total_cases` and broadcasts
   - upserts in batches of 50 (1 select, 1 transaction and 1 broadcast per batch)
   - marks the row completed and broadcasts. The page reloads the row, and the
     wizard bumps `caseCount`.
6. On failure: the row is Failed with TestRail's message. Rows this import created
   are deleted, and updates to existing rows stay (existing behaviour).

## Alternatives considered

- **Fully synchronous import.** Too many TestRail round trips (plus 429 back-off)
  inside a request, and no progress, cancel or history.
- **All in Celery, including `get_run`.** Typos, host mismatches and "not found"
  would surface as failed history rows, and the label would be unknown at creation.
- **`get_case` per test.** N requests and rate-limit risk. Tests already carry the
  case fields.
- **`get_cases` for the suite, then filter.** Reads the whole suite. Kept only as
  the documented fallback (see Risks).
- **Branching inside `import_testrail_cases`.** Mixes two use cases in one task.
- **Keeping XML as a hidden or deprecated path.** The user asked for full removal.
- **Wizard alternatives after XML removal:**
  - linking out to the Import page only (a worse onboarding flow)
  - embedding the account/suite picker (needs picker state and the multi-suite
    picker page to return into the wizard)

  The run card with `next` is the smallest change that keeps the wizard functional.

## Risks and mitigations

- **SSRF via the pasted URL.** The pasted host is only compared, never requested.
  The client is always built from `project.testrail_url`. The verifier must confirm
  this.
- **Open redirect via `next`.** It is validated with `url_has_allowed_host_and_scheme`
  against `request.get_host()`, and the Import page is the fallback.
- **XSS.** Messages echo the pasted host and the run name, and the label holds the
  run name. All of them are autoescaped (`partials/_message_alert.html`,
  `{{ upload.original_filename }}`, `|escapejs` in the delete trigger and the wizard
  JSON).
- **`get_tests` lacking `custom_*` on some instances** would import empty steps,
  expected results and preconditions. This is a blocker check in Verification. If
  hit, switch `iter_run_cases` to the `get_cases`-suite-filter fallback; the change
  is contained in the client and service.
- **TestRail before 6.7** returns an unpaginated list. `_paginate` would raise. Suite
  import has the same limitation, so this is accepted.
- **Duplicates with XML-era `C123` rows** are accepted (Q1).
- **Concurrent imports of overlapping case sets** can race and create duplicates
  because there is no unique constraint. The guard only covers the same run. This is
  an existing, accepted risk.
- **Removing `process_xml_upload`** strands queued XML tasks at deploy (see the
  deploy note).
- **Removing XML from the wizard** changes onboarding for users without TestRail
  credentials. They must connect TestRail under Settings or choose "Create manually".
  This is a deliberate consequence of retiring XML.
- **Dead-code removal breaking imports.** Every removed symbol was checked with
  `git grep`, and the list in Context is exhaustive. `mypy .` in step 7 catches any
  miss.

## Implementation steps

### Foundations (serial, before the parallel parts; one agent or the orchestrator)

1. **Model.** Files: `projects/models.py` (add `testrail_run_id` and
   `is_testrail_run_import`; delete the `ParsedTestCase` dataclass),
   `projects/migrations/0016_testcaseupload_testrail_run_id.py` (via
   `makemigrations`), `projects/admin.py`.
   - Note: `projects/services.py` still imports `ParsedTestCase` until Part D lands.
     The tree is only expected to type-check after step 7.
2. **Client.** File: `projects/testrail_client.py`. Extend `TestRailRun`; add
   `_parse_test_as_case` and `iter_run_cases`.
3. **Exceptions.** File: `projects/exceptions.py` (new).

### Parallel parts (disjoint file ownership; no file appears in two parts)

- **Part A: run-import service.**
  - Owns: `projects/testrail_run_import_services.py` (new).
  - Exposes:
    - `parse_testrail_run_reference(raw: str, *, testrail_base_url: str) -> int`
    - `build_testrail_run_import_label(run: TestRailRun) -> str`
    - `start_testrail_run_import(*, project: Project, user: CustomUser, raw_reference: str) -> TestCaseUpload`
    - `import_testrail_run_cases(upload: TestCaseUpload, *, notify_progress: Callable[[], None]) -> TestRailUpsertCounts`
  - Dispatches the task name `"projects.tasks.import_testrail_run_cases"`.
- **Part B: Celery tasks and routing** (both the new task and the XML task removal,
  because they share these files).
  - Owns: `projects/tasks.py` and `auto_tester/settings.py`.
  - Adds task `import_testrail_run_cases`, consuming Part A's
    `import_testrail_run_cases` by the signature above.
  - Deletes `_process_upload_file` and `process_xml_upload`.
  - Swaps the routes.
- **Part C: run-import page surface.**
  - Owns: `projects/forms.py` (`TestRailRunImportForm`), `projects/testrail_views.py`
    (`testrail_run_import`, `_safe_next`), `projects/urls.py` (add
    `testrail_run_import`; remove the `upload_create` entry),
    `templates/projects/includes/_testrail_run_import_card.html` (new),
    `templates/projects/includes/_testrail_import_card.html` (wording),
    `templates/projects/includes/_import_history_table.html` ("Open in TestRail" link).
  - Consumes `start_testrail_run_import` and the step 3 exceptions.
  - Card contract: `project`, `csrf_token`, optional `next_url`.
- **Part D: XML removal and page/wizard restructuring.**
  - Owns: `projects/services.py` (delete every XML symbol and import listed in
    Context), `projects/views.py` (delete `_handle_xml_upload` and `upload_create`
    and their imports; drop `upload_create_url`; make `setup_cases` GET-only with
    `@require_GET`), `templates/projects/import.html` (remove the XML card;
    include the Part C card; subtitle and contract comment),
    `templates/projects/wizard/step_cases.html` (include the Part C card with
    `next_url`; copy; header comment), `templates/partials/dropzone.html` (delete
    the file), `static/css/punk-hazard.css` (delete the three `.dropzone` rules),
    `CLAUDE.md` (line 7: "…create projects, import test cases from TestRail (a whole
    suite or a single run) and run them…").
  - Includes Part C's card only through its frozen contract; it does not edit it.
- Seams between parts:
  - The `upload_create` URL name is removed by C (urls.py) while its only `reverse`
    is removed by D (views.py), so it is consistent once both land.
  - D's templates reference `projects:testrail_run_import`, which C adds.

### Integration (orchestrator, not delegated)

7. Wire and check the seams:
   - the view → service import names
   - the task-name string in Part A matching the Part B decorator and route
   - the URL name used in the card
   - the card contract used by `import.html` and `step_cases.html`

   Then run these. Each must be empty or clean:
   - `git grep -n -i -E "xml_upload|parse_testrail_xml|validate_testrail_xml|ParsedTestCase|dropzone|upload_create|is_valid_xml_filename|_handle_xml_upload" -- ':!docs' ':!ui-prototype' ':!*/migrations/*'`
   - `pre-commit run --all-files`
   - `python manage.py check`
   - `python manage.py makemigrations --check --dry-run`

## Verification

There are no automated tests; do not add any.

- **Static checks.** `mypy .`, `pre-commit run --all-files`, `manage.py check` and
  `makemigrations --check` are clean. The `git grep` from step 7 finds no XML
  symbols outside docs, migrations and `ui-prototype/`.
- **Parser smoke** (`manage.py shell -c`, base `https://acme.testrail.io`):
  - Return 123:
    - `…/index.php?/runs/view/123`
    - the same with `&group_by=cases:section_id&group_order=asc`
    - `http://ACME.testrail.io/index.php?/runs/view/123#x`
    - the scheme-less form
    - `R123`, `r123`, ` 123 `
  - Raise the reference error: `0`, `R0`
  - Raise the mismatch error: another host
  - Raise the specific errors: plan URL, test URL
  - Raise the generic error: case URL
  - With a base of `https://example.com/testrail`, only `/testrail/index.php` is
    accepted.
- **Against the user's TestRail instance:**
  1. A raw `get_tests` response includes `custom_steps_separated` and
     `custom_preconds` (the blocker).
  2. Import a run from a single-suite project. You see the flash, then live
     progress, then "N new · 0 updated". Fields match TestRail.
  3. Re-import the same run: "0 new · N updated", with no duplicates.
  4. A quick double submit gets "already being imported".
  5. A run from a multi-suite project and a run inside a test plan both import.
     The row has `testrail_project_id`, `testrail_suite_id` and `testrail_run_id`
     (check in admin). "Open in TestRail" opens the run.
  6. Error paths:
     - unknown run ID → not found
     - wrong host → mismatch
     - broken API key → credentials message
     - TestRail cleared → the card shows the connect hint, and a forced POST says
       "not configured"
  7. Cancel a large in-progress import. Only the rows it created are deleted.
  8. Push-back: create a PH run from the imported cases, finish it, and push. The
     suggested TestRail project is correct, and a new TestRail run is created, as
     before.
  9. A non-member POST is denied and an anonymous user is redirected to login.
     `next=https://evil.example/` falls back to the Import page.
- **XML removal:**
  - The Import page shows only the account picker, the run card and history.
  - Old XML history rows still render, and View/Delete work.
  - `POST /projects/<id>/uploads/create/` returns 404.
  - `POST /projects/<id>/setup/cases/` returns 405.
  - The `upload` worker starts with no `process_xml_upload` registered.
- **Wizard step 3:**
  - Configured project: paste a run, and you land back on step 3. The status card
    goes from importing to complete, and Continue enables.
  - Unconfigured project: a Settings link is shown, and "Create manually" still
    enables Continue.
- Run `reviewer` and `verifier` in parallel. Give the verifier the original request
  plus the user's answers below and this plan path, and point it at SSRF via the
  pasted URL, open redirect via `next`, and XSS via run names and hosts.

## Open questions

All questions are resolved by the user. They are kept here for the record:

- **Q1 – Dedup across XML (`C123`) and API (`123`) IDs.** Resolved: no matching
  change; duplicates are acceptable. Additionally, remove the XML upload flow
  entirely in this change.
- **Q2 – Push into the source run.** Resolved: no. Push to a new TestRail run as
  today. Only record `testrail_run_id` on the upload.
- **Q3 – Auto-create a Draft run.** Resolved: no.
- **Q4 – Paste field location.** Resolved: the Import page only. After removal, the
  Import page offers the suite import and the run import.
  - Planner note: wizard step 3 also hosted XML upload. It now reuses the run-import
    card so onboarding still has an import path; this is not a new entry point.
    Confirm if the wizard should instead just link to the Import page.
- **Q5 – "View test cases" shows only created rows.** Resolved: acceptable.
- **Q6 – Closed runs.** Resolved: allowed, with no warning.
- **Q7 – Host matching.** Resolved: ignore the scheme and default ports; exact
  case-insensitive hostname; no aliases.

## Changelog

- 2026-10-22: created (Draft).
- 2026-10-22: user answers applied; Status → Ready. Q1 changed scope:
  - Added full XML upload removal (Part D), with an exhaustive file and symbol list.
    Model fields and history rows are kept, and there is no destructive migration.
  - Dropped the `_upsert_batch` `C`-prefix change.
  - Q2–Q7 confirmed as assumed.
  - Wizard step 3 rebuilt on the run card (it was XML-only). The run-import form
    gained a validated `next`.
  - Re-partitioned the work so `tasks.py`/`settings.py` belong to Part B,
    `urls.py` to Part C, and `import.html`/`views.py`/`services.py` to Part D.
- 2026-10-22: review pass 1 fixes:
  - `send_task` failure now marks the row FAILED and raises `TestRailError`, so a broker
    outage cannot leave the in-flight guard stuck.
  - The guard and the create run in one transaction under a `select_for_update` on the
    project row, which closes the double-submit race.
  - `TestRailRun.project_id` becomes `int | None` (no fake 0).
  - The view reads `next` from `form.cleaned_data`; the unused widget attrs are dropped.
  - The history table now uses `is_testrail_run_import`.
- Rejected review findings:
  - Task/worker duplication with the suite import: extracting a shared runner means
    refactoring the existing suite flow (its logic lives in `tasks._run_testrail_import`).
    That is unrelated to this plan, so it goes in a separate refactor plan.
  - `_safe_next` vs `views._next_or_default`: already decided in Design; moving it to a
    shared util is unrelated refactoring.
  - Success message wording ("R123: name" vs "R123 “name”"): cosmetic; it reuses the
    stored label.
  - In-request `get_run` latency and 429 retries: these match the suite import flow and
    are accepted, as the plan already notes.
  - Restricting `testrail_url` to non-private hosts: pre-existing and out of scope;
    worth its own plan.
- 2026-10-22: review pass 2 finding fixed (history link hidden when TestRail settings are cleared); pass 3 clean. Status → Done.
