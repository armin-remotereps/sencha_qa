# feat: Blocked result for cases that could not be executed or judged

Status: Done
Created: 2026-10-22
Request: There is no "Blocked" case result. When the Test Runner disconnects, a
timeout fires, the visual engine (OmniParser) fails or the AI service errors, the
case ends up Failed. Every Failed result means a new Jira ticket for the QE team,
so these false failures are expensive. Add a Blocked result that means "the case
could not be executed or judged because of infrastructure". Record why it was
blocked, show it everywhere results are shown, keep Blocked out of the pass rate,
push it to TestRail as Blocked, and let users re-run blocked cases. Failed then
means only one thing: the agent judged that the application under test did not
meet the expected result.

## Context

**Statuses.** `projects/models.py` has `TestRunTestCaseStatus` (created,
in_progress, success, failed, cancelled) on `TestRunTestCase.status`
(max_length 20), and `TestRunStatus` (waiting, started, done, cancelled) on
`TestRun`. The case's message is stored in `TestRunTestCase.result`. The latest
migration is `0017_project_agent_channel_name`.

**Execution.** `projects/tasks.py:execute_test_run_case` runs on the `execution`
queue with `acks_late` and `reject_on_worker_lost`. Its limits are soft
1800 + 2 × grace and hard 1860 + 2 × grace. It calls
`projects/services.py:execute_test_run_test_case`. A run is a Celery `chain` of
`.si()` tasks (`start_test_run`). The case runs in this order:
`wait_for_controller` → `_wait_for_omniparser_ready` → `_safe_cleanup` →
`agents/services/orchestrator.py:run_orchestrator` → `_finalize_pivot`.

**How Passed and Failed are decided today.**
- `run_orchestrator` plans sub-tasks with an LLM call. Each sub-task runs through
  `agents/services/sub_agent.py:run_sub_agent`, which wraps
  `agents/services/agent_loop.py:_run_agent_loop`. The sub-agent replies
  `RESULT: PASS|FAIL`, and an LLM call evaluates each failure.
- `_build_verdict` sets `pass` only when every sub-task passed. It then makes one
  more LLM call to write the summary.
- `_to_agent_result` maps `pass` to `TASK_COMPLETE` and `fail` to `ERROR`, and
  `_finalize_pivot` maps those to SUCCESS and FAILED.

**Paths where an infrastructure fault becomes Failed today** (all read in code):

| Fault | Where | Outcome today |
|---|---|---|
| LLM error in the agent loop (any status, timeout, connection) | `agent_loop._run_agent_loop` catches `Exception` around `send_chat_completion` and returns `AgentStopReason.ERROR` | The sub-task fails. The orchestrator evaluates it with another LLM call, and the case is usually **Failed** |
| LLM error during plan, evaluate or verdict | `orchestrator._plan_sub_tasks`, `_evaluate_failure`, `_build_verdict` (nothing catches it) | Reaches the `except Exception` in `execute_test_run_test_case`: **Failed**, then re-raised |
| LLM returns an unusable plan or JSON | `OrchestratorPlanError`, `OrchestratorParseError` | **Failed**, then re-raised |
| LLM error inside a tool (vision Q&A, element matching) | `agents/services/tool_utils.py:safe_tool_call` turns the exception into a tool error | The agent sees an error and usually reports FAIL: **Failed** |
| OmniParser `failed` status, or not ready within `CONTROLLER_OMNIPARSER_READY_TIMEOUT` | `_raise_for_omniparser_failure` raises `ControllerActionError`; `_wait_for_omniparser_ready` raises `TimeoutError` | **Failed**, then re-raised |
| `find_element` error reply (`FIND_ELEMENT_FAILED`, `OMNIPARSER_NOT_READY`) or `success: false` | `_raise_for_error_reply` and `_raise_for_find_element_failure` raise `ControllerActionError`, and `safe_tool_call` turns it into a tool error | Usually **Failed** |
| Controller reply timeout while still connected | `_raise_reply_timeout` raises `ControllerActionError`, which becomes a tool error | Usually **Failed** |
| Connection lost twice during one action | `_run_with_reconnect_retry` raises `ControllerActionError("…lost again…")`, which becomes a tool error | Usually **Failed** |
| Runner gone for longer than `CONTROLLER_RECONNECT_GRACE_SECONDS` | `ControllerReconnectTimeoutError` → `_abort_run_after_reconnect_timeout` | Case **Cancelled**, run Cancelled (since the 2026-10-22 reconnect work). This is not Failed, but it is also not reported as a cause |
| Celery soft time limit | `SoftTimeLimitExceeded` is an `Exception` subclass (billiard 4.x / celery 5.6.3) | If raised inside a tool, `safe_tool_call` **swallows** it, the agent continues, and the hard limit kills it. If raised elsewhere it is **Failed**, then re-raised |
| Celery hard time limit | The worker kills the child process; `on_timeout` acks the message (`celery/worker/request.py`) | `finally` never runs, the case stays **In progress**, and the run stays Started (see Non-goals) |
| User Abort or Force disconnect | `abort_test_run` marks in-progress and pending cases Cancelled, then revokes with `terminate=True` | Cancelled. Prefork children reset SIGTERM to the default (`WORKER_SIGRESET` in `celery/concurrency/prefork.py`), so the child dies without running Python handlers. The Abort dialog text ("In-progress cases will be marked as failed") is wrong |

