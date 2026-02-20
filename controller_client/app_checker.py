from __future__ import annotations

import shutil
import time

from controller_client.app_discovery import (
    MATCH_THRESHOLD,
    AppCandidate,
    discover_apps,
    find_best_match,
)
from controller_client.protocol import ActionResultPayload, CheckAppInstalledPayload


def execute_check_app_installed(
    payload: CheckAppInstalledPayload,
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

    message = _determine_installation_status(query)
    elapsed = (time.monotonic() - start) * 1000
    return ActionResultPayload(success=True, message=message, duration_ms=elapsed)


def _determine_installation_status(query: str) -> str:
    cli_path = _check_cli(query)
    if cli_path is not None:
        return f"INSTALLED: '{query}' found at {cli_path} (CLI)"

    gui_match = _check_gui(query)
    if gui_match is not None:
        return f"INSTALLED: '{gui_match.display_name}' found (GUI app)"

    return f"NOT INSTALLED: '{query}' was not found as a CLI tool or GUI application."


def _check_cli(query: str) -> str | None:
    variants = _build_cli_variants(query)
    for variant in variants:
        path = shutil.which(variant)
        if path is not None:
            return path
    return None


def _build_cli_variants(query: str) -> list[str]:
    base = query.lower().replace(" ", "-")
    variants: list[str] = [query]
    if base != query:
        variants.append(base)
    no_separator = base.replace("-", "")
    if no_separator not in variants:
        variants.append(no_separator)
    return variants


def _check_gui(query: str) -> AppCandidate | None:
    candidates = discover_apps()
    best_match, best_score = find_best_match(query, candidates)
    if best_match is not None and best_score >= MATCH_THRESHOLD:
        return best_match
    return None
