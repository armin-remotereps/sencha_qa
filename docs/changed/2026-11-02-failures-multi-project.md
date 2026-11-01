# Failures page: multi-select projects, every project for admins

Date: 2026-11-02
Plan: docs/plan/feat-failures-multi-project.md

## What changed
- `dashboard/services/failure_scope.py` (new): the `/failures/` project scope. Members keep their non-archived projects (bulk on writable ones); `has_superuser_project_control` users get every non-archived project, all writable. Used by the list, by-case view, CSV export, bulk actions and presets.
- `FailedCaseFilters.project_id` → `project_ids` (sorted tuple); `failed_case_queryset` narrows with `__in`; `encode_filters` repeats `project=`.
- `FailedCaseFilterForm.project`: `TypedMultipleChoiceField` + checkboxes. `scope_project_data` drops ids outside the scope one by one (non-field error), so a stale preset id no longer wipes the whole selection.
- `failed_case_run_choices`: runs of all selected projects (one query), labels prefixed "Project · " when more than one is selected.
- Design system: `templates/partials/multi_select_field.html` (dropdown of checkboxes), `multiSelect` Alpine component, `.multi-select` / `.field-label` CSS. Closing the dropdown after a change clears the run and re-submits (`failureFilters.projectsChanged`).

## Why
Users wanted to filter failures by several projects at once, and admins now control every project (`2026-11-01-admin-all-projects.md`), so `/failures/` shows them all.

## Behaviour and contract changes
- `/failures/` query: `project` may repeat; old `?project=N` links and saved presets still work.
- Reverses "keep `/failures/` membership-only" from `2026-11-01-admin-all-projects.md` (note updated). Dashboard widget, attention and machine health stay membership-only.
- No migration, setting or URL change.

## Notes for future changes
- `RUN_CHOICE_LIMIT` (200) is shared across the selected projects; an older run in a preset may fall out of the run list and be reported as invalid.
- Not exercised against a database (no Postgres available locally): checked with strict mypy, `manage.py check`, black/isort and a no-DB script for the form, encoding and partial rendering.
