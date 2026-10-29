# Human in the loop: Ask AI, exact how-to steps, HITL by default, project run mode

Date: 2026-10-29
Plan: docs/plan/feat-hitl-ask-ai-and-howto.md

## What changed
- `agents/types.py`, `agents/checkpoint.py`: `PersonHowToItem`, `PersonStepHandOff.how_to` (optional checkpoint key `how_to`, malformed items dropped one by one), `PersonStepAdviceContext`.
- `agents/os_shell.py`: `open_shell_hint`, `shell_syntax_hint` (Windows: Command Prompt only, never PowerShell).
- `agents/services/person_step_prompts.py`, `person_steps.py`: the prepare prompt writes a numbered OS-specific `how_to` (open the right app, the minimal command for the runner's shell, what right/wrong output looks like); `parse_hand_off` keeps the hand-off when items are bad; suggested commands are not checked with `_is_explicit_command` (the case-stated `command` rule is unchanged). The confirm sub-agent sees the suggested commands as fenced data. `format_how_to_text` for old runners. `_fence` now strips markers until none are left (a nested marker could fake a fence).
- `agents/services/person_step_advisor.py`, `person_step_advice_prompts.py`: one tool-free LLM call answering a tester's question about the step; advisory only, all inputs fenced.
- `projects/models.py`: `Project.default_help_mode` (default HITL), `TestRun.help_mode` default HITL, `ProjectRunModeScope`, `CasePersonStep.how_to`, `PersonStepQuestion` (+ `PersonStepQuestionStatus`, one pending per step constraint). Migration `projects.0042_hitl_assist_and_project_mode`.
- `projects/person_step_question_services.py`, `person_step_question_view.py`, `person_step_question_push.py`, `person_step_question_views.py`, `tasks.answer_person_step_question`: ask (throttle 10/min/project, locks project→run→step, 20 per step, lazy fail of a stale pending row), answer outside any transaction with a conditional update, broadcast to the case page and nudge the runner.
- `projects/person_step_services.py`, `person_step_rows.py`: panel gains `how_to`, `has_commands`, `questions`, `can_ask`, `ask_blocker`, `has_pending` (questions only with `include_questions=True`, i.e. the case page); rows store the capped how-to.
- `projects/runner_person_step_services.py`, `controller_consumer.py`: snapshots carry `how_to` and, for `PERSON_STEP_ASSIST_V1` runners, `questions` (asker labels blanked) and `questions_enabled`; older runners get the how-to inlined into the instruction; new `person_step_question` handler → `ask_from_runner` (owner connection, same actor rules).
- `projects/project_run_mode_services.py`, `project_run_mode_views.py`, `testrail_views.render_project_settings`, `templates/projects/settings.html`: "Run mode" card; a superuser who is a writable member sets the project mode and applies it to new runs / drafts / the open run / all runs. The open run is switched through `change_run_help_mode` (hand-back, Night auto-fix, broadcasts reused).
- `projects/services.py`: `create_test_run_with_cases` and `rerun_cases` use the project mode, `duplicate_project` copies it; `projects/case_task_text.py` holds the moved `build_task_description`. The wizard starts its first run with `help_mode=None`.
- Web card (`_person_step_card.html`, `test_run_case_detail.html`, `punk-hazard.css`): "How to do it" list with Copy and the "Suggested by the AI" note, output field whenever a command exists, "Ask the AI about this step" section with live updates (`x-text` only) and sessionStorage drafts for the report and the question.
- Test Runner (`controller_client/protocol.py`, `person_window*.py`, `client.py`): `CLIENT_VERSION` 0.10.0, capability `person_step_assist_v1`, how-to with per-command Copy, collapsible "Ask the AI" section that never hides or raises the window, separate pending slot for questions.

## Why
Testers performing Human in the loop steps needed exact instructions for shell and OS steps (e.g. "Check JAVA_HOME") and a way to ask the AI how to do a step from the test machine. Human in the loop becomes the default, and superusers can set a project's run mode in one place.

## Behaviour and contract changes
- Migration `projects.0042_hitl_assist_and_project_mode` (additive). Every existing project's mode becomes Human in the loop; existing runs keep their mode. Non-superusers now run HITL unless a superuser sets the project to Attended or Night.
- New URLs: `projects:test_run_case_person_ask` (POST, `…/cases/<pivot>/person-step/ask/`, redirects with `?ask=refused` on refusal) and `projects:settings_run_mode` (POST, `<project>/settings/run-mode/`).
- New Celery queue `assist` (task `projects.tasks.answer_person_step_question`) and the `assist-celery` compose service. New setting `PERSON_STEP_QUESTION_TIMEOUT_SECONDS` (default 120).
- Case page socket event `person_step_questions`. Controller protocol 0.10.0: `person_step_question` / `person_step_question_result`, snapshot fields `how_to`, `questions`, `questions_enabled`.
- Re-run as a new run now takes the project's mode, not the source run's.
- Deploy: `migrate`, rebuild the server image, restart Daphne/web and every Celery worker, start `assist-celery`; update runners from the Environment page.

## Notes for future changes
- Accepted: anyone with the Connection key can ask anonymously, and answers draw on the case text and application context. The shared question throttle counts refused attempts, so one person can use it up for a minute.
- AI-suggested commands are shown, never run by the server; prompts forbid destructive commands the step does not require (prompt-enforced).
- The 25-run cap was dropped: services keep at most one STARTED/PAUSED run per project under the project lock.
- Verified with pre-commit (strict mypy incl. controller win32), `manage.py check`, `makemigrations --check`, `sqlmigrate projects 0042`, template compilation, throwaway-DB smoke scripts (question services, query counts), stubbed-LLM how-to and advisor scripts, runner IPC and real-Tk smokes, `diagnose --window --no-interactive`, and four reviewer/verifier rounds. Not yet run with a live runner, a vision worker or the real model, and the web card was not checked in a browser.
