# Machine setup intake, artifacts inventory and per-run stage 0

Date: 2026-10-31
Plan: docs/plan/feat-machine-setup-intake.md

## What changed
- New app `machine_readiness`: artifacts profile (base directory per OS, items, notes; versioned, last 20, restore; capped at write by `ARTIFACT_PROFILE_MAX_CHARS`), runner inventory per OS, `Stage0Requirement` with status "until changed", `PreconditionAnalysis` cached per `TestCase.preconditions_md5` (generated column).
- Main chat intake: while a project has no profile, `run_setup_task` and `scan_artifacts` are refused by the server (`setup_tools`) and the lead asks one numbered set of questions, including the artifacts base directory for the runner's OS (`setup_prompt._build_intake`). Skip intake (chat banner and tab) releases the gate.
- Main chat tools (`machine_setup/services/readiness_tools.py`): `get_artifact_profile`, `save_artifact_profile`, `scan_artifacts`, `get_gap_report`, `analyse_preconditions`, `list_stage0_groups`, `get_run_stage0`, `record_requirement_status`, `propose_action`; `propose_test_runs` gains the `stage0_group` selector.
- Approval cards (`SetupActionApproval`, `machine_setup/services/action_approval*.py`): one card per install, upgrade, uninstall or base-directory data removal; `run_setup_task(approval_id=)` claims one approved action (≤ 2 attempts) and the server injects its text as APPROVED ACTION; the executor refuses any other software change.
- The main chat no longer needs a READY machine to send; machine tools answer "not connected" instead. Help, result and auto-fix threads keep the READY gate.
- "Machine readiness" project tab: profile edit and history, inventory, gap report, requirements page (edit, status, Analyse).
- Run page "Machine for this run" card (Waiting and Paused runs): stage-0 checklist, Check machine (no LLM, `stage0` socket event), Prepare machine (sends "Prepare the machine for run «name» (#id)" to the chat); "Ready to run?" gets a warning-only `machine_prepared` item.
- Planner: `run_orchestrator(artifacts_profile=)` adds a fenced ARTIFACTS PROFILE block (absent profile → prompt unchanged); help and result chat leads get the same block. Tester sub-agents never see it.
- Test Runner 0.11.0, `ARTIFACT_SCAN_V1`: `scan_directory` and `inspect_paths` (`controller_client/artifact_scan.py`; stat only, no symlinks or junctions, caps on depth, entries and time; network paths refused unless the server passes `allow_network`).

## Why
Machine setup guessed where the test artifacts were and never asked for the base directory, and installs ran on a chat go-ahead. The intake, the deterministic inventory and the per-run checklist make setup faster and the approval cards make every software change an explicit person's decision.

## Behaviour and contract changes
- Migrations: `machine_readiness.0001`, `projects.0051` (generated column; rewrites `projects_testcase` under an exclusive lock — run in a quiet window), `machine_setup.0009`.
- Settings / `example.env`: `ARTIFACT_SCAN_MAX_ENTRIES`, `ARTIFACT_SCAN_MAX_DEPTH`, `ARTIFACT_PROFILE_MAX_CHARS`, `STAGE0_ANALYSIS_BATCH_CHARS`, `STAGE0_ANALYSIS_MAX_CALLS`, `STAGE0_QUICK_CHECK_BUDGET_SECONDS`. Tasks: `projects.tasks.scan_artifacts_task`, `projects.tasks.quick_check_task` (`assist`), `machine_readiness.tasks.analyse_preconditions_task` (`upload`; also queued after every TestRail import).
- URLs: `machine_readiness:*` under `projects/<id>/machine-readiness/` (overview, artifacts edit, version restore, intake skip, requirements, analyse, requirement update), `projects:machine_readiness_scan`, `projects:test_run_machine_check`, `projects:test_run_prepare_machine`, `machine_setup:action_approve` / `action_discard`.
- WebSocket: run socket `stage0` event (checklist, outcome or `refused`); setup socket `machine_readiness` event (`profile`, `intake_skipped`, `scan`, `analysis`); setup message payload gains `action_approval`.
- Controller protocol 0.11.0: capability `ARTIFACT_SCAN_V1`, messages `scan_directory`, `inspect_paths`, reply `artifact_scan_result`; `app_check_variants` / `is_network_app_target` shared with the server. Publish the runner bundle; runners ≥ 0.8.0 self-update.
- `llm_client.send_chat_completion` accepts `tool_choice="required"`.

## Notes for future changes
- Import rule: `machine_readiness` imports only `projects.models` / `decorators` / `exceptions`, KB services and the agents LLM client. Runner calls are injected as `RunnerPorts`, built in `projects/machine_readiness_ports.py`; that is why the runner-calling tasks and the scan view live in `projects`.
- Network paths (`\\host`, `//host`, `\\?\`, `\??\`, and app-check name variants) are opened only for a base directory a person entered and paths under it (`machine_readiness/network_paths.py`); agent writes and analysis output never add one. Keep that rule for any new path the runner touches.
- Scan, quick check and analysis each hold a per-project cache lock (`project_locks.py`) released by the task; a new caller must go through `request_*` services, not `send_task`.
- Precondition analysis stays on `upload`; move it to its own queue if imports start waiting behind it. Follow-ups S6–S12 are listed in the plan.