**Confirmed side effect.** The `except Exception` and `except BaseException`
branches in `execute_test_run_test_case` re-raise. A chain fires its next link
only when a task exits successfully (Celery canvas docs: "The callback will only
be applied if the task exited successfully"). So today one infrastructure error
also leaves every later case Not started and the run stuck in Started until
someone aborts it.

**Where results are aggregated and shown.**
- `projects/services.py`:
  - `get_test_run_summary`, `_annotate_run_counts`, `get_latest_runs_by_project`;
  - `_compute_pass_rate_30d` (already `success / (success + failed)`);
  - `list_test_run_cases` (failures first), `build_run_narrative`;
  - `can_rerun_failed_cases` / `rerun_failed_cases` (the `@transaction.atomic` is on the wrong function, `can_rerun_failed_cases`);
  - `reset_test_run`;
  - `list_test_cases_for_project` (last-run status filter, built from the enum by `STATUS_FILTER_CHOICES`).
- `dashboard/services.py`: `_build_run_summary` (7-day pass rate, already excluding non-judged results), `_failed_run_attention_items`, `_summaries_for_runs`, `_build_activity_items`.
- Templates:
  - `templates/projects/test_run_detail.html`: stat cards; the pass rate is `success / total`, which is inconsistent with the overview; JS badge maps; "Failure reason" column; "Re-run failed".
  - `templates/projects/test_run_case_detail.html`.
  - `templates/partials/badge_case_status.html`, `templates/partials/badge_run_status.html`.
  - `templates/projects/includes/_test_run_table.html`, `_overview_runs.html`, `_overview_stats.html`, `_overview_alert.html`, `_project_table.html`.
  - `templates/dashboard/includes/_run_summary.html`, `_recent_projects.html`, `_attention.html`, `_recent_activity.html`.
  - Badge CSS in `static/css/punk-hazard.css`; `.badge-warning` already exists.
- WebSocket: `PivotStatusEvent` and `TestRunStatusEvent` carry `summary`; `CaseStatusEvent` carries `status` and `result` (`projects/consumers.py`).

**TestRail push.** `projects/testrail_mapping.py:_PIVOT_STATUS_TO_TESTRAIL` maps
only SUCCESS to 1 and FAILED to 5. Other statuses are "not pushable"
(`projects/testrail_results_services.py:_plan_pivot`). TestRail's system status
2 is Blocked; ids 1 to 5 are built in and custom statuses start at 6.

**Re-run capability today.** "Re-run failed" creates a new run with the FAILED
cases and does not copy the TestRail target. There is no per-case re-run. The
Test cases page can filter by last-run status and create a run from a bulk
selection.

## Goals
- A new terminal case status, `blocked`, with a required reason category and a
  user-facing message.
- Failed is produced only by the agent's verdict, meaning the orchestrator ran to
  completion and judged `fail`.
- Every infrastructure path in the table above produces Blocked with the right
  category. The rest of the run keeps executing (no halted chain) unless the
  Test Runner is gone, in which case the run is aborted as today.
- Blocked is shown on every surface that shows results. It never counts toward
  the pass rate, in either the numerator or the denominator.
- Blocked results push to TestRail as status 2 with the reason in the comment.
- "Re-run blocked" on a finished run.

## Non-goals
- Retrying or backing off on LLM 429/5xx responses (Q8). This plan classifies
  failures; it does not reduce them.
- Recovering from a Celery **hard** time-limit kill: the case stuck In progress
  and the run stuck Started. A separate fix plan is needed (for example a beat
  sweep or a `task_failure` signal handler); the soft limit is made to work
  properly here, so the hard limit becomes a last resort.
- Classifying controller `EXECUTION_FAILED` sub-cases such as `InputBlockedError`
  (UIPI) or `SCREENSHOT_FAILED` as Blocked. Telling them apart needs new
  `ErrorCode`s, which is a controller protocol change. They remain tool errors
  that the agent judges.
- Backfilling historical Failed rows (Q7).
- A new `TestRunStatus`. Run status stays waiting, started, done or cancelled;
  the Blocked look of a run is derived from its case counts.

## Design

### Data model
`projects/models.py`:
- `TestRunTestCaseStatus.BLOCKED = "blocked", "Blocked"`.
- New `BlockedReason(models.TextChoices)`:
  - `RUNNER_DISCONNECTED = "runner_disconnected", "Test Runner disconnected"`
  - `RUNNER_UNRESPONSIVE = "runner_unresponsive", "Test Runner stopped responding"`
  - `VISUAL_ENGINE = "visual_engine", "Visual engine unavailable"`
  - `AI_SERVICE = "ai_service", "AI service error"`
  - `TIME_LIMIT = "time_limit", "Case time limit reached"`
  - `INTERNAL_ERROR = "internal_error", "Internal error"`
- `TestRunTestCase.blocked_reason = CharField(max_length=32, choices=BlockedReason.choices, blank=True, default="")`.
  It is set only when status is `blocked`.
- The user-facing message reuses `TestRunTestCase.result`, as failures already do.
  No new message field.
- Migration `0018`:
  - `AlterField` on `status` changes choices only; Postgres needs no DDL.
  - `AddField` `blocked_reason` with a constant default; Postgres 11+ adds it
    without rewriting the table.
  - Additive only. Existing rows are untouched.

The reason is a separate column, not several `blocked_*` statuses. One status
keeps every count, filter and badge simple, and the reason stays a detail.

### Exceptions (domain errors)
`agents/exceptions.py`:
- `InfrastructureError(Exception)`: base for "the case cannot be executed or
  judged". It is never an application verdict. Plain class.
- `AIServiceError(InfrastructureError)` carries a `kind: AIServiceErrorKind`
  (StrEnum in the same module): `rate_limited` (429), `auth` (401/403),
  `unavailable` (5xx), `rejected` (other 4xx, for example context length),
  `timeout`, `connection`, `bad_response` (non-JSON or unparseable body,
  unusable plan or JSON).
- `OrchestratorPlanError` and `OrchestratorParseError` move here from
  `orchestrator.py` as `AIServiceError` subclasses with kind `bad_response`.
  `orchestrator.py` imports them.

`projects/exceptions.py`:
- `ControllerActionError` moves here from `projects/services.py`. `services.py`
  imports it, so `projects.services.ControllerActionError` still resolves for
  `agents/services/tools_controller.py` and the `test_controller_*` commands.
- `TestRunnerUnavailableError(ControllerActionError, InfrastructureError)` with
  two subclasses:
  - `TestRunnerDisconnectedError`: the connection was lost again on the re-send.
  - `TestRunnerUnresponsiveError`: a reply timed out while the runner still owned
    the connection, for runner-local actions only (see below).
- `VisualEngineError(ControllerActionError, InfrastructureError)`.
- `ControllerReconnectTimeoutError` also becomes an `InfrastructureError`
  subclass. It keeps its special handling, which aborts the run.

These subclass `ControllerActionError` so existing `except ControllerActionError`
sites (the management commands) keep working.

### Services
**Classification.** New `projects/case_blocking.py` (pure, no ORM).
- `classify_case_exception(exc: BaseException) -> BlockedReason` uses an ordered
  isinstance map:
  - `ControllerReconnectTimeoutError` and `TestRunnerDisconnectedError` →
    RUNNER_DISCONNECTED;
  - `TestRunnerUnresponsiveError` → RUNNER_UNRESPONSIVE;
  - `VisualEngineError` → VISUAL_ENGINE;
  - `AIServiceError` → AI_SERVICE;
  - `SoftTimeLimitExceeded` → TIME_LIMIT;
  - anything else → INTERNAL_ERROR.
- `blocked_message(exc, reason) -> str` builds safe text, for example
  "AI service rate limited the request (HTTP 429)". It contains no headers, no
  API key, no endpoint URL, and at most a short, truncated provider error body.

It is a separate module because both `execute_test_run_test_case` and the
TestRail comment need the labels. It must not import `projects.services`.

**AI service** (`agents/services/llm_client.py`). `send_chat_completion` wraps
`httpx.TimeoutException`, `httpx.TransportError`, `httpx.HTTPStatusError` (by
status) and JSON or `_parse_response` failures into `AIServiceError`. That is
the only exception type it raises for service faults.

**Agent loop and orchestrator.**
- `agent_loop._run_agent_loop` no longer catches exceptions around
  `send_chat_completion`, so `AIServiceError` propagates through
  `run_sub_agent` and `run_orchestrator`.
  `AgentStopReason.ERROR` then means only "agent verdict fail".
- `orchestrator._build_verdict` already knows pass or fail deterministically.
  If its summary LLM call raises `AIServiceError`, it falls back to a
  deterministic summary built from the sub-task results instead of blocking a
  case that was fully judged. This is the one place an AI error does not block.
- Planning and evaluate errors propagate and block the case.

**`safe_tool_call`** (`agents/services/tool_utils.py`) re-raises
`(AgentCancelledError, InfrastructureError, SoftTimeLimitExceeded)` and still
turns every other exception into a tool error. Tool failures that concern the
app itself (element not found, verification exhausted, command non-zero)
therefore still reach the agent.

**Graceful fallbacks stay graceful, but must not swallow the soft limit.**
- `context_summarizer._summarize_with_fallback` and
  `output_summarizer._summarize_with_fallback` keep their `except Exception`
  fallback to truncation and add `except SoftTimeLimitExceeded: raise` before it.
- `tools_search._attempt_summarization` adds `AIServiceError` to its caught
  tuple, because it would otherwise escape the old `httpx.HTTPError` catch and
  block a case over a search summary.

**Controller dispatch** (`projects/services.py`).
- `_raise_for_error_reply`: an `error.result` whose `code` is
  `FIND_ELEMENT_FAILED` or `OMNIPARSER_NOT_READY` raises `VisualEngineError`.
  `_raise_for_find_element_failure` (`success: false`) raises
  `VisualEngineError`.
- `_raise_for_omniparser_failure` raises `VisualEngineError`, and the timeout of
  `_wait_for_omniparser_ready` raises `VisualEngineError` instead of
  `TimeoutError`.
- `_run_with_reconnect_retry`: "lost again" raises `TestRunnerDisconnectedError`.
- Reply timeouts while the runner is still connected raise
  `TestRunnerUnresponsiveError` only for **runner-local actions**, whose
  duration does not depend on the application under test:
  - click, hover, drag, type_text, key_press;
  - screenshot, find_element;
  - cleanup_environment, check_app_installed.
  The timeout is passed per call site, for example as a keyword on
  `_dispatch_controller_action`. Timeouts of `run_command` (streaming),
  interactive commands, `launch_app` and all `browser_*` actions stay plain
  `ControllerActionError`, so the agent can judge a hung app or a slow page as a
  real failure (Q2).
- `_safe_cleanup` adds `SoftTimeLimitExceeded` to the exceptions it re-raises
  before a case.

**Outcome** (`projects/services.py:execute_test_run_test_case`). The same use
case gets new branches.
- `AgentCancelledError` and `ControllerNotConnectedError`: Cancelled, unchanged.
- `ControllerReconnectTimeoutError`:
  1. `_mark_pivot_blocked(pivot, RUNNER_DISCONNECTED, message)`;
  2. abort the run as today (`abort_test_run(..., revoke_task=False)`). It only
     touches pending and in-progress cases, so the remaining cases become
     Cancelled (Q6).
- `Exception`, which includes `SoftTimeLimitExceeded`, `AIServiceError`,
  `VisualEngineError`, `TestRunnerUnavailableError` and genuine bugs:
  - If the run was cancelled meanwhile, mark the case Cancelled. Otherwise mark
    it Blocked with `classify_case_exception`.
  - Log with `logger.exception` for INTERNAL_ERROR and `logger.warning` for the
    other categories.
  - **Do not re-raise.** The chain continues and the run reaches Done.
- `BaseException` (SystemExit and similar): same marking rule, then re-raise.
- `_finalize_pivot` is unchanged. It only runs after a complete orchestrator
  verdict.
- `_mark_pivot_blocked` sits next to `_mark_pivot_failed`. It sets status,
  `blocked_reason`, `result` and `finished_at`, appends
  `[Blocked: <label>] <message>` to the case log through the log callback, and
  broadcasts.

**Aggregation** (`projects/services.py`).
- `get_test_run_summary` gains `blocked`.
- `_annotate_run_counts` and `get_latest_runs_by_project` gain `blocked_count`:
  one more conditional `Count` in the same query, so no new queries.
- `list_test_run_cases` orders failed, then blocked, then the rest.
- `build_run_narrative` takes `blocked_titles` and adds
  "N blocked (not judged): titles."; `_build_narrative_for_active_run` counts
  blocked as finished. `list_blocked_case_titles` mirrors
  `list_failed_case_titles`.
- `_compute_pass_rate_30d` already excludes Blocked; leave it.
- `reset_test_run` also clears `blocked_reason`.
- `_update_test_run_status_if_needed` needs no change: `blocked` is not in its
  "still running" set.

**Re-run.**
- Generalise to `rerun_cases(test_run, scope: RerunScope)` with
  `RerunScope(StrEnum)`: `FAILED`, `BLOCKED`.
- `can_rerun_cases(test_run, count)` keeps today's rule: the run is DONE or
  CANCELLED and count > 0.
- The new run is named "… — failed rerun" or "… — blocked rerun".
- It copies `testrail_project_id`, `testrail_suite_id` and `testrail_run_id`, so
  pushing it adds the new verdicts to the same TestRail run (Q4). This also
  applies to Re-run failed.
- Move `@transaction.atomic` onto the creating function.
- `rerun_failed_cases` and `can_rerun_failed_cases` are replaced; update the
  callers in `projects/views.py`.

**TestRail** (`projects/testrail_mapping.py`).
- `TESTRAIL_STATUS_BLOCKED: Final = 2`, with BLOCKED → 2 in
  `_PIVOT_STATUS_TO_TESTRAIL` and label "Blocked".
- `_plan_pivot` passes the reason to the comment: the result text becomes
  `"<reason label>: <result>"` for blocked cases, built by a small pure helper in
  `testrail_mapping.py`.
- A re-push after a reset changes `status_id`, so `_is_changed` picks it up
  without other changes.

**Dashboard** (`dashboard/services.py`).
- `RunSummary` gains `blocked_7d`; the pass rate is unchanged.
- `_summaries_for_runs` and the activity annotation gain `blocked`.
- New attention kind `blocked_run`: DONE runs with blocked > 0 and failed == 0,
  so a run with failures is listed once, as `failed_run`. Label
  "Re-run blocked cases".
- `ActivityItem` gains `blocked: bool`.

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_rerun_blocked` `/projects/<id>/test-runs/<run_id>/rerun-blocked/` (new) | POST | `@project_membership_required`, `@require_POST` | none (no input) | redirect to the new run's detail | 404 for an unknown run; `ValueError` → `messages.error` + redirect back (mirrors `test_run_rerun_failed`) |
| `projects:test_run_rerun_failed` (existing) | POST | unchanged | none | unchanged; now copies the TestRail target | unchanged |
| `projects:test_run_detail` | GET | unchanged | — | `summary` gains `blocked`; new `blocked_titles`, `can_rerun_blocked`; `can_rerun_failed` is computed by `can_rerun_cases` | — |
| `projects:test_run_case_detail` | GET | unchanged | — | `pivot.blocked_reason` / `get_blocked_reason_display` (no extra query) | — |
| `projects:test_case_list` | GET | unchanged | — | "Blocked" appears in the status filter automatically, from the enum | — |
| `dashboard:index` | GET | unchanged | — | `summary.blocked_7d`, `blocked_run` attention items | — |

Template changes:
- `partials/badge_case_status.html`: a `blocked` branch.
- `partials/badge_run_status.html`: new optional `blocked_count`, with the order
  Failed, then Blocked, then Passed. A finished run with blocked cases never
  reads Passed.
- Pass `blocked_count` from `_test_run_table.html`, `_overview_runs.html`,
  `_overview_stats.html`, `_project_table.html` and `_recent_projects.html`.
  `_test_run_table.html` also adds "· N blocked".
- `test_run_detail.html`:
  - a fifth stat card, Blocked;
  - pass rate becomes `success / (success + failed)`, the same rule as overview
    and dashboard;
  - the JS badge and label maps gain `blocked`;
  - the "Failure reason" column becomes "Reason" and shows the reason label plus
    the truncated message for blocked cases;
  - a "Re-run blocked" form;
  - the `summary` init gains `blocked`;
  - the Abort dialog copy is fixed to "cancelled".
- `test_run_case_detail.html`:
  - badge maps;
  - a "Why this case was blocked" card: reason label, message, and a hint that
    the case was not judged and can be re-run;
  - a `card-tone-warning` tone.
- `_overview_alert.html`: a blocked alert when failed == 0 and blocked > 0,
  instead of "passed".
- `dashboard/includes/_run_summary.html`: a "Blocked · 7 days" stat
  (tone warning).
- `_attention.html`: the `blocked_run` kind.
- `_recent_activity.html`: a warning icon tone when blocked and not failed.
- `partials/stat_card.html`: add `warning` to the tone contract.
- `partials/README.md`: if it documents the badge contracts.

CSS (`static/css/punk-hazard.css`): `.badge-blocked` (warning palette),
`.stat-value.is-warning`, `.card-tone-warning` if missing, and
`.activity .activity-icon.is-warning`.

XSS: the reason label and `result` render with Django autoescaping only. No
`|safe`. Alpine uses `x-text`, never `x-html`.

### Background work and WebSockets
- No new Celery task or queue. `execute_test_run_case` keeps its limits.
- It now returns normally for Blocked, so the chain continues.
- `test_run.pivot_status` and `test_run.status` events: `summary` gains
  `blocked`.
- `test_run_case.status` event (`CaseStatusEvent`) gains `blocked_reason` and
  `blocked_reason_label` (empty unless blocked). The initial state sent by the
  case consumer (`fetch_test_case_state` path in `projects/consumers.py`) gains
  the same fields.
- These are additive; pages are server-rendered and reload on run completion.
- **Controller protocol: no change.** Classification uses the existing
  `ErrorCode` values (`FIND_ELEMENT_FAILED`, `OMNIPARSER_NOT_READY`,
  `CONTROLLER_DISCONNECTED`) and server-side timeouts. `CLIENT_VERSION`,
  `ClientCapability` and `REQUIRED_CLIENT_CAPABILITIES` stay untouched, and every
  deployed runner works as before.

### Settings / infrastructure
None. There are no new env vars, and `example.env` is unchanged.

### Data flow
1. A worker runs `execute_test_run_case(pivot_id)` → `execute_test_run_test_case`.
2. Infrastructure fault examples:
   - The LLM returns 503 → `send_chat_completion` raises
     `AIServiceError(kind=unavailable)` → it passes `_run_agent_loop` and the
     orchestrator → it reaches `execute_test_run_test_case`.
   - `find_element` replies `FIND_ELEMENT_FAILED` → `VisualEngineError` →
     `safe_tool_call` re-raises → same place.
3. `classify_case_exception` returns the reason → `_mark_pivot_blocked` saves
   status, reason and message, appends to the log, and broadcasts `pivot_status`
   (summary.blocked += 1) and the case `status`.
4. The task returns normally, `finally` runs cleanup and the run-status check,
   and the chain starts the next case.
5. When all cases are terminal the run becomes DONE. The badge reads Blocked if
   there are no failures and at least one blocked case. "Re-run blocked" creates
   a new run with the same TestRail target.
6. Push to TestRail: blocked cases go out as status 2 with
   "Blocked by Punk Hazard … <reason label>: <message>".

## Alternatives considered
- **Agent-reported Blocked**, where the sub-agent may answer `RESULT: BLOCKED`
  when a precondition cannot be met. It would need `SubTaskResult.status`
  `"blocked"`, an orchestrator short-circuit and a `PRECONDITION` reason. It is
  not in this draft: an LLM can retreat to BLOCKED to avoid a judgement, which
  would turn real failures into silent non-results, and "environment not
  reachable" is often exactly what QE wants reported. The code gives no current
  signal for it (Q1).
- **Classify by matching error text**, for example `result` containing
  "LLM request failed". It is fragile and untyped. Typed exceptions at the
  boundary where the fault is known are cheap and mypy-checked.
- **Several statuses**, such as `blocked_runner` and `blocked_ai`. They multiply
  every count, filter, badge and TestRail mapping for no gain. One status plus a
  reason column is enough.
- **Block on any tool error that looks infrastructural**, after the agent
  verdict. Rejected: it changes a judged result after the fact. Errors that are
  clearly infrastructure abort immediately; everything else stays the agent's
  call.
- **Retry blocked cases in place in the same run.** This keeps one run and one
  TestRail push, but it rewrites history (logs and screenshots of the blocked
  attempt) and needs a DONE → STARTED transition that `start_test_run` forbids.
  A new run mirrors "Re-run failed" and, by copying the TestRail target, still
  lands in the same TestRail run (Q4).
- **No `InfrastructureError` base**, listing the concrete types in
  `safe_tool_call`. Rejected: the list would have to change with every new fault
  type. The base is the one abstraction added, and it is justified because
  several catch sites must agree.

## Risks and mitigations
- **Over-blocking hides real failures.**
  - A hung app could surface as a runner timeout. Only runner-local actions map
    to `runner_unresponsive`; commands, launch and browser timeouts stay agent
    judgements.
  - A real application error could show up as an AI or visual failure. Blocked
    keeps the full log, screenshots and message, and "Re-run blocked" makes the
    second attempt one click.
- **Under-blocking.** Faults that still end as Failed: UIPI input blocking,
  screenshot capture failures, a sub-agent step budget running out. These are
  listed in Non-goals and Q2.
- **The chain no longer halts on errors.** An outage of the AI service or visual
  engine now runs every remaining case into Blocked quickly. An OmniParser stuck
  in `loading` costs up to `CONTROLLER_OMNIPARSER_READY_TIMEOUT` per case. This
  is acceptable: the user sees it live and can abort. If this proves noisy, a
  follow-up could abort the run after N consecutive blocks of the same reason.
- **Swallowed `SoftTimeLimitExceeded`.** Every broad `except Exception` in the
  agent path must re-raise it. The implementer should grep `except Exception` in
  `agents/` and `projects/services.py` and check each site.
- **Secrets in messages.**
  - `blocked_message` must not include request headers, the `OPENAI_API_KEY`,
    or the endpoint URL; httpx's `HTTPStatusError` string includes the URL.
  - Provider bodies are truncated to about 300 chars.
  - The message is shown in the UI and pushed to TestRail comments.
- **Migration.** It is additive. A rollback must first convert `blocked` rows
  back (`UPDATE … SET status='failed' WHERE status='blocked'`); otherwise old
  code renders an empty badge. Note this in the change note.
- **TestRail.** Some instances relabel or hide status 2, while system statuses
  1 to 5 always exist. Pushing Blocked to a team whose tooling also tickets
  Blocked would defeat the purpose (Q5).
- **Counts and N+1.** Each `blocked_count` is one more `Count(filter=…)` inside
  existing aggregate queries. The run table, overview, dashboard and project list
  keep the same query count. Check with `connection.queries` in a
  `manage.py shell` session.
- **Moving `ControllerActionError` and the orchestrator errors.** Re-importing
  them keeps the old import paths valid, and mypy catches any miss.

## Implementation steps
1. **Model and migration.** Add `BLOCKED`, `BlockedReason` and `blocked_reason`;
   `makemigrations` produces 0018. Review it as additive before migrating.
   (files: `projects/models.py`, `projects/migrations/0018_*.py`)
2. **Shared exceptions and classifier.** Written first, before any parallel work.
   (files: `agents/exceptions.py`, `projects/exceptions.py`,
   `projects/case_blocking.py`; the import edits for the moved classes in
   `agents/services/orchestrator.py` and `projects/services.py`)
3. **AI-side propagation.** The orchestrator part covers the verdict-summary
   fallback and the moved errors.
   (files: `agents/services/llm_client.py`, `agents/services/agent_loop.py`,
   `agents/services/orchestrator.py`, `agents/services/tool_utils.py`,
   `agents/services/context_summarizer.py`,
   `agents/services/output_summarizer.py`, `agents/services/tools_search.py`)
4. **Controller classification and execution outcome.** One sub-agent, because
   it is all one file.
   - Dispatch: error-code and timeout mapping, runner-local timeout keyword,
     OmniParser wait.
   - `_safe_cleanup`.
   - `execute_test_run_test_case` branches, `_mark_pivot_blocked`, the
     reconnect-timeout path.
   - `CaseStatusEvent` fields.
   - Summary and annotations, ordering, narrative, `list_blocked_case_titles`,
     `rerun_cases`, `reset_test_run`.
   (files: `projects/services.py`; `projects/consumers.py` for the case-status
   fields)
5. **Views and URL for re-run blocked.** Update the detail context
   (`blocked_titles`, `can_rerun_blocked`) and the existing rerun-failed view to
   the new service.
   (files: `projects/views.py`, `projects/urls.py`)
6. **TestRail mapping.** (files: `projects/testrail_mapping.py`,
   `projects/testrail_results_services.py`)
7. **Dashboard services.** (files: `dashboard/services.py`)
8. **Templates and CSS.** (files: `templates/partials/badge_case_status.html`,
   `templates/partials/badge_run_status.html`,
   `templates/partials/stat_card.html`, `templates/partials/README.md`,
   `templates/projects/test_run_detail.html`,
   `templates/projects/test_run_case_detail.html`,
   `templates/projects/includes/_test_run_table.html`, `_overview_runs.html`,
   `_overview_stats.html`, `_overview_alert.html`, `_project_table.html`,
   `templates/dashboard/includes/_run_summary.html`, `_recent_projects.html`,
   `_attention.html`, `_recent_activity.html`, `static/css/punk-hazard.css`)

Steps 3, 4 + 5, 6, 7 and 8 touch disjoint files and can run in parallel after
steps 1 and 2. The orchestrator then integrates them and runs
`pre-commit run --all-files`.

## Verification
Static checks:
- `mypy .`, `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg controller_client/`
  (it should be untouched), and `pre-commit run --all-files`.
- `python manage.py makemigrations --check` after 0018, then `migrate` on a dev DB.

Manual checks (dev stack, one connected Test Runner):
1. **Agent verdict still Failed.** Run a case whose expected result is wrong for
   the app. It ends Failed with no `blocked_reason`, and the Reason column shows
   the agent summary.
2. **AI service, auth.** Set an invalid `OPENAI_API_KEY` and restart the
   execution worker. Run 3 cases:
   - all 3 end Blocked with "AI service error";
   - the message mentions HTTP 401 and contains no key or URL;
   - the run reaches Done (this proves the chain no longer halts), and the badge
     reads Blocked.
3. **AI service, connection.** Point `OPENAI_BASE_URL` at an unroutable host.
   Expect Blocked with kind connection.
4. **Visual engine.** Make OmniParser fail to load (for example rename the
   weights dir on the runner, confirm with `python -m controller_client.diagnose`).
   The case ends Blocked "Visual engine unavailable" before the agent starts.
5. **Runner disconnected.** Set `CONTROLLER_RECONNECT_GRACE_SECONDS=20`, start a
   run and stop the runner mid-case. The current case ends Blocked "Test Runner
   disconnected", later cases end Cancelled and the run is Cancelled. Reconnect
   within 20s instead: the run continues as before.
6. **Time limit.** From `manage.py shell`, enqueue one pivot with
   `execute_test_run_case.apply_async(args=[pivot_id], soft_time_limit=60, time_limit=120)`
   on a long case. It ends Blocked "Case time limit reached" before the hard
   kill, and is not stuck In progress.
7. **User abort.** Abort a run mid-case. The current case is Cancelled, not
   Failed or Blocked, and the dialog text says cancelled.
8. **Surfaces.** Check each one:
   - run detail: Blocked stat; pass rate excludes blocked; Reason column; live
     badge update over WebSocket;
   - case detail: reason card;
   - Runs list "· N blocked";
   - project overview alert and stats;
   - dashboard: "Blocked · 7 days" stat, `blocked_run` attention item, project
     card badge;
   - Test cases filter "Blocked".
9. **Re-run blocked.** The button appears only on finished runs with blocked
   cases. The new run contains exactly those cases and carries the TestRail
   project, suite and run ids.
10. **TestRail push.** Push a run with Passed, Failed and Blocked cases. In
    TestRail the blocked tests show Blocked, and the comment has the reason
    label. Push the re-run: those tests now show the new result in the same
    TestRail run.
11. **Query counts.** Runs list, overview and dashboard make the same number of
    queries before and after the change (`django.db.connection.queries` in
    shell, `DEBUG=True`).

Then run the `reviewer` and `verifier` agents in parallel on the diff, and give
`verifier` the original request and this plan.

## Open questions
- **Q1. Agent-reported Blocked.** Should the agent itself be able to report
  Blocked, for example when a precondition cannot be met or the application URL
  is unreachable, or only infrastructure exceptions?
  (Assumption: infrastructure only. The agent's outcomes stay PASS or FAIL;
  agent-reported Blocked would be a follow-up with a `precondition` reason.)
- **Q2. Which timeouts are Blocked?**
  (Assumption, three parts:
  (a) reply timeouts block (`runner_unresponsive`) only for runner-local actions
  (click, hover, drag, type, key, screenshot, find_element, cleanup,
  check_app_installed). Command, launch and browser timeouts stay agent-visible,
  because a hung app or slow page is a real failure;
  (b) the Celery soft time limit means Blocked `time_limit`;
  (c) a sub-agent running out of its step budget stays Failed, as part of the
  agent's verdict.)
- **Q3. Unexpected server errors.** Should unexpected exceptions (bugs) also be
  Blocked (`internal_error`), with the run continuing to the next case?
  (Assumption: yes. A crash is never a verdict on the app, and halting the chain
  leaves the run stuck in Started.)
- **Q4. How to re-run blocked cases.** "Re-run blocked" creates a new run, like
  "Re-run failed", copying the TestRail project, suite and run id so the new
  verdicts land in the same TestRail run. Is that right, or should blocked cases
  be retried in place inside the same run? Is a per-case "Re-run" button needed?
  (Assumption: a new run with the TestRail target copied, applied to Re-run
  failed as well. No per-case button; single cases can be re-run from the Test
  cases page filtered by "Blocked" plus bulk create run.)
- **Q5. TestRail push.** Should Blocked be pushed to TestRail as status 2
  (Blocked), or left out of pushes entirely? Does your QE tooling create tickets
  for Blocked?
  (Assumption: push as Blocked (2), with the reason label in the comment.)
- **Q6. Stopped runs.** When the Test Runner is lost past the grace period, only
  the current case becomes Blocked; are the remaining not-started cases
  Cancelled or Blocked? And should user-initiated Abort and Force disconnect
  stay Cancelled?
  (Assumption: remaining cases Cancelled, which is not pushed to TestRail;
  Abort and Force disconnect stay Cancelled.)
- **Q7. Existing results.** Should existing Failed rows whose message clearly
  shows an infrastructure cause ("LLM request failed", "OmniParser failed to
  initialize", "Timed out waiting for controller reply") be backfilled to
  Blocked by a data migration?
  (Assumption: no backfill. History stays as recorded, and results already
  pushed to TestRail are unaffected.)
- **Q8. LLM retries.** Should this change also add a bounded retry with backoff
  for LLM 429, 5xx and timeouts (honouring `Retry-After`), to cut down how many
  cases get Blocked in the first place?
  (Assumption: out of scope; a separate plan.)

## Changelog
- 2026-10-22: created (Draft).
- 2026-10-22: user accepted the default answers for Q1–Q8; Ready, then In progress.
- 2026-10-22: review round 1 fixes: `httpx.RequestError` → `connection`; no provider body in auth-error messages; visual-engine Blocked message is fixed text (controller detail, incl. local paths, stays in the server log); reconnect-timeout path re-checks the case status before blocking; `_mark_pivot_blocked` saves logs in one UPDATE; narrative titles capped at 10 ("and N more") and only queried when the count is > 0; unused `blocked_titles` context key dropped. Not changed: the check-then-save window in `_settle_pivot_after_error` (an abort in that instant leaves Blocked instead of Cancelled; harmless and the verifier agreed no action needed).
- 2026-10-22: review round 2 clean; Done.
