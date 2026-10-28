# Windows Test Runner: shell commands, launch_app, prompts

Date: 2026-10-28
Plan: docs/plan/fix-windows-runner-commands-and-apps.md

## What changed
- `controller_client/interactive_session.py`: picks a platform `ShellProcess`
  (typed by a `Protocol`). The pexpect/bash code moved to
  `interactive_process_posix.py`, unchanged except for `codec_errors="replace"`.
- `controller_client/interactive_process_windows.py` (new): runs System32
  `cmd.exe /d /s /c "<cmd>"` in a hidden console. A reader thread decodes the
  output as OEM with `replace`, the PID is registered, and terminate runs
  `taskkill /T`. It stops queueing output 5 s after cmd exits but keeps
  draining the pipe, so a `start /b` child does not block or grow memory.
- `controller_client/process_tracker.py`: uses `sys.platform` branches, keeps a
  win32-only `kill_process_tree`, and skips PIDs whose psutil create time
  changed, so it never kills a reused PID.
- `controller_client/app_discovery.py` / `app_launcher.py`: `AppLaunchKind`.
  - Get-StartApps AppIDs launch via `shell:AppsFolder\<AppID>`, with no PID.
  - `.exe` files launch via `Popen`, register their PID and fall back to
    `os.startfile` on error 740 (elevation).
  - Get-StartApps runs in a hidden console with UTF-8 output, read as
    `utf-8-sig`.
- `controller_client/shell_encoding.py` + `executor.py`: legacy `run_command`
  decodes with an explicit encoding and `errors="replace"`.
- `agents/services/shell_command.py` (`build_shell_command`): on Windows it
  builds `cd /d "<cwd>" && ...` and rejects `"`/line breaks in cwd and multi-line
  commands (`InvalidShellCommandError`, a tool error). POSIX is unchanged.
- `projects/runner_capability_services.py`: `runner_os_name` and
  `ensure_runner_supports_windows_shell`. `tools_controller.execute_command`
  calls them.
- `agents/services/prompt_parts.py`, `tool_definitions.py`: the shell rules and
  ENVIRONMENT name the shell (cmd.exe on Windows, bash elsewhere), with
  Windows-specific rules. The examples are OS-neutral.

## Why
On Windows every shell tool failed (`pexpect.spawn` and `/bin/bash` do not
exist there). `launch_app` passed AppIDs to `os.startfile` (WinError 2). The
prompts assumed bash.

## Behaviour and contract changes
- New optional capability `windows_shell_v1`; `CLIENT_VERSION` is now 0.6.2. A
  Windows runner without it pauses the case as Runner outdated on its first
  `execute_command`. macOS/Linux runners are not checked.
- New pre-commit hook `mypy-controller-client-win32` (`--platform win32`).

## Notes for future changes
- Guard Windows-only controller code with `sys.platform == "win32"`, not
  `platform.system()`, so the win32 mypy pass checks it. The pexpect stubs
  declare `spawn` on every platform.
- Windows commands have no PTY: input is not echoed, and console-only prompts
  (getpass) cannot be answered.
- Not yet run on a real Windows machine. The manual checks are listed in the
  plan's Verification section, including whether `os.startfile` accepts
  `shell:AppsFolder`; the fallback is `explorer.exe shell:AppsFolder\...`.
