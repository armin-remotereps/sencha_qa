# Change workflow

Every implementation, bug fix, refactor, or other change to code in this repository
follows this loop. Do not skip steps, and do not merge them.

## 1. Plan

Run the `planner` agent with the full request. It reads the code and docs,
researches library behaviour, and writes `docs/plan/<type>-<slug>.md`.

- Relay the plan's **Open questions** to the user and wait for answers before
  implementing anything the answers would change. Work that does not depend on
  them may start.
- Set the plan `Status` to `Ready` once questions are resolved, `In progress` when
  implementation starts.
- For a trivial change (a one-line fix with no design decisions) the planner
  returns an inline plan instead of a file; that is sufficient.

## 2. Implement

Split the work before writing code. If the plan touches independent parts that
live in different files (for example a Celery task, a view with its template, and
a WebSocket consumer), implement each part with its own sub-agent, launched in the
same message so they run in parallel:

- Give each sub-agent the plan path, the exact files and symbols it owns, the
  interfaces it must expose or consume (function signatures, form names, template
  context keys, WebSocket event names, controller action names), and the
  conventions below. Two sub-agents must never edit the same file.
- Shared code that every part depends on (a new model, a shared dataclass, a
  domain exception, a controller protocol type) is written first, before the
  sub-agents start, so they build on it instead of inventing it independently.
- When all sub-agents finish, integrate: read the combined diff, wire the parts
  together where the seams meet (imports, URL routing, consumer routing, task
  registration and queues, template includes), resolve naming or signature
  mismatches, and run `pre-commit run --all-files` on the whole tree. This final
  touch is done by the orchestrating agent, not delegated.

Work that lives in one file, or in files that depend on each other line by line,
is implemented directly without sub-agents.

Whether done directly or by sub-agents, follow the plan and the conventions in
`CLAUDE.md`:

- SOLID: one use case per service module, thin views/consumers/tasks, explicit
  forms and template context, domain exceptions, dependencies pointing inward.
- Resources: minimum queries, rows, and memory; no N+1; bulk operations for
  many rows; independent I/O run concurrently or moved to a Celery task;
  bounded pools and closed resources.
- Fast: measure the hot path in queries and round trips, not guesses.
- Fully typed; `pre-commit run --all-files` (isort, black, mypy for the server
  and `controller_client/`) clean before moving on.
- Change only what the plan calls for. Unrelated improvements go in a separate
  plan.

## 3. Review and verify (in parallel, read-only)

Launch both agents in the same message so they run concurrently:

- `reviewer`: SOLID, performance and resource usage, architecture rules.
- `verifier`: give it the original request (and plan path); it checks that the
  requested logic is fully and exactly implemented, that nothing unrelated
  changed, and that there are no security bugs.

Neither agent edits files. Collect both reports before acting.

## 4. Fix

Address every finding that is valid. For a finding you reject, note why in the
plan's Changelog; do not silently ignore it.

## 5. Loop

If step 4 changed any file, return to step 3 and run both agents again on the new
diff. Repeat until a pass produces no findings that require changes.

## 6. Document

When review and verification are clean, write `docs/changed/<YYYY-MM-DD>-<slug>.md`
and set the plan `Status` to `Done`. Keep the note as short as possible while
still telling a future agent what changed and why:

```markdown
# <Title>

Date: YYYY-MM-DD
Plan: docs/plan/<type>-<slug>.md

## What changed
- <module, page, task, or controller action>: <one line>

## Why
<one or two sentences>

## Behaviour and contract changes
- <new/changed page, URL, form field, WebSocket event, controller protocol,
  setting, migration; or "none">

## Notes for future changes
- <gotcha, follow-up, decision worth knowing>
```

Omit a section only if it would be empty. No code listings; name files and
symbols instead.
