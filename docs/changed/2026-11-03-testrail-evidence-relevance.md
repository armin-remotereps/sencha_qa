# AI-reviewed screenshots for TestRail results

Date: 2026-11-03
Plan: docs/plan/feat-testrail-evidence-relevance.md

## What changed
- `projects/testrail_evidence.py`: candidates (causing attempts' shots for agent cases, the current hand-in's shots for Manual), `evidence_review_key`, `select_reviewed_evidence`. The old positional selection and its "last 3 shots" fallback are gone.
- `projects/testrail_evidence_services.py`: `prepare_testrail_evidence` replaces `freeze_testrail_evidence`. It runs at check, query-only, and sets each case to `done` (nothing to review, no source run, or the stored review still matches) or `pending` (queues the review).
- `agents/services/evidence_review.py` (+ prompts): one vision call per case judges each candidate relevant or not, using the log excerpt (agent) or the tester's marks and notes (Manual).
- `auto_tester/image_downscale.py`: JPEG downscale before the AI call.
- `projects/testrail_evidence_review_services.py` + task `review_testrail_evidence` (`upload`): reviews in chunks of 20 with up to 4 threads (no ORM in threads), and writes under the run lock and then the case lock.
- `projects/testrail_checked_push_services.py`: Send checked and the early case send rewrite manual notes, review inline, then send only cases whose review is `done` and current. The panel shows waiting/failed counts. The summary reports "not reviewed".
- `projects/testrail_mapping.py`: the comment notes screenshots left out as not relevant, or that none was attached.
- Pages: "Reviewing screenshots" / "Screenshot review failed" badges (run table, case page, manual card) and a "Retry screenshot review" button.

## Why
When the application crashed mid-step, the attached screenshots showed a blank frame, the desktop or an unrelated window, and confused TestRail readers. The user asked for the AI to decide from the screenshots and the log, to attach nothing when none is relevant, to review Manual cases too, and never to send an unreviewed case.

## Behaviour and contract changes
- Migration `projects.0056_evidence_review`: `TestRunTestCase.evidence_review_state/_key/_error`, `testrail_evidence_omitted`; `TestRunScreenshot.relevance/relevance_note`. Already partly sent checked cases are marked `done`.
- New URL `projects:testrail_evidence_review_retry` (POST, writable members): re-queues failed or never-prepared reviews of checked, unsent cases.
- New Celery task `projects.tasks.review_testrail_evidence` on `upload`.
- New settings `TESTRAIL_EVIDENCE_REVIEW_MAX_IMAGES` (8) and `TESTRAIL_EVIDENCE_REVIEW_TIMEOUT_SECONDS` (90).
- Sends depend on the AI service. During an outage, cases with candidate screenshots stay checked and unsent until a review succeeds. Passed agent cases have no candidates, so they are never blocked.

## Notes for future changes
- Flags and judgments may change only while `testrail_source_result_id` is null; the attachment counter is a resume index into the flagged list.
- The review key does not hash the log: every log writer reopens the case, which clears Checked.
- A transient storage `OSError` fails the review (retryable); missing or undecodable files are "unreadable image" (not relevant).
- Badges do not refresh live (the run page listens only to push status); reload to see them.
- The whole-run 018 "Push to TestRail" attaches no screenshots and is not gated.
