from __future__ import annotations

import os
import platform
import re
import subprocess
import time

from controller_client.app_discovery import (
    MATCH_THRESHOLD,
    AppCandidate,
    compute_match_score,
    discover_apps,
    find_best_match,
)
from controller_client.process_tracker import ProcessTracker
from controller_client.protocol import ActionResultPayload, LaunchAppPayload

_MAX_SUGGESTIONS = 5
_FIELD_CODE_PATTERN = re.compile(r"\s*%[a-zA-Z]")


def execute_launch_app(
    payload: LaunchAppPayload, process_tracker: ProcessTracker
) -> ActionResultPayload:
    start = time.monotonic()
    query = payload.app_name.strip()

    if not query:
        elapsed = (time.monotonic() - start) * 1000
        return ActionResultPayload(
            success=False,
            message="app_name must not be empty.",
            duration_ms=elapsed,
        )

    candidates = discover_apps()
    best_match, best_score = find_best_match(query, candidates)

    if best_match is not None and best_score >= MATCH_THRESHOLD:
        message, success = _launch_app(best_match, process_tracker)
    else:
        message = _build_suggestion_message(query, candidates)
        success = False

    elapsed = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=success,
        message=message,
        duration_ms=elapsed,
    )


def _launch_app(
    candidate: AppCandidate, process_tracker: ProcessTracker
) -> tuple[str, bool]:
    system = platform.system()
    try:
        if system == "Darwin":
            proc = subprocess.Popen(
                ["open", "-a", candidate.exec_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            process_tracker.register(proc.pid)
        elif system == "Windows":
            os.startfile(candidate.exec_path)  # type: ignore[attr-defined]  # noqa: S606
        else:
            exec_cmd = _strip_field_codes(candidate.exec_path)
            proc = subprocess.Popen(
                exec_cmd,
                shell=True,  # noqa: S602
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            process_tracker.register(proc.pid)
    except OSError as exc:
        return f"Failed to launch '{candidate.display_name}': {exc}", False

    return f"Launched '{candidate.display_name}'.", True


def _build_suggestion_message(
    query: str,
    candidates: list[AppCandidate],
) -> str:
    scored = sorted(
        ((c, compute_match_score(query, c)) for c in candidates),
        key=lambda pair: pair[1],
        reverse=True,
    )
    top = [c.display_name for c, score in scored[:_MAX_SUGGESTIONS] if score > 0.0]

    if top:
        names = ", ".join(top)
        return f"No app matching '{query}' found. Similar apps: {names}"

    return f"No app matching '{query}' found and no suggestions available."


def _strip_field_codes(exec_cmd: str) -> str:
    return _FIELD_CODE_PATTERN.sub("", exec_cmd).strip()
