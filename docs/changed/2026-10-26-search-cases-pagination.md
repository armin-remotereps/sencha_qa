# search_test_cases returns preconditions and steps, paged by number

Date: 2026-10-26
Plan: docs/plan/feat-search-cases-pagination.md

## What changed
- `machine_setup/services/case_catalog.py::search_cases`: Django `Paginator` over a `.values()` page (two queries). Every case shows its preconditions and steps, each fetched and capped via `Substr` at `SEARCH_FIELD_LIMIT` (2000). `SEARCH_PAGE_SIZE` drops from 50 to 5. It returns `str`, with a "Page N of M … Has previous page … Has next page …" header.
- `machine_setup/services/setup_tools.py`: `search_test_cases(query?, page?)`. `offset` is replaced by `page` (from 1, validated by `_optional_page`). The description mentions the cap and says to use `get_test_cases` when a field is truncated.
- `machine_setup/services/setup_prompt.py`: the tool list and paging hint mention `page=N`. METHOD says to page through `search_test_cases` without a query to see each case's own preconditions, and to skip the steps it also shows.

## Why
An agent planning S0 for a run should be able to read every case's preconditions by paging through one tool, instead of reading previews and then calling `get_test_cases` in batches.

## Behaviour and contract changes
- Setup agent tool contract: `search_test_cases` takes `page` instead of `offset`. Each page holds 5 full-ish cases instead of 50 previews. No migration or setting. Deploy: restart the workers that run setup turns.

## Notes for future changes
- A page's worst case is about 23k characters, just under `CASES_BATCH_CHAR_BUDGET`. Raising `SEARCH_PAGE_SIZE` or `SEARCH_FIELD_LIMIT` breaks that.
- `list_test_case_preconditions` still pages by `offset`.
