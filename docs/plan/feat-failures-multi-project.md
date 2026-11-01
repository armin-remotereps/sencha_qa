# Failures page: multi-select project filter, every project for admins

Status: Done

## Request
On `/failures/` the project filter should be a multi-select, and for an admin
it should show all the projects.

## Plan
- `dashboard/services/failure_scope.py` (new): the page's project scope.
  Members: their non-archived projects, writable = writable memberships (as
  before). `has_superuser_project_control` users: every non-archived project,
  all writable (matches `get_project_access` since
  `docs/changed/2026-11-01-admin-all-projects.md`). One query either way.
  Used by every `/failures/` view (list, export, bulk, presets).
  The dashboard widget, attention and machine health stay membership-only.
- `FailedCaseFilters.project_id` → `project_ids: tuple[int, ...]` (sorted,
  distinct); `failed_case_queryset` narrows with `__in`; `encode_filters`
  repeats `project=`. Old `?project=5` links and presets stay valid.
- `FailedCaseFilterForm.project` → `TypedMultipleChoiceField` with
  `CheckboxSelectMultiple`; `selected_project_ids` replaces
  `selected_project_id`.
- `failed_case_run_choices(scope_ids, selected_ids)`: runs with a Failed case
  of the selected projects (none selected → no run filter); labels prefixed
  with the project name when more than one project is selected. One query.
- UI: `templates/partials/multi_select_field.html` (dropdown of checkboxes,
  `<details>`), `multiSelect` Alpine component and `.multi-select` CSS in the
  design system. Closing the dropdown after a change clears the run and
  re-submits (`failureFilters.projectsChanged`), like the old select.

## Changelog
- 2026-11-01: written by the orchestrator directly (small, single-area change).
- Review round 1: fixed `Any`/dead Mapping branch and whole-field invalidation
  on one stale id (`scope_project_data`); commented the double submit. Not
  changed: `project__name` fetched for a single project (negligible) and the
  `.multi-select-panel .checkbox` spacing duplicating `.case-picker`'s (separate
  components, kept local).
