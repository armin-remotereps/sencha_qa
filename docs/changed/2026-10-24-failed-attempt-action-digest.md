# Recovery and retry know what the failed attempt did

Date: 2026-10-24
Plan: docs/plan/fix-failed-attempt-action-digest.md

## What changed
- `agents/attempt_digest.py` (new, pure): `build_attempt_digest(messages)` turns a sub-agent transcript into numbered tool-call lines (compact args, result head) plus the last observation. Capped at `ATTEMPT_DIGEST_MAX_CHARS` (6000); when it does not fit it keeps the first and last calls. Images become `[image]`.
- `agents/types.py`: `SubTaskResult.actions_digest` (empty unless failed).
- `agents/services/sub_agent.py`: `_parse_sub_task_result` fills the digest on every fail path (timeout, max iterations, `RESULT: FAIL`).
- `agents/services/orchestrator_prompts.py`:
  - the evaluate prompt shows the digest and asks for a recovery task that does not repeat actions already in effect;
  - `format_failed_attempt_state(step, result, with_actions=)` is the one formatter.
- `agents/services/orchestrator.py`:
  - the recovery sub-agent's state gets the failed line with its actions;
  - after a passed recovery, `state_lines` get a one-line failed entry and `_Progress.retry_notes[step]` holds the version with actions, shown only while that step is retried;
  - `_rebuild_state` (was `_rebuild_state_lines`) rebuilds both on resume.
- `agents/checkpoint.py`: each attempt stores `actions_digest`. The key is optional, read leniently, and the version is unchanged.

## Why
On a timeout, the transcript was thrown away and the summary was just "Timed out after N seconds". So the evaluation, the recovery sub-agent and the retried step started blind, for example retyping input or restarting a long operation.

## Behaviour and contract changes
- Prompts only: the evaluate, recovery and retry prompts carry the failed attempt's actions. The verdict, case page, TestRail and case log are unchanged.
- Old checkpoints load (digest `""`). No migration, setting, URL or protocol change. Deploy: restart the execution workers.

## Notes for future changes
- Keep the live loop and `_rebuild_state` in step; both go through `format_failed_attempt_state`.
- Up to 150 chars of each typed value land in `execution_checkpoint`, the same text the case log already has from `[Tool Call]`.
- Accepted: a second recovery of one step sees both digests (about 12 KB at most, that one prompt). A case interrupted mid-recovery resumes without the in-flight digest.
- Not exercised against a live runner yet (plan Verification, manual step).
