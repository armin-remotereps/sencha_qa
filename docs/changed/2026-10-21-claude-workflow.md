# Claude workflow aligned with sdr_agent_backend

Date: 2026-10-21

## What changed
- `.claude/agents/`: replaced the ten specialist agents with `planner`, `reviewer`, `verifier`.
- `.claude/rules/`: replaced the general, security and testing rules with `workflow.md`;
  their content moved into the Conventions section of `CLAUDE.md`.
- `.claude/skills/`: removed (`commit`, `implement-spec`, `migrate`, `worktree`, `merge-worktree`).
- `CLAUDE.md`: moved from `.claude/` to the repo root; added Session start, Workflow, Conventions.
- `docs/plan/` and `docs/changed/` added; finished TestRail plans 017/018 moved from the root to `docs/impl/`.
- Test suites removed (`accounts`, `agents`, `auto_tester`, `controller_client`, `dashboard`, `projects` `tests/`
  packages), with pytest, pytest-django and pytest-asyncio from both requirements files, the `[tool:pytest]`
  section in `controller_client/setup.cfg`, and the test-only `reset_omniparser_model()` helper.

## Why
Use the same plan -> implement -> review/verify -> document loop as sdr_agent_backend.

## Notes for future changes
- `docs/specs/` and `docs/impl/` are kept as history; new work uses `docs/plan/` and `docs/changed/`.
- The project has no tests; verify with mypy and manual smoke checks. The `test_*` management
  commands in `agents/` and `projects/` are manual dev tools, not tests, and stay.
