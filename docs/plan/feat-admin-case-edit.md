# feat: Admin edit of a run case's result (status, summary, steps, screenshots)

Status: Done
Created: 2026-11-12
Request (verbatim): "I need a feature so admin can change everything over a case, steps, wording, final result, the summary, screenshots, everything"

Restated: an admin opens a finished case of a run and corrects what the run recorded, by hand and with no AI involved. That covers the final status, the result summary, each step of the plan (its wording, its expected result, its outcome, and its result and failure text), and the screenshots: which ones go to TestRail, deleting one, and uploading a new one. What was recorded before must not be lost silently. The edited result is what the case page, run triage and TestRail show from then on.

Type: `feat`.

## Context

- **What "a case" is here.** A run case is `TestRunTestCase` (`projects/models.py`). Its result lives in four places:
  - `status`, `blocked_reason`, `failed_by`, `result` (the verdict summary text);
  - `execution_checkpoint` (JSON `OrchestratorCheckpoint`, `agents/types.py`). It holds `plan` (`SubTask.description` / `expected_result`, the planner's wording; for a Manual case, the case's own step text), `attempts` (`SubTaskAttempt.result`: `status` pass/fail, `summary`, `error`, `performed_by`) and `decisions` (`StepDecision.reason`);
  - `TestRunScreenshot` rows (`attempt_id`, `step_num`, `testrail_evidence`, AI `relevance`, `manual_key`);
  - for Manual cases, `ManualCaseSubmission` (the hand-in, plus the AI rewrite of notes in `refined_summary` / `refined_step_notes`).
