# Manual runs performed through MCP (Claude Cowork, Codex)

Date: 2026-11-07
Plan: docs/plan/feat-controller-mcp.md

## What changed
- `mcp_gateway` (new app): always-on remote MCP server with 12 tools (read project/runs/cases/case, claim, add screenshot, mark step, hand in, release, KB list/search/read); thin adapters over the existing manual services; staging models `McpStagedShot`, `McpStepMark`; hourly sweep `mcp_gateway.tasks.sweep_mcp_staged_shots` (`notify`).
- `auto_tester/mcp_asgi.py`: uvicorn entrypoint (body cap 10 MiB, 401 before reading the body without a bearer, client IP for the auth throttle, Host/Origin check from `ALLOWED_HOSTS`). Compose service `mcp` (127.0.0.1:8001), nginx `location /mcp`, `scripts/verify_server_image.sh`, deploy skill.
- `projects`: internal `TestRunTestCase.manual_claim_source` / `ManualCaseSubmission.source` (migration 0059), `PersonStepActor.tester()` ("a tester"), public `check_manual_image`, `store_manual_screenshot(image_checked=)` returning the stored name, `release_manual_case` returns whether the claim was at the Test Runner.
- Manual runs no longer need a Test Runner: readiness (`manual_runner` and the machine-connected check), `start_test_run`, Resume/Re-run blockers and the run page's runner notice are gone for Manual (agent and HITL unchanged; phone projects too).
- Neutral wording for MCP-performed cases on the case card, run page, case page, release message, Resume dialog, TestRail shots note; runner window, Discuss, Pause/Continue ignore or refuse MCP claims; admin hides the marker. Manual run-mode descriptions no longer say a Test Runner window is required; `MANUAL_TESTER_ON_MACHINE` is neutral for everyone.

## Why
Let an external AI client act as the manual tester at the machine, with nothing in the app or TestRail showing that an AI did it.

## Behaviour and contract changes
- New endpoint `https://<host>/mcp` (MCP Streamable HTTP, stateless). Header `Authorization: Bearer <project Connection key>`. Claude custom connector: URL + that request header. Codex `config.toml`: `[mcp_servers.punk-hazard] url = "https://<host>/mcp"`, `bearer_token_env_var = "PUNK_HAZARD_CONNECTION_KEY"`. Regenerating the Connection key revokes runners and MCP clients at once.
- New deps `mcp~=2.3.0`, `uvicorn~=0.54.0`. New compose service `mcp`; nginx must be reloaded. No new env vars or settings.
- Migrations `projects/0059_manual_claim_source`, `mcp_gateway/0001_initial` (additive).
- MCP actions are labelled "a tester" (TestRail: "Passed manually by a tester in run …"); no member credit or Test Session row.
- Fail steps need 1..5 screenshots (same protocol rule as the runner).

## Notes for future changes
- Not yet run against a database or a real client: smoke-test with MCP Inspector, then Claude and Codex, on staging (see the plan's Verification).
- Never render `manual_claim_source` / `ManualCaseSubmission.source`; new manual wording must switch on `at_test_runner` / `manual_claim_at_runner`.
- MCP claim/release/hand-in share the runner's 20/min per-project answer budget by design.
- The hand-in writes up to 60 files under the project lock (≈1 s on local disk); move writes before the transaction if storage goes off-host.
