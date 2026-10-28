# Answer person steps from the Test Runner window

Date: 2026-10-28
Plan: docs/plan/feat-hitl-runner-chat.md

## What changed
- `controller_client/protocol.py`: `CLIENT_VERSION` 0.9.0, optional capability `person_steps_v1` (advertised only when the window works), messages `person_step_state` / `person_step_action` / `person_step_action_result`, handshake field `person_email`, caps and parsers.
- `projects/models.py`: `PersonStepChannel`, `CasePersonStep.reported_via` / `closed_via`; migration `projects.0040_person_step_channel`. `projects/person_step_actor.py`: who answered (web user, runner member or anonymous runner) and its label.
- `projects/person_step_services.py`: actions take `actor` and `expected_round`; panel gains `runner_can_answer`, `reported_at_runner`, `reporter_label`. `person_step_rows._settle_row`: an overrule without a user is still `overruled`.
- `projects/runner_person_step_services.py` (snapshot in 1 query, email → active member, `answer_from_runner` with owner check, 20/min/project throttle, round guard), `runner_person_step_push.py` (nudge), `forms.RunnerPersonStepActionForm`.
- `projects/controller_consumer.py`: `person_email` at handshake, snapshot after the ok ack and on nudges (only when changed), `person_step_action` → form → service → result. `services.py`: nudge on run broadcasts (after commit); the download `.env` gains `CONTROLLER_PERSON_WINDOW` / `CONTROLLER_PERSON_EMAIL`.
- `controller_client/person_window.py`, `person_window_app.py`, `person_window_ipc.py`, `person_window_front_app.py`: a Tk window in its own process (stdin/stdout JSON lines, bounded), the manager the runner drives, and macOS front-app restore.
- `controller_client/client.py`, `config.py`, `main.py`, `app_close_policy.py`: email prompt and `.env` save, handshake fields, handlers, answer forwarder, hide-before-action in `_dispatch`, window protected from `close_app`, stopped before a self-update hand-over. `update_layout.install_env_file` writes a CLI-only email into the new `.env`.
- `controller_client/diagnose.py` + `person_window_diagnostics.py`: `diagnose --window`. Setup scripts warn when tkinter is missing.
- Web card: "You can also answer this on the Test Runner window" and "Reported at the Test Runner by …".

## Why
The person doing a Human in the loop step sits at the test machine; they can now read and answer each step there instead of switching to Punk Hazard on another device.

## Behaviour and contract changes
- Migration `projects.0040_person_step_channel` (additive).
- Controller protocol 0.9.0 (`person_steps_v1`, three messages, handshake `person_email`). Older runners keep using the web card; 0.8.0 runners can be updated from the panel.
- New runner settings `CONTROLLER_PERSON_WINDOW` (default true) and `CONTROLLER_PERSON_EMAIL`; `--no-person-window`, `--person-email`.
- Deploy: `migrate`, rebuild the server image, restart Daphne/web and every Celery worker; update runners from the Environment page.

## Notes for future changes
- Accepted by the user: email-only identity. Anyone with the Connection key can answer, overrule or mark failed/blocked any open HITL step of that project in any member's name; every such answer is labelled "via Test Runner".
- The window is hidden (ack within 1.5 s, else killed) before every message except ping, the two person-step messages and `update_runner`. Every screen capture goes through `_dispatch`.
- macOS: Tk always activates Python on show; the previous front app is re-activated before each hide ack. Not yet measured on an unlocked screen (scratchpad `act_probe2.py`). Windows no-activate show and Linux are untested on real machines.
- The snapshot has no blockers on purpose (they went stale and cost ~8 queries per push).
- Verified with pre-commit (strict mypy incl. win32), `manage.py check`, `makemigrations --check`, throwaway-DB smokes (runner services, consumer, web card, HITL web flow), a cross-venv wire test (server → real `ControllerClient` → window stub → server), a real Tk window smoke on macOS, `diagnose --window --no-interactive`, and three reviewer/verifier rounds.