- **The imported case definition** (`TestCase`: title, steps, expected, preconditions) can already be edited on the case page ("Edit test case": `TestCaseForm`, `services.update_test_case`, `can_edit_test_case_in_run`). That edit is shared by every run of the project and changes future runs. This plan leaves it alone (Q2).
- **Single read seams.** Every reader of step rows goes through `projects/case_step_progress.build_case_step_progress`: the case page (`case_step_view`), the WebSocket payload (`pivot_step_payload`), TestRail comments (`testrail_results_services.pivot_step_breakdown`), triage input (`run_triage/services/triage_input.py`) and Machine setup run state (`machine_setup/services/run_state.py`). Manual comments read the checkpoint plan and attempts directly (`manual_refinement_services._comment_parts`). Evidence candidates read the checkpoint attempts (`testrail_evidence.causing_attempt_ids`).
- **What TestRail receives** (`testrail_checked_push_services._plan_pivot`, `testrail_results_services._plan_pivot`, `testrail_mapping.build_*_comment`):
  - `status_id` from `status`;
  - `comment` = header + `pivot_result_text` (blocked reason label / retest lead + `result`, or a Manual case's refined summary) + step breakdown (checkpoint rows, or manual step lines with refined notes) + "Checked by" + screenshot note;
  - `elapsed` from `active_duration`;
  - attachments: the `testrail_evidence=True` screenshots in `(created_at, id)` order. Send checked only; the whole-run 018 push attaches none.
  - There are no per-step custom fields.
- **The closest existing feature** is the result chat Apply (`projects/case_result_change_services.py`, `docs/changed/2026-10-26-case-result-chat.md`):
  - it changes `status` + `result` on Passed/Failed/Blocked cases;
  - it locks project, run, then case (`run_reopen_services.lock_project_and_run`);
  - it refuses during a live send (`list_testrail_push_blockers`);
  - it writes `BlockedReason.PERSON_REVIEW` / `FailedBy.REVIEW`;
  - it clears Checked/Sent (`clear_checked_and_sent`, `CHECKED_SENT_FIELDS`);
  - it keeps the old values on the `CaseResultChange` row as history;
  - it broadcasts with `services.broadcast_run_and_pivots`;
  - it records `CaseActivity.RESULT_CHANGE` for test sessions.
  - Its fingerprint is `result_fingerprint(pivot)` (status, blocked reason, finished_at, result).
- **Checked / evidence** (`run_case_check_services.check_locked_cases`, `testrail_evidence_services.prepare_testrail_evidence`, `testrail_evidence_review_services`, `docs/changed/2026-11-03-testrail-evidence-relevance.md`):
  - flags are recomputed at check time;
  - the review key hashes status, verdict text, candidate ids and manual marks, so editing status or text means a new AI review at the next check;
  - flags change only while `testrail_source_result_id` is null.
- **Manual rewrite.** `manual_refinement_services` replaces the tester's notes with an AI rewrite while the hash of its input matches. After an edit the hash no longer matches, so today the next push would ask the AI to reword the admin's text again.
- **Triage** staleness is computed on read from an input hash over `pivot_result_text` + the step breakdown (`triage_input._input_hash`), so edits mark triage stale with no extra work. **Test sessions** read the live case until frozen. **Credit** (`case_credit.credit_cases`) is moved only by actions that execute or settle a case.
- **Admin** = `projects.project_access.has_superuser_project_control(user)` (active superuser). Superusers already pass `@project_membership_required` as writable, and `relevance` is already "Admin only" on screenshots.
- **Image validation** for uploads exists: `projects/manual_screenshot_services.check_manual_image` / `load_manual_image` (PNG/JPEG, `MANUAL_SHOT_MAX_BYTES` ≤ 7 MiB, `MANUAL_SHOT_MAX_PIXELS`, `verify()` + full `load()`). Django `FILE_UPLOAD_MAX_MEMORY_SIZE` = 10 MB; nginx allows 200M on the site.
- **Reset** (`services.reset_test_run`) deletes `CaseResultChange` rows of the run.
- Versions: Django ~=5.2.11, channels ~=4.3.2, Pillow ~=11.2.1.

## Goals
- An admin edits a finished case (Passed / Failed / Blocked) on one editor page:
  - final status;
  - result summary;
  - for every step of the current plan: wording (description), expected result, outcome (Passed / Failed, for steps that ran), result text, failure/evidence text and the stop/continue decision note;
  - screenshots: TestRail evidence override (auto / always attach / never attach), permanent delete, upload a new image (optionally tagged to a step).
- Every edit leaves an audit row: who, when, the field-level before/after values, and a snapshot of the case's result state before the edit. The newest edit can be reverted.
- The edit becomes the single source of truth. The case page, WebSocket payloads, TestRail comment and attachments, triage and Resume all read the edited data with no overlay logic.
- An edited text reaches TestRail verbatim: the Manual AI rewrite never rewords it. Screenshot overrides beat the AI relevance review.
- Members can see that a case was edited, and by whom.

## Non-goals
- Editing the imported `TestCase` definition. It already exists and is shared across runs.
- Adding, removing or reordering plan steps. Editing earlier (redesigned-away) plans, the log, `active_duration` (TestRail elapsed), credit, or help / person-step history.
- Editing cases that are not finished (Created, In progress, Need help, Your turn, a claimed Manual case, Needs re-test, Cancelled).
- Updating or deleting results or attachments already in TestRail.
- Image editing (crop, blur, annotate) and reordering screenshots.
- Edits by non-admin members. AI-assisted edits: the result chat already covers those.
- A REST/MCP surface for edits.

## Design

### Data model

Migration `projects.0064_case_result_edit`. All additive: no data migration, no backfill.

- **`CaseResultEdit`** (new). One row per saved edit, immutable except for `reverted_by`. Fields:
  - `test_run_test_case` FK CASCADE;
  - `kind` (`CaseEditKind`: `result`, `screenshot_evidence`, `screenshot_delete`, `screenshot_upload`, `revert`);
  - `edited_by` FK `CustomUser` SET_NULL, plus `edited_by_label` (name snapshot) and `edited_at`;
  - `changes` JSON: a list of `{field, step_num|null, before, after}` with each text capped for display;
  - `before_state` JSON, `result` / `revert` kinds only: `status`, `blocked_reason`, `failed_by`, `result` and the full `execution_checkpoint` as it was;
  - `after_fingerprint` (64): the case state after this edit, used to refuse a revert that is out of date;
  - `screenshot` FK `TestRunScreenshot` SET_NULL, plus `screenshot_label` snapshot (tool, step, time) so deletes stay readable;
  - `reverts` FK self, null (on a `revert` row);
  - `reverted_by` FK self, null.
  - Index on `(test_run_test_case, -edited_at)`.
- **`TestRunTestCase`** gets three fields: `admin_edited_at` (null), `admin_edited_label` (CharField 255), `admin_edit_fingerprint` (CharField 64, blank).
  - The marker counts only while `admin_edit_fingerprint == result_fingerprint(pivot)`, compared in Python with no query. Any new verdict (Resume, Re-run, help, Apply, Reset) changes `finished_at` / `status` / `result`, so the marker drops out by itself. No reopen path has to be touched to clear it.
  - These fields are denormalised so batch readers (push planners, manual refinement) need no extra query.
- **`TestRunScreenshot.evidence_override`**: CharField choices `EvidenceOverride` (`""` auto, `include`, `exclude`), default `""`.
- Uploaded screenshots use the new constant `UPLOADED_SCREENSHOT_TOOL_NAME = "uploaded"` (caption "Uploaded") with `capture_source` left at NONE. They are created with `evidence_override=include`.
- No new `BlockedReason` / `FailedBy` values. Changing a case to Blocked writes `PERSON_REVIEW`, and changing it to Failed writes `FailedBy.REVIEW`, as Apply does (the status is assigned from a variable, so the "one literal BLOCKED write" grep rule holds). A case that keeps its status keeps its reason / failed_by.
- Text caps are model constants: summary 20 000, step description / expected 2 000, step result 4 000, step error / evidence 20 000, decision 1 000. The form refuses text over a cap; it never truncates.

### Services (projects app, flat modules as the app already does; `projects/services.py` is legacy and gets nothing new)

- `projects/case_edit_policy.py` (pure, no ORM):
  - `can_admin_edit(user)` wraps `has_superuser_project_control`;
  - `EDITABLE_STATUSES` (Passed / Failed / Blocked);
  - `is_case_editable(pivot)`;
  - user-safe refusal texts.
- `projects/case_edit_checkpoint.py` (pure):
  - takes an `OrchestratorCheckpoint` and the cleaned step edits, and returns the new checkpoint plus the field diff, using `dataclasses.replace` on `SubTask`, the step's final `kind="step"` attempt's `SubTaskResult` and its latest `StepDecision`;
  - refuses an outcome change on a step with no final attempt;
  - never touches `next_step`, `phase`, `help`, `person`, `plan_revisions` or carried attempts' origin;
  - serialises with `agents.checkpoint.checkpoint_to_json`;
  - also computes the "base key": sha256 of `result_fingerprint` + the canonical checkpoint JSON + screenshot ids/overrides. Every form carries it for optimistic concurrency.
- `projects/case_edit_write.py` (the one write path every edit uses):
  - `lock_editable_case(pivot_id, user, base_key)`: locks project → run → case (via `lock_project_and_run`, case `select_for_update().defer("logs")`), then refuses when:
    - the user is not an admin;
    - the case is not editable;
    - `list_testrail_push_blockers(run)` is not empty;
    - the case is in `uploading_manual_case_ids`;
    - the base key differs ("The case changed since you opened the editor. Reload.").
  - `finish_edit(...)`, after the change:
    - `clear_checked_and_sent`;
    - supersedes pending result-chat proposals (`supersede_pending_result_change`, `case_review_redesign_store.supersede_case_review_drafts`);
    - sets the marker fields;
    - writes the `CaseResultEdit` row;
    - schedules `broadcast_run_and_pivots` on commit.
- `projects/case_edit_result_services.py`: `edit_case_result(pivot_id, user, base_key, data)` saves status, summary and all step edits together in one save and one audit row. If nothing changed, it raises `CaseEditError("Nothing changed.")`.
- `projects/case_edit_screenshot_services.py`:
  - `set_screenshot_evidence(...)` (auto / include / exclude);
  - `delete_case_screenshot(...)` deletes the row and removes the file on commit (`transaction.on_commit` → `image.delete(save=False)`; an `OSError` is logged, never raised). The audit row keeps the label and step, not the image.
- `projects/case_edit_upload_services.py`: `upload_case_screenshot(pivot_id, user, base_key, data, mime, step_num)`. It validates with `load_manual_image` (no new limits), requires `step_num` to be in the plan when given, stores through the `ImageField` storage under the existing `screenshots/trtc_<id>/` path with a random name, and sets `attempt_id` to that step's final attempt id ("" when the step has none). At most `CASE_EDIT_MAX_UPLOADS_PER_CASE` = 20 uploads per case.
- `projects/case_edit_revert_services.py`: `revert_case_edit(pivot_id, edit_id, user)`.
  - Only the newest non-reverted `result` edit can be reverted, and only while the case's current state still matches that edit's `after_fingerprint`.
  - It restores `before_state` and writes a `revert` row (with its own `before_state`, so a revert can itself be reverted).
  - Screenshot edits are reverted by acting again (set auto, upload again); a delete cannot be undone.
- `projects/case_edit_page.py` (read side): builds the editor context (form initial values, step rows from the checkpoint, screenshots with their AI relevance + override + "will be attached" preview, the current base key) and `list_case_edits(pivot)` (one query, `select_related("edited_by")`, newest first). Edits go through these services, never through the result chat services or the Django admin.
- **Integration changes in existing modules** (each small and local):
  - `projects/testrail_evidence.py`: `select_reviewed_evidence` takes an `overrides` map. The result is (AI-relevant selection − `exclude`) ∪ every `include` shot of the case that has a file, in `(created_at, id)` order. The caps apply to AI picks only; `include` is always attached.
  - `projects/testrail_evidence_services.py`:
    - `load_case_evidence` also loads `evidence_override`, and `include` shots outside the candidate list;
    - a case whose candidates all carry an override is `done` without an AI call;
    - the override map is not part of `evidence_review_key`, so changing an override never forces a new AI review.
  - `projects/testrail_evidence_review_services.py`: its final flag write applies the same overrides through the same function.
  - `projects/manual_refinement_services.py`: a case whose admin marker is current never sends its notes for rewriting, and its comment uses the edited checkpoint notes and `result` verbatim.
  - `projects/testrail_mapping.py` + both planners: when the marker is current, the comment gets one line, "Edited in Punk Hazard by <label> on <date> UTC." (Q7), under the header. `_PLAN_PIVOT_FIELDS` gains the three marker fields.
  - `projects/services.reset_test_run`: deletes the run's `CaseResultEdit` rows next to `CaseResultChange` (Reset discards verdict history).
- Domain errors in `projects/exceptions.py`: `CaseEditError(message)` and `CaseEditForbiddenError`.

### Pages and forms

All views live in a new `projects/case_edit_views.py` and are decorated with `@project_membership_required` (superusers pass). The view maps `CaseEditForbiddenError` to 403 (`PermissionDenied`) and `CaseEditError` to `messages.error` + redirect back. The ORM is accessed only through services. Forms live in a new `projects/case_edit_forms.py` (forms.py is already ~1200 lines). Every write is a POST that carries `base_key`.

| URL (under `projects/<project_id>/test-runs/<run_id>/cases/<pivot_id>/`) | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `edit/` (`projects:test_run_case_edit`) | GET | member + superuser (403 otherwise; 404 when the case is not in the project/run) | `CaseResultEditForm` + `CaseStepEditFormSet` (formset with a fixed count = plan length, no add/delete) | `projects/test_run_case_edit.html`: `project`, `test_run`, `pivot`, `form`, `step_formset`, `step_rows` (read-only attempt history per step), `screenshots` (view rows with relevance, override, will_attach), `upload_form`, `edits` (history), `base_key`, `editable` / `blocker` text | Not editable → page renders read-only with the reason |
| `edit/` | POST | same | `CaseResultEditForm` (`status` ChoiceField Passed/Failed/Blocked, `result` text, `base_key` hidden) + formset (`description`, `expected_result`, `outcome` (only on steps that ran), `summary`, `error`, `decision_reason`) | redirect to case page with success flash | invalid form → re-render with errors; `CaseEditError` → flash + redirect to the editor |
| `edit/screenshots/<shot_id>/evidence/` (`…_edit_shot_evidence`) | POST | same | `ScreenshotEvidenceForm` (`override` auto/include/exclude, `base_key`) | redirect to editor `#shots` | shot not on this case → 404 |
| `edit/screenshots/<shot_id>/delete/` (`…_edit_shot_delete`) | POST | same | `ScreenshotDeleteForm` (`base_key`, `confirm`) | confirm dialog (`dialog` Alpine component); redirect to editor | 404 / CaseEditError |
| `edit/screenshots/upload/` (`…_edit_shot_upload`) | POST multipart | same | `ScreenshotUploadForm` (`image` FileField, `step_num` optional ChoiceField from plan, `base_key`) | redirect to editor | validation reason from `load_manual_image` shown as a form error |
| `edit/history/<edit_id>/revert/` (`…_edit_revert`) | POST | same | `CaseEditRevertForm` (`base_key`) | confirm dialog; redirect to case page | not newest / outdated → flash |

- **Case page** (`templates/projects/test_run_case_detail.html`, `views.test_run_case_detail`):
  - an "Edit result" button, shown only to admins (`can_admin_edit` in the context);
  - for every member, a line "Edited by <label> on <date>" when the marker is current (same place as "Changed by");
  - an "Edits" history section (`templates/projects/includes/_case_edits.html`) listing each edit's field diffs, escaped;
  - the gallery shows an "Uploaded" caption for uploaded shots;
  - one extra query (`list_case_edits`).
- **Rendering**: all edited text is rendered with the escaping renderers the page already uses (`json_script`, the case-text renderer). No `|safe`, no `mark_safe`. The step outcome select offers only Passed / Failed.
- **Styling**: existing `.card`, `.data-table`, `.btn-*`, `.dialog-backdrop` classes. Any new class goes in `static/css/punk-hazard.css`. No inline styles.

### Background work and WebSockets
- No new Celery task, consumer or controller action. No controller protocol change, so no `CLIENT_VERSION` / capability change.
- On commit, `broadcast_run_and_pivots` updates open run and case pages (status, result, steps) through the existing events. A screenshot delete or upload shows on other open pages after a reload (accepted, as with the evidence badges).
- The next check queues the existing `review_testrail_evidence` (upload queue) when the status or text changed and some candidate has no override.

### Settings / infrastructure
- None. Upload limits reuse `MANUAL_SHOT_MAX_BYTES` / `MANUAL_SHOT_MAX_PIXELS`, and `example.env` is unchanged.
- Deploy: `migrate` (`projects.0064`), then restart web/Daphne and the upload workers (the evidence selection code changed).

### Data flow
1. The admin clicks "Edit result" on the case page. A GET to `edit/` makes `case_edit_page` build the editor from the case, checkpoint, screenshots and history (about 4 queries), including the `base_key`.
2. The admin saves. The view validates the forms and calls `edit_case_result`. `case_edit_write.lock_editable_case` locks project → run → case and checks the admin, editable status, no live send, no Manual upload and the base key.
3. `case_edit_checkpoint` applies the step edits to the checkpoint. The service writes `status` / `blocked_reason` / `failed_by` / `result` / `execution_checkpoint`.
4. `finish_edit` clears Checked/Sent, supersedes the pending result-chat proposals, sets the marker and writes the `CaseResultEdit` (before snapshot + diff).
5. On commit the edit is broadcast, and the view redirects to the case page.
6. Later, a member checks the case. `prepare_testrail_evidence` uses the stored AI review when its key still matches, else queues it; the overrides are applied in the selection. The case is sent with "Send checked": a new TestRail result with the edited status and comment, plus the selected screenshots. The 018 push sees a changed comment hash or status and adds a new result too.
7. Triage shows the case as stale and re-classifies it on the next run of its thread.

## Alternatives considered
- **Overlay layer** (keep the agent's output untouched and store the edits separately, applied on read). Rejected. At least eight readers would each need the overlay: the case page, WS payload, two push planners, triage input, Machine setup run state, manual comment parts, evidence candidates and the refine input. Resume and the agents would then see different data from what people see. The audit row's `before_state` keeps the original anyway, and Revert restores it.
- **Extending the result chat `CaseResultChange` / Apply.** Rejected as the vehicle. It is a proposal workflow (agent proposes, person applies) that covers only status + summary. Its lock order, push blockers, Checked/Sent clearing and reason values are reused instead.
- **Django admin `ModelAdmin` editing.** Rejected. It bypasses the locks, gates, checkpoint validation, audit and broadcast, and the project rules forbid auto CRUD as the production path.
- **"Hide" screenshots (kept but not shown).** Not added: every one of the ~30 screenshot readers would need to filter on it. "Never attach to TestRail" covers the TestRail side and delete covers sensitive images (Q6).
- **Updating the existing TestRail result** (`edit_result`). TestRail results are append-only on most versions; the `edit_result` endpoint is reported only for TestRail 10.4.1+ and is unverified here. Adding a new result works everywhere and matches Apply (Q4).
- **One module per editable field.** Rejected: status, summary and steps are saved in one form as one use case ("edit the case's result"). Screenshots, upload and revert are separate use cases.

## Risks and mitigations
- **Checkpoint corruption.** The edit is a pure, typed transform over `OrchestratorCheckpoint`, round-tripped through `checkpoint_from_json` / `checkpoint_to_json` (version kept). Only the fields listed above change. The before-snapshot allows a revert.
- **Resume after an edit uses the edited plan text and step outcomes.** This is by design (a single source of truth), but it means admin text reaches the agent's prompts. Plan text is already planner text that the agent reads. The editor page states this.
- **Concurrency.**
  - Lock order is project → run → case, as every writer uses.
  - The base key refuses edits made against a stale page.
  - Live sends are refused, and the case must be finished. The agent worker only writes cases it has reopened under the same locks.
  - An in-flight evidence review task may still write relevance; flags are recomputed at the next check.
  - A live result-chat turn may propose against old text; its fingerprint makes Apply refuse it.
- **Already sent to TestRail.** Clearing Sent lets the admin send a new result. The old result and its attachments stay in TestRail, and the latest result sets the test's status. The editor warns when `testrail_source_result_id` or `testrail_pushed_at` is set.
- **AI dependency at send.** A status or text edit changes the review key, so the case waits for an AI review again, as after Apply. Overriding every candidate skips the AI call.
- **Irreversible delete.** Confirm dialog; the audit row keeps the metadata only. Copies already attached in TestRail are not removed (stated in the dialog).
- **Uploads.**
  - PNG/JPEG only, with the existing byte, pixel, `verify()` and `load()` checks and a per-case count cap;
  - the stored name is random and the user's filename is never used;
  - files are served from MEDIA like every other screenshot (same exposure as today).
- **XSS.** All edited text is user data, rendered escaped. The diff view escapes `before` / `after`.
- **Authorization.** The superuser check is enforced in the services (`lock_editable_case`), not only in the template and views. Read-only members and non-admin members get 403 on every edit URL.
- **Audit size.** `before_state` holds a full checkpoint per edit (tens of KB at most, TOAST-compressed); edits are rare. Measure on a large Manual case during verification.
- **Manual case notes.** The marker stops the AI rewrite. If the marker drops out (a new verdict), normal rewriting resumes.
- **Test sessions / credit.** Edits record no session activity and move no credit (Q11). Open sessions show the edited status live, as with any other change.

## Implementation steps
1. **Shared foundation** (orchestrator, before any sub-agent): models, choices, constants, migration `0064`, exceptions. (files: `projects/models.py`, `projects/migrations/0064_case_result_edit.py`, `projects/exceptions.py`)
2. **Pure modules**: policy and checkpoint transform + base key. (files: `projects/case_edit_policy.py`, `projects/case_edit_checkpoint.py`)
3. **Shared write path**: lock, gates, finish/audit/broadcast. (files: `projects/case_edit_write.py`)
4. **In parallel after 1–3**:
   - a. Result + steps service and forms. (files: `projects/case_edit_result_services.py`, `projects/case_edit_forms.py`)
   - b. Screenshot evidence/delete and upload services. (files: `projects/case_edit_screenshot_services.py`, `projects/case_edit_upload_services.py`)
   - c. Revert service and read side. (files: `projects/case_edit_revert_services.py`, `projects/case_edit_page.py`)
   - d. TestRail/evidence integration: overrides in selection, skip AI when all overridden, manual rewrite skip, "Edited" comment line, planner field lists. (files: `projects/testrail_evidence.py`, `projects/testrail_evidence_services.py`, `projects/testrail_evidence_review_services.py`, `projects/manual_refinement_services.py`, `projects/testrail_mapping.py`, `projects/testrail_checked_push_services.py`, `projects/testrail_results_services.py`)
5. **Views, URLs, templates**: editor page, case page button / "Edited by" line / history include, upload caption. (files: `projects/case_edit_views.py`, `projects/urls.py`, `projects/views.py` (context keys only), `templates/projects/test_run_case_edit.html`, `templates/projects/includes/_case_edits.html`, `templates/projects/test_run_case_detail.html`, `static/css/punk-hazard.css` if needed)
6. **Reset** deletes edit rows. (files: `projects/services.py`, one line next to the `CaseResultChange` delete)
7. **Integration pass**: `pre-commit run --all-files`, `python manage.py check`, `makemigrations --check`, then document in `docs/changed/2026-11-xx-admin-case-edit.md` and CLAUDE.md (one bullet under Need help / Key Patterns).

## Verification
Manual checks (no automated tests in this project):
1. As a non-superuser writable member: no "Edit result" button; a GET or POST to each edit URL returns 403. A read-only member also gets 403.
2. As a superuser on a Failed agent case:
   - change it to Passed, reword the summary, reword step 3 and flip it to Passed;
   - the case page, run page counts, step list and WebSocket update on a second tab all show the edit;
   - the "Edited by" line and history show the diff;
   - Checked/Sent are cleared.
3. Check + Send checked against a TestRail sandbox run. The new result has the edited status, comment, step lines and the "Edited in Punk Hazard" line. A step with "always attach" is attached, "never attach" is not, and an uploaded shot is attached.
4. Repeat on a Manual case: the comment uses the admin's notes verbatim (no AI rewrite in the logs). Editing while its hand-in is still uploading is refused.
5. Edit an already-sent case: a warning appears; after the edit, a new result is added on Send and the old one is untouched.
6. Stale editor: open the editor in two tabs, save in one; the other is refused with "The case changed…".
7. Start Send checked and try to save: refused. Try to edit a Need help / In progress / Needs re-test case: the page is read-only with the reason.
8. Revert the newest edit: the original status, summary and step texts come back. Reverting an older edit is not offered. After a Resume of the case the marker disappears.
9. Upload: a 0-byte file, a renamed `.txt`, a PNG over `MANUAL_SHOT_MAX_BYTES` and an over-pixel image are all refused with the right reason.
10. Delete: the file is gone from MEDIA, the history row reads "Deleted screenshot (step 4, take_screenshot, …)".
11. Triage on the run shows the edited case as stale.
12. Query counts: the editor GET ≤ 6 queries, the save ≤ 10 (`django.db.connection.queries` in a shell smoke script, not committed).
13. `mypy .` strict and `pre-commit run --all-files` are clean.

Then run the `reviewer` and `verifier` agents in parallel; `verifier` gets the verbatim request and this plan path.

## Open questions

Resolved 2026-11-12: the user accepted every recommended default (Q1–Q12) as written below.

- **Q1 Who counts as "admin"?** An active superuser only (`has_superuser_project_control`), or any writable project member? (Assumption in this draft: superusers only. Writable members keep the AI-mediated result chat.)
- **Q2 Which "case" is edited?** The run case's recorded result (status, summary, steps, screenshots) for this run only, or also the imported test case definition (title, steps, expected, preconditions) shared by all runs? (Assumption: the run case only. The shared definition is already editable with "Edit test case" on the case page and stays as it is.)
- **Q3 Overwrite or keep the original?** (Assumption: overwrite in place so every page, TestRail, triage and Resume agree. Every edit stores a full before-snapshot and a field diff, and the newest edit can be reverted. The original agent output is visible in the Edits history.)
- **Q4 A case already sent to TestRail.** Should an edit (a) clear Sent so a new TestRail result can be sent, (b) try to update the existing TestRail result in place (needs TestRail 10.4.1+ `edit_result`, not verified for your instance), or (c) be refused? (Assumption: (a), as the result chat Apply does. The old TestRail result and its attachments stay; nothing is sent automatically, and the admin re-checks and sends.)
- **Q5 Screenshot upload: yes or no?** (Assumption: yes. PNG/JPEG with the same size and pixel limits as Manual run screenshots, at most 20 per case, optionally tied to a step, attached to TestRail by default.)
- **Q6 Removing screenshots: permanent delete, or hide while keeping the file?** (Assumption: permanent delete with a confirm, useful for screenshots showing secrets, plus a per-screenshot "always / never attach to TestRail" switch. No "hidden but kept" state.)
- **Q7 Should TestRail show that a result was edited?** (Assumption: yes, one line "Edited in Punk Hazard by <name> on <date> UTC." in the comment. Inside Punk Hazard every member sees "Edited by <name>".)
- **Q8 How far do step edits go?** (Assumption: wording, expected result, result and failure text and the decision note on every step of the current plan; outcome Passed ↔ Failed only on steps that ran. No adding, removing or reordering steps, and no outcome for steps that never ran.)
- **Q9 Which cases are editable?** (Assumption: Passed, Failed and Blocked only, in any run state, while no TestRail send is live and no Manual upload is in flight. Needs re-test cases must be undone or re-run first.)
- **Q10 Elapsed time and the log.** Should TestRail elapsed (`active_duration`) or the case log be editable? (Assumption: no.)
- **Q11 Test sessions and credit.** Should an admin edit count as test-session activity or move the case's credit to the admin? (Assumption: neither.)
- **Q12 Should saving an edit also mark the case Checked?** (Assumption: no. The edit clears Checked and the admin checks it as usual.)

## Changelog
- 2026-11-12: created (Draft).
- 2026-11-12: all open questions resolved with the recommended defaults; In progress.
- 2026-11-12: implemented. Deviations: URLs are `projects:test_run_case_result_edit*` under `cases/<pivot_id>/result/edit/` (the name `test_run_case_edit` already existed); revert offered only in the editor; upload takes no `mime` (sniffed from bytes).
- 2026-11-12: review round 1 fixes: CRLF normalisation; `omitted` keeps its "not relevant" meaning; marker set only by result edits and restored on revert; `LockedCase` to cut save-path queries; upload stored before locks; decision note editable whenever one exists; `CaseEditNotFoundError`.
- 2026-11-12: review round 2 fixes: broadcast `on_commit(robust=True)`; evidence overrides scoped to the verdict (`evidence_override_finished_at`, `testrail_evidence.effective_override`). Round 3 clean. Done.
