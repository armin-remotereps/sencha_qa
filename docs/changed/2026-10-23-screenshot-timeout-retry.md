# Retry desktop screenshot timeouts before blocking the case

Date: 2026-10-23
Plan: docs/plan/fix-screenshot-timeout-retry.md

## What changed
- `projects/services.py:controller_screenshot`: a reply timeout (`TestRunnerUnresponsiveError`) is re-sent, 3 attempts in total (`_SCREENSHOT_ATTEMPTS`), each with a WARNING log. Only the third timeout propagates, with the text "Test Runner did not return a screenshot after 3 attempts of 30s each".

## Why
A single slow multi-MiB screenshot reply (slow uplink, runner busy with the visual engine) blocked the case as `runner_unresponsive` even though the runner was fine.

## Behaviour and contract changes
- A truly hung runner now blocks the case after ~90s instead of 30s. The Blocked reason is unchanged (`runner_unresponsive`); the message text is new and reaches the UI and TestRail.
- Controller protocol, settings and migrations are unchanged.

## Notes for future changes
- The retry is deliberately screenshot-only: click, type, hover, drag and key press are not idempotent, so do not move it into `_dispatch_controller_action`.
- Each attempt still gets its own reconnect/resend inside `_run_with_reconnect_retry`; reconnect waits stay on the per-case outage budget.
- Browser screenshots (`controller_browser_take_screenshot`) are not runner-local, so their timeout is already an ordinary tool error and is not retried.
- Vision-model (`AIServiceError`) retries remain out of scope; no LLM retry exists yet.
- Not exercised live; verified with mypy/pre-commit and review only. The plan's manual check (temporary `time.sleep(35)` in the runner's `execute_screenshot`) is still to do.
