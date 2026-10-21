# feat: Run test cases in TestRail order

Status: Done
Created: 2026-10-22
Request: A Punk Hazard test run must execute its cases in exactly the order they
have in TestRail. "TestRail order" means the suite's order: sections in tree order
(each level sorted by the section's `display_order`, depth-first), then cases
within a section by their `display_order`, then by case ID. The user approved this
for both suite and run-link imports. The run results page keeps its failures-first
grouping (Failed, then Blocked, then the rest). Within each group, cases follow
TestRail order instead of today's newest-first. The other run surfaces use the
same order: case detail "Case N of M", the wizard case picker, the "What happened"
title lists, and the push to TestRail.

## Context

- `projects/models.py`
  - `TestCase` stores `testrail_id` (string, `"123"` for API imports and `"C123"`
    for XML-era rows). It stores no section or position.
  - `TestRunTestCase` (the run "pivot") has no ordering field. `created_at` is
    the only time hint, and `bulk_create` gives every row in a batch the same or
    nearly the same value.
  - The latest migration is `0019_blocked_reason_browser`.
- `projects/testrail_client.py`
  - `TestRailCase` is built by `_case_from_fields`, called from `_parse_case`
    (`get_cases`) and `_parse_test_as_case` (`get_tests`). It reads neither
    `section_id` nor `display_order`.
  - `iter_cases(project_id, suite_id)` paginates `get_cases`, and
    `iter_run_cases(run_id)` paginates `get_tests`.
  - There is no `get_sections`.
  - `_paginate` expects the post-6.7 envelope (`{"<key>": [...], "_links": ...}`).
- Suite import: the task helper `projects/tasks.py::_run_testrail_import`
  (legacy: logic in the task module) calls `iter_cases`, then
  `projects/testrail_services.py::upsert_test_cases_from_testrail` →
  `_upsert_batch` → `_apply_data` / `_new_test_case`. Updates go through
  `bulk_update` with `_UPSERT_FIELDS`.
- Run import: `projects/testrail_run_import_services.py::import_testrail_run_cases`
  calls `iter_run_cases`, then `_unique_by_case_id`, then the same upsert. The
  upload row stores `testrail_project_id` and `testrail_suite_id` from
  `get_run`; both are nullable.
- `TestCaseData` (`projects/models.py`) is shared by the manual case form, and
  `update_test_case` does `setattr` for every field in `asdict(data)`. Any field
  added to `TestCaseData` would be wiped on a manual edit, so the order fields
  must **not** go there.
- Run ordering today (`projects/services.py`):
  - `create_test_run_with_cases`, `add_cases_to_test_run` and `rerun_cases`
    bulk-create pivots in whatever order the queryset returns.
  - `start_test_run` builds the Celery `chain` from an unordered
    `pivot_entries.values_list("id")`. This is the execution order, and it is
    effectively undefined.
  - `get_case_position` uses `created_at, id`.
  - `list_test_run_cases` orders by `_failure_priority, -created_at`.
  - `list_failed_case_titles` and `list_blocked_case_titles` order by `id`.
  - `get_run_case_picker` orders by `title` (the contract comment in
    `templates/projects/wizard/step_run.html` says so too).
- `projects/testrail_results_services.py::plan_testrail_push` orders pivots by
  `id`.
- `projects/services.py::_clone_test_case` (duplicate project / copy cases) copies
  case fields explicitly.
- Nothing else sorts pivots by run order:
  - The consumer's `fetch_test_run_state` returns an id→status map.
  - Reset, abort and status-rollup do not depend on order.
- Versions: Django ~=5.2.11, Postgres (psycopg ~=3.3.2), Celery ~=5.6.2,
  httpx ~=0.28.1.

### TestRail API behaviour this relies on

- Bulk GET endpoints return records **ordered by ID**, not by UI order. Sorting
  must therefore happen client-side.
- `get_sections/{project_id}&suite_id=X` returns sections with `id`, `parent_id`,
  `display_order` and `depth`. TestRail documents that "depth, display_order and
  parent fields determine the hierarchy of the sections in a test suite". It is
  paginated in the same envelope as `get_cases` (key `sections`, `_links.next`,
  250 per page). `suite_id` is optional for single-suite-mode projects.
- `get_cases` objects include `section_id` and `display_order` (shown in the
  official example response). `display_order` on cases is **not documented**,
  but in practice it is the case's manual position within its section.
- `get_tests` objects carry `case_id` and the case's content fields. Section and
  display order are not documented on tests and must not be relied on. A run
  import therefore has to fetch `get_sections` and `get_cases` for the run's
  suite to know the order.

## Goals

- Every newly imported or re-imported TestRail case stores its suite and its
  0-based position in TestRail suite order.
- Execution order (`start_test_run` chain) is TestRail order.
- The run results page keeps the failures-first groups and uses TestRail order
  within each group.
- These also follow TestRail order:
  - "Case N of M" on the case detail page
  - the wizard case picker
  - the "What happened" title lists
  - the push plan
- Cases with no TestRail position come after positioned ones, in a stable
  order. This covers manual cases, XML-era `"C123"` rows and rows not yet
  re-imported.

## Non-goals

- The Test Cases list page (`list_test_cases_for_project`, newest first) keeps
  its ordering.
- Manual reordering of cases inside Punk Hazard.
- Following the TestRail *run* view's own grouping or sort settings. The user
  approved suite order.
- Backfilling positions without a re-import. Existing cases stay unpositioned
  until their suite or run is imported again, and no migration calls TestRail.
- Reusing the extra `get_cases` fetch in run imports as the content source. It
  would fix the "steps may import empty from `get_tests`" risk in
  `docs/changed/2026-10-22-testrail-run-import.md`, but it is a separate
  change.
- Deduplicating the suite and run import runners.

## Design

### Data model

`TestCase` gains two nullable, additive fields:

- `testrail_suite_id = PositiveIntegerField(null=True, blank=True)`: the suite
  the position belongs to.
- `testrail_position = PositiveIntegerField(null=True, blank=True)`: the 0-based
  rank in that suite's TestRail order.

The suite is stored so that a run mixing cases from two suites runs one suite
after the other. Without it, the positions would interleave (A1, B1, A2, B2, …).

No change to `TestRunTestCase`. Run order is derived at query time from the case,
so no pivot field and no data migration are needed. See Alternatives.

Migration `0020_testcase_testrail_order` adds the two fields with `AddField`
only. It needs no index: run order sorts a single run's pivots joined to
`TestCase` by PK, and the picker sorts one project's cases with `LIMIT 200`.

### Ordering rule (single source)

New module `projects/run_case_order.py`, which holds the domain ordering rule
and has no queries of its own:

- `RUN_CASE_ORDERING`: the ORM ordering for pivots, a tuple of:
  1. `F("test_case__testrail_suite_id").asc(nulls_last=True)`
  2. `F("test_case__testrail_position").asc(nulls_last=True)`
  3. `"test_case_id"`
- `PICKER_CASE_ORDERING`: the same rule on `TestCase`, with the fallback
  `testrail_suite_id`, `testrail_position` (both nulls last), then `title`.

The pivot fallback is `test_case_id` rather than the pivot `id`. It is stable,
and for manual cases it approximates creation order. Both
`projects/services.py` and `projects/testrail_results_services.py` import from
this module, which avoids a cycle between them.

### TestRail order computation

New pure module `projects/testrail_case_order.py`, with no I/O and no ORM:

- `rank_testrail_cases(sections: Sequence[TestRailSection], cases: Iterable[TestRailCase]) -> dict[int, int]`
  returns case ID → 0-based rank:
  - Sections: build children-by-`parent_id`, sort siblings by
    `(display_order, id)`, walk depth-first pre-order, and give each section a
    rank.
  - Cases: sort by `(section_rank, display_order, case id)`.
  - A case whose section is missing from the list sorts after all known
    sections. A missing `display_order` sorts last within its section.
  - It iterates `cases` once and keeps only `(id, section_id, display_order)`,
    so a large suite's full case bodies are not held twice.
  - Guard: a `parent_id` cycle or unknown parent is treated as a root. The walk
    tracks visited IDs and never loops.
- `append_unranked(ranks: Mapping[int, int], case_ids_in_order: Sequence[int]) -> dict[int, int]`
  returns the ranks restricted to `case_ids_in_order`. IDs without a rank are
  appended after the maximum, in the given order. The run import uses it for
  cases `get_cases` did not return, with `get_tests` order as the fallback.

### Client (`projects/testrail_client.py`)

- New `TestRailSection(id, parent_id: int | None, display_order: int, depth: int)`
  frozen dataclass and `_parse_section`.
- New `get_sections(project_id: int, suite_id: int | None) -> list[TestRailSection]`
  paginates `get_sections/{project_id}[&suite_id=X]&limit=250` with key
  `sections`.
- `TestRailCase` gains `section_id: int | None` and `display_order: int | None`,
  read in `_case_from_fields` via `_optional_int`. For tests they will usually
  be `None`, which is harmless.
- `iter_cases(project_id, suite_id: int | None)` leaves out `&suite_id=` when it
  is `None` (single-suite mode). The existing suite import always passes an int.

### Services

`projects/testrail_services.py`:

- New `fetch_testrail_case_ranks(client, *, project_id: int, suite_id: int | None, cases: Iterable[TestRailCase] | None = None) -> dict[int, int]`.
  It fetches `get_sections` and, when `cases` is `None`, also streams
  `iter_cases`. It returns `rank_testrail_cases(...)`. This is the one place
  where the order is fetched from TestRail. It does API I/O only, no ORM.
- `upsert_test_cases_from_testrail` gains required keyword-only parameters
  `suite_id: int | None` and `positions: Mapping[int, int]` (TestRail case ID →
  rank).
  - `_upsert_batch` sets `testrail_suite_id` and `testrail_position` on both
    created and updated rows through a new small `_apply_order` helper.
    `_apply_data` and `TestCaseData` stay unchanged.
  - The two field names are added to the `bulk_update` field list.
  - A case missing from `positions` gets `testrail_position=None`.
  - No extra queries: the same one select, one `bulk_update` and one
    `bulk_create` per batch.

Suite import (`projects/tasks.py::_run_testrail_import`):

- Inside the existing client block, after `cases = list(iter_cases(...))`, it
  calls `fetch_testrail_case_ranks(client, project_id=…, suite_id=…, cases=cases)`.
  That adds only the `get_sections` pages.
- It passes `suite_id=upload.testrail_suite_id` and `positions=` to the upsert.
- It stays a thin addition to existing legacy code and does not grow new logic
  in the task.

Run import (`projects/testrail_run_import_services.py::import_testrail_run_cases`):

- After `_unique_by_case_id(...)`, and when `upload.testrail_project_id` is set,
  it calls
  `fetch_testrail_case_ranks(client, project_id=upload.testrail_project_id, suite_id=upload.testrail_suite_id)`.
  This fetches sections plus the whole suite's `get_cases`.
- It then calls `append_unranked(ranks, [c.id for c in cases])`. The positions
  of the run's cases are therefore the *suite* ranks, the same values a suite
  import would write. A case missing from the suite listing goes last, in
  `get_tests` order.
- If `testrail_project_id` is `None` (not expected), positions fall back to
  `get_tests` order via `append_unranked({}, ...)`.
- A `TestRailError` from the extra calls fails the import through the existing
  path. The import is all-or-nothing, as it is today.

`projects/services.py`:

- `start_test_run` orders `pivot_entries` by `RUN_CASE_ORDERING` before
  `values_list("id")`. The chain order is now TestRail order. Do not touch or
  extend the existing lazy import.
- `get_case_position` is replaced by one query: the run's pivot IDs ordered by
  `RUN_CASE_ORDERING` as a `values_list(flat=True)`. It returns
  `index(pivot.id) + 1` and `len`. That is one query instead of two, and the
  docstring is updated. A run holds hundreds of IDs, so reading them is cheap.
- `list_test_run_cases` orders by
  `("_failure_priority", *RUN_CASE_ORDERING)`. The docstring changes to "TestRail
  order within each group". The annotation and `select_related("test_case")`
  stay as they are.
- `list_failed_case_titles` and `list_blocked_case_titles` order by
  `RUN_CASE_ORDERING` instead of `id`.
- `get_run_case_picker` orders by `PICKER_CASE_ORDERING`.
- `create_test_run_with_cases`, `add_cases_to_test_run` and `rerun_cases` are
  unchanged. Order is derived, so added cases slot into TestRail order and a
  rerun keeps the original relative order automatically.
- `_clone_test_case` copies `testrail_suite_id` and `testrail_position`. A
  cloned case keeps its `testrail_id`, so it should keep its order too.

`projects/testrail_results_services.py`: `plan_testrail_push` orders by
`RUN_CASE_ORDERING`. This is only for consistency, because TestRail shows
results in its own order.

### Pages and forms

No new URLs, forms or context keys. Only the order of existing context changes.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_detail` | GET | `@project_membership_required` | — | `cases`: groups unchanged; TestRail order within each group | unchanged |
| `projects:test_run_case_detail` | GET | `@project_membership_required` | — | `case_position` / `case_total` use run order | unchanged |
| `projects:setup_run` (wizard step 5) | GET/POST | `@project_membership_required` | `TestRunCreateForm` | `test_cases` in TestRail order, then title; update the contract comment in `templates/projects/wizard/step_run.html` | unchanged |

### Background work and WebSockets

- `execute_test_run_case` (queue `execution`): the chain order changes. The
  task itself does not change.
- Import tasks (queue `upload`): the run import makes `ceil(suite_cases/250)`
  extra `get_cases` calls plus the `get_sections` pages. The suite import adds
  only the `get_sections` pages. Both fit easily within the 600 s soft limit for
  normal suites (a 10k-case suite is about 40 extra calls).
- No WebSocket event or controller protocol change. Controllers are unaffected.

### Settings / infrastructure

None. Restart the `upload` and `execution` workers on deploy.

### Data flow

1. Import (suite or run) → client fetches cases, sections (and the suite's
   cases, for a run import) → `rank_testrail_cases` →
   `upsert_test_cases_from_testrail(..., suite_id, positions)` writes
   `testrail_suite_id` / `testrail_position` on each upserted case.
2. Create a run, add cases or rerun → pivots are created as today.
3. Start → `start_test_run` reads pivot IDs ordered by `RUN_CASE_ORDERING` →
   Celery chain → the cases run in TestRail order.
4. Results page → failures-first annotation, then `RUN_CASE_ORDERING`.

## Alternatives considered

- **Stored `TestRunTestCase.position`, assigned on create, add and rerun**: the
  initially proposed design. Rejected for these reasons:
  - It needs the same ordering rule at three write sites, plus an
    append-after-max rule for adds.
  - It needs a data migration backfilling every existing pivot.
  - It does not fix runs whose cases have no position yet: they would be
    frozen in fallback order even after the user re-imports.
  - Cases added to a waiting run would go to the end instead of into their
    TestRail slot, which contradicts "exactly as TestRail".

  The only advantage is that a finished run's displayed order would not change
  after a later re-import reorders TestRail. That is a cosmetic difference,
  accepted here (see Risks).
- **Per-case `section_id` + `display_order` columns, sorted in SQL**: section
  order is a tree walk and cannot be expressed as a plain `ORDER BY` without a
  recursive CTE. A single precomputed rank is simpler.
- **Sort by `get_tests` order for run imports**: bulk endpoints are ordered by
  ID, so this is not TestRail order. It is kept only as the last-resort
  fallback.

## Risks and mitigations

- **Undocumented `display_order` on cases.** If an instance omits it, cases
  still group by section in section order and fall back to case ID within a
  section. In the manual check, compare against the TestRail UI.
- **Run imports now read the whole suite.** For very large suites this adds API
  time and some 429 retries (already handled). Only `(id, section_id,
  display_order)` is kept in memory while ranking.
- **Order of finished runs can shift** after a re-import reorders TestRail,
  because order is derived. Results are unaffected and "Case N of M" may shift.
  Accepted.
- **Existing data is unpositioned until re-import.** Runs made of those cases
  keep a stable but non-TestRail order (`test_case_id`). Mention this in the
  `docs/changed` note: users should re-import the suite or run once.
- **Manual edits to `testrail_id`** leave a stale position until the next import.
  Acceptable.
- **Join in `list_test_run_cases` / `start_test_run`.** `list_test_run_cases`
  already uses `select_related("test_case")`. `start_test_run` and
  `get_case_position` add a single PK join and stay at one query each. There is
  no N+1.
- **Migration** is additive and nullable on `TestCase`. It is safe on populated
  tables.
- **No secrets, auth, templates with user data or controller compatibility are
  touched.**

## Implementation steps

1. Model and migration: add `TestCase.testrail_suite_id` and
   `testrail_position`, then `makemigrations` → `0020`. Review that it is
   AddField only. (files: `projects/models.py`,
   `projects/migrations/0020_testcase_testrail_order.py`)
2. Client: `TestRailSection`, `get_sections`, the new `TestRailCase` fields, and
   `iter_cases` accepting a `None` suite. (files: `projects/testrail_client.py`)
3. Pure ranking: `rank_testrail_cases`, `append_unranked`. (files:
   `projects/testrail_case_order.py` new)
4. Import pipeline:
   - `fetch_testrail_case_ranks`
   - the upsert's `suite_id`/`positions` parameters, `_apply_order` and the
     `bulk_update` fields
   - wiring in both import paths

   (files: `projects/testrail_services.py`, `projects/tasks.py`,
   `projects/testrail_run_import_services.py`)
5. Run ordering:
   - `RUN_CASE_ORDERING` / `PICKER_CASE_ORDERING`
   - their use in `start_test_run`, `get_case_position`,
     `list_test_run_cases`, the title lists and `get_run_case_picker`
   - `_clone_test_case`
   - `plan_testrail_push`
   - the wizard template comment

   (files: `projects/run_case_order.py` new, `projects/services.py`,
   `projects/testrail_results_services.py`,
   `templates/projects/wizard/step_run.html`)
6. `pre-commit run --all-files`.

Parallelism: steps 1 and 2 are shared foundations and go first.

- **Group A** is steps 3 and 4: `testrail_case_order.py`,
  `testrail_services.py`, `tasks.py` and `testrail_run_import_services.py`.
- **Group B** is step 5: `run_case_order.py`, `services.py`,
  `testrail_results_services.py` and the wizard template.

The groups touch disjoint files and share only the model fields from step 1, so
they can go to two parallel sub-agents.

## Verification

- `python manage.py makemigrations --check --dry-run` is clean after step 1, and
  `mypy .` is strict-clean.
- Suite import:
  1. Pick a TestRail suite with nested sections and manually reordered cases
     (drag a case above a lower-ID case).
  2. Import it.
  3. In `manage.py shell -c`, list the project's cases ordered by
     `testrail_position` and compare with the TestRail suite UI top to bottom.
- Run import:
  1. Import a run link whose run includes a subset of that suite.
  2. Check that positions equal the suite ranks from the suite import and that
     the run's cases are in suite order.
- Execution:
  1. Create a run with those cases in arbitrary selection order and start it.
  2. Watch the `execution` worker log or the run page. Cases must start in
     TestRail order.
- Results page:
  - After the run finishes with mixed results: Failed, then Blocked, then the
    rest, each group in TestRail order.
  - "Case N of M" matches the execution sequence.
- Add cases to a waiting run: the added case appears in its TestRail slot.
- Rerun of the failed cases: TestRail relative order is kept.
- Unpositioned data: a manual case and an old unre-imported case sort after the
  positioned ones, stably.
- Wizard step 5 picker lists cases in TestRail order.
- Run `reviewer` and `verifier` in parallel on the diff.

## Open questions

- None blocking. The user approved the ordering rule and the failures-first
  grouping. One point to confirm only if it matters to the user: a finished
  run's displayed order can change after a later re-import changes the TestRail
  order. Assumption used in this draft: acceptable, because order is derived
  live rather than frozen per run.

## Changelog

- 2026-10-22: created. Chose derived ordering over the proposed
  `TestRunTestCase.position` (see Alternatives). Added
  `TestCase.testrail_suite_id` alongside the position, so mixed-suite runs do not
  interleave.
- 2026-10-22: review round 1. Applied: `fetch_testrail_case_ranks` always
  streams the suite (suite import ranks its already-fetched cases directly);
  dropped unused `TestRailSection.depth`; malformed `section_id`/`display_order`
  mean "no order" instead of failing the import; a run import with no suite id
  whose order fetch fails keeps `get_tests` order instead of failing.
  Rejected: a lean "order keys only" case iterator for run imports. The HTTP
  pages are the real cost and stay either way; parsing them once in the upload
  worker is cheap and the ranker already keeps only three ints per case.
- 2026-10-22: review round 2. Section `parent_id`/`display_order` are parsed
  leniently too. The suite import calls the new `rank_suite_cases` service
  (sections fetch + rank of given cases), which `fetch_testrail_case_ranks`
  also uses, so the task holds no ranking logic and there is no mode flag.
