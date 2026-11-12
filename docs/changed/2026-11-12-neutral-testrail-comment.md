# Neutral TestRail result comment

Date: 2026-11-12
Plan: inline (owner request: "no person names, no Runner, no PH run names, no Edited in Punk Hazard line; keep it minimal, just the result")

## What changed
- `projects/testrail_mapping.py`: `build_result_comment` starts with "<Outcome>." only (no "by Punk Hazard in run …", no tester header, no edit line) and runs the result text and step list through `neutral_comment_text`; `build_checked_result_comment` drops the "Checked by <name> on <date>" line; the no-screenshot note no longer mentions Punk Hazard; `format_manual_shots_note` says "N of M screenshots received." Removed `format_manual_result_header`, `format_admin_edit_line`, `MANUAL_UNKNOWN_TESTER`.
- `neutral_comment_text` / `_PERSON_IN_TEXT`: strips the person and "Test Runner" from the lead lines other services write into results and step notes (help/person-step marks, Apply's "Result changed … by …", "a person (<name>)", paused-by clauses, Test Runner fault labels). Each writer has a pointer comment.
- `projects/testrail_results_services.py`: new `testrail_result_text` (sends: no blocked reason label, "Marked for re-test: <note>" without the person); `pivot_result_text` stays for run triage.
- `projects/testrail_checked_push_services.py`, `projects/manual_refinement_services.py`, `projects/manual_current_submission.py`: dead tester/checker/edit-marker fields, joins and params removed.
- Migration `projects.0065_neutral_testrail_comment_hashes`: data only, marks already pushed rows `LEGACY_COMMENT_HASH` (as 0044) so the next whole-run Push does not re-send them.

## Why
Results sent to the client's TestRail must read as plain results: no people, tools, PH runs or PH itself.

## Behaviour and contract changes
- TestRail comment format for Push and Send checked (above). Run triage input unchanged.
- Migration 0065 (data only). Rows marked legacy are re-pushed only on a status change, as for 0044.

## Notes for future changes
- Any new text that names a person or the Test Runner and can reach a result or step note must either avoid it or get a pattern in `_PERSON_IN_TEXT`. A label containing ")" is not fully stripped.
- Free text typed by testers is sent as written.
- Deferred: the agent's verdict prompt still gets the pauser's label (`orchestrator_prompts.help_verdict_note`), so an AI-written summary may name them in its own words. Passing "a person" to the verdict prompt would close that.
