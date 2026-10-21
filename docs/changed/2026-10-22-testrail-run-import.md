# Import test cases from a TestRail run URL; XML upload removed

Date: 2026-10-22
Plan: docs/plan/feat-testrail-run-import.md

## What changed
- `projects/testrail_run_import_services.py` (new):
  - `parse_testrail_run_reference` (pure URL/ID parser and host check)
  - `start_testrail_run_import` (validates, calls `get_run`, creates the history row, dispatches)
  - `import_testrail_run_cases` (worker: `get_tests`, then the existing upsert)
- `projects/exceptions.py` (new): `TestRailRunImportError` and its subclasses (reference, host mismatch, not found, in progress).
- `projects/testrail_client.py`: `TestRailRun` gains `project_id`, `suite_id` and `plan_id`. New `iter_run_cases(run_id)` paginates `get_tests`; each test carries its case's fields.
- `projects/tasks.py`: new `import_testrail_run_cases` task on the `upload` queue. `process_xml_upload` removed.
- Run-import surface:
  - `projects/testrail_views.py`: `testrail_run_import` view.
  - `projects/forms.py`: `TestRailRunImportForm`.
  - `templates/projects/includes/_testrail_run_import_card.html`: the card, on the Import page and in wizard step 3.
- XML flow removed: services, views (`upload_create`, the POST branch of `setup_cases`), `ParsedTestCase`, `partials/dropzone.html` and the `.dropzone` CSS.
- `_import_history_table.html`: "Open in TestRail" link on run imports.

## Why
Users import exactly the cases of a TestRail run by pasting its link, instead of exporting XML or importing a whole suite. XML upload was no longer wanted.

## Behaviour and contract changes
- New URL `projects:testrail_run_import`: `POST /projects/<id>/test-cases/import/testrail/run/`. It takes the fields `run_reference` and `next`; `next` is validated as same-host.
- Removed URL `projects:upload_create`. `projects:setup_cases` is now GET only (a POST returns 405).
- Migration `0016`: nullable `TestCaseUpload.testrail_run_id`, additive only.
- Celery route `projects.tasks.import_testrail_run_cases` replaces `process_xml_upload`. Restart the `upload` worker on deploy. XML tasks still queued at deploy leave rows stuck in "Importing…"; cancel or delete them from the Import page.

## Notes for future changes
- The pasted host is only compared with `project.testrail_url`, never requested. Keep it that way to avoid SSRF.
- The in-flight guard locks the project row while it checks for an import of the same run and creates the new row. A failed `send_task` marks the row FAILED, so the guard never stays stuck.
- Unverified: whether `get_tests` returns `custom_steps_separated` and `custom_preconds` on the user's TestRail instance. If it doesn't, steps and preconditions import empty; the fallback is to call `get_cases` for the suite and filter to the run's cases (see plan Risks).
- XML-era cases store `testrail_id="C123"`, API imports store `"123"`, and the two are not deduplicated (accepted). `UploadSource.XML` and `TestCaseUpload.file` are kept for old history rows.
- Possible follow-ups:
  - Deduplicate the run and suite import task and worker code into one shared runner.
  - Restrict `testrail_url` to non-private hosts.
