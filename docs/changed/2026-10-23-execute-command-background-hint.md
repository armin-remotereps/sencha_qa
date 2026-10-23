# Tell the agent execute_command runs in the background

Date: 2026-10-23
Plan: inline (trivial prompt change)

## What changed
- `agents/services/tool_definitions.py` (`execute_command` description): states the command runs in a hidden background shell, nothing appears on screen, and the output is in the tool result.
- `agents/services/prompt_parts.py` (`build_shell_rules`): new BACKGROUND SHELL rule telling the agent not to screenshot to watch a command or read its output; screenshot only for a GUI app the command launched.

## Why
After `execute_command` the agent expected something to happen on screen and took screenshots to check, wasting iterations.

## Behaviour and contract changes
- none (prompt text only)
