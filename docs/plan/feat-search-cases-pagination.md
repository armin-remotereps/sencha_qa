# feat: search_test_cases returns full preconditions and steps, paged by number

Status: Done
Created: 2026-10-26
Request: The setup agent planning S0 for a run should be able to read all
preconditions in one shot from the query test cases tool. First asked as
"contains steps / contains preconditions" flags; then revised: no flags, always
return steps and preconditions, and page by page number with has next / has
previous, like a paginator.

## Context
- `machine_setup/services/setup_tools.py`: `search_test_cases(query?, offset?)`
  tool definition and `_handle_search_cases`.
- `machine_setup/services/case_catalog.py::search_cases`: pages of 50 cases with a
  200-character preconditions preview, continued via `offset`.
- `machine_setup/services/setup_prompt.py`: project data tool list and METHOD.

## Design
- `search_test_cases(query?, page?)`: `offset` replaced by `page` (from 1,
  default 1, strict integer >= 1).
- `search_cases(project_id, query, page) -> str` uses Django's `Paginator` over a
  `.values()` queryset
  ordered by id: one count + one page query. Every case shows its preconditions
  and steps, each fetched and capped via `Substr` at `SEARCH_FIELD_LIMIT` (2000);
  a truncated field points to `get_test_cases`.
- `SEARCH_PAGE_SIZE` drops from 50 to 5, so a page is at most ~23k characters
  (within `CASES_BATCH_CHAR_BUDGET`). The page size is fixed, so a page number
  always names the same cases.
- Header: "Page N of M (cases a-b of T). Has previous page: yes, page=N-1 / no.
  Has next page: yes, call again with page=N+1 / no." A page past the end returns
  a plain "There is no page N" text.
- Prompt: the tool list shows `page?`, and the paging hint covers `page=N`. METHOD
  says to page through `search_test_cases` without a query to see each case's
  own preconditions, skipping the steps it also shows.

## Non-goals
- No flags. Expected result is not included. `list_test_case_preconditions` keeps
  `offset`. `get_test_cases` is unchanged.

## Changelog
- 2026-10-26: flags dropped at the user's request; char-budget paging replaced by
  page numbers.
- 2026-10-26 review pass 1: page size 10 -> 5 and per-field cap 4000 -> 2000 so a
  page fits the 24k budget; fields fetched with `Substr`; METHOD wording no
  longer contradicts "do not read test steps"; `_search_entry` takes typed
  keyword fields. Rejected: sharing a section helper with `_render_case` — the
  two formats differ on purpose (indent, no expected result) and a helper would
  add a parameter list for two callers.
