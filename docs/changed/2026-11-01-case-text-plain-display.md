# TestRail case text shown without HTML / Markdown markup

Date: 2026-11-01
Plan: docs/plan/feat-case-text-rich-render.md

## What changed
- `projects/case_markup.py` (new, stdlib only): `markup_to_plain` turns TestRail HTML (rich-text editor) or Markdown into readable plain text; plain text passes through.
- `projects/case_text_display.py` (new): `case_text_display(test_case)` converts preconditions, steps, expected and each numbered step (split first, then converted).
- Test Runner snapshots: Test case tab (`runner_case_services._build_view`), "Covers …" steps (`runner_person_step_services._case_step_payloads`), Manual run tab (`runner_manual_state_services._active_case`).
- Web: run case page "Test case" card (`case_text` context; numbered list for steps-template cases), `case_text` template filter in `_person_step_card.html` and `_manual_case_card.html`, manual-run Steps card rows (`case_step_progress._row_payload`).

## Why
TestRail fields arrive as HTML or Markdown and the raw tags were shown to the tester in the runner window and on the case page.

## Behaviour and contract changes
- Snapshot payload shapes unchanged, only their text; works for every runner version (no CLIENT_VERSION bump).
- New template filter `case_text` (project_extras); new case page context key `case_text`.

## Notes for future changes
- Display only. Raw text stays in edit dialogs, agent/LLM prompts (incl. Machine setup run-state), Ask AI context, TestRail push and manual refinement — never feed `markup_to_plain` output into those.
- HTML is detected only by a closed known element or a br/hr/img tag, so "a < b" or "<username>" stay text. Output is plain `str`; templates must keep autoescaping it.
