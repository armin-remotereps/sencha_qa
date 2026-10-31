"""Use case: "Check machine", a zero-LLM check of stage-0 requirements.

Plan: docs/plan/feat-machine-setup-intake.md › Services › quick_check.py,
Pages › machine-check/. Application requirements go through the runner's
``check_app_installed``; path and artifact requirements through one
``inspect_paths`` call (ARTIFACT_SCAN_V1, at most INSPECT_PATHS_MAX paths),
or through the inventory when it is under ten minutes old. Bounded by
``STAGE0_QUICK_CHECK_BUDGET_SECONDS``. What the check cannot decide keeps its
stored status (trusted until changed, D5). Results are written in one
``bulk_update`` stamped with the runner's hostname; with a run, the run page
gets the ``stage0`` event.
"""

from __future__ import annotations

import logging
import ntpath
import posixpath
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.utils import timezone

from accounts.models import CustomUser
from auto_tester.celery import app as celery_app
from controller_client.protocol import (
    ARTIFACT_PATH_MAX_CHARS,
    INSPECT_PATHS_MAX,
    ClientCapability,
)
from machine_readiness.artifact_matching import (
    extract_versions,
    format_version,
    match_inventory,
    parse_expected_version,
    version_satisfies,
)
from machine_readiness.exceptions import QuickCheckRefusedError
from machine_readiness.keys import normalise_key
from machine_readiness.models import (
    LATEST_VERSION,
    ArtifactInventory,
    ArtifactOs,
    RequirementKind,
    RequirementStatus,
    RequirementStatusSource,
    Stage0Requirement,
)
from machine_readiness.profile_rules import artifact_os_for
from machine_readiness.profile_text import load_profile_snapshot
from machine_readiness.run_stage0_state import load_run_stage0_rows, runner_host
from machine_readiness.services.requirement_status import (
    RequirementStatusUpdate,
    record_statuses,
)
from machine_readiness.services.run_stage0 import get_run_stage0
from machine_readiness.task_names import QUICK_CHECK
from machine_readiness.types import (
    InventoryEntry,
    MatchStatus,
    ProfileData,
    ProfileItemData,
)
from machine_setup.turn_state import MACHINE_SETUP_RUNNING_MESSAGE, live_turn_exists
from projects.exceptions import (
    ControllerActionError,
    ControllerNotConnectedError,
    TestRunnerUnavailableError,
)
from projects.models import Project, TestRun, TestRunStatus
from projects.runner_update_state import (
    RUNNER_UPDATE_RUNNING_MESSAGE,
    runner_update_in_progress,
)
from projects.services import controller_check_app_installed, controller_inspect_paths

logger = logging.getLogger(__name__)

# An inventory younger than this stands in for inspect_paths.
INVENTORY_FRESH_FOR = timedelta(minutes=10)
# Never start a runner action with less time than this left in the budget.
_MIN_ACTION_SECONDS = 2.0
# Per action; the runner answers check_app_installed and inspect_paths fast.
_ACTION_TIMEOUT_SECONDS = 30.0
_GLOB_CHARS = frozenset("*?[")
_RUNNING_MESSAGE = (
    "A run is executing on this machine. Check the machine when it has finished."
)
_NOT_CONNECTED_MESSAGE = "The Test Runner is not connected."


@dataclass(frozen=True)
class QuickCheckOutcome:
    """What one check decided; the rest kept their stored status."""

    # Requirements looked at (not ignored; of the run when one was given).
    total: int
    present: int
    missing: int
    mismatch: int
    # Not decidable here (kind, old runner, glob without a fresh inventory,
    # runner error) or not reached within the budget.
    undecided: int
    # The budget ran out or the runner went away before every check ran.
    stopped_early: bool
    host: str

    @property
    def checked(self) -> int:
        return self.present + self.missing + self.mismatch

    def to_payload(self) -> dict[str, object]:
        return {
            "total": self.total,
            "checked": self.checked,
            "present": self.present,
            "missing": self.missing,
            "mismatch": self.mismatch,
            "undecided": self.undecided,
            "stopped_early": self.stopped_early,
            "host": self.host,
        }


@dataclass(frozen=True)
class _Target:
    key: str
    kind: str
    check_target: str


@dataclass(frozen=True)
class _Runner:
    host: str
    os_name: ArtifactOs | None
    scan_supported: bool


def ensure_quick_check_allowed(*, project_id: int) -> None:
    """Raise ``QuickCheckRefusedError`` when a check may not run now.

    Refused while a run is Started, a setup turn is live, the runner is
    updating, or no runner is connected. Four small queries.
    """
    now = timezone.now()
    if TestRun.objects.filter(
        project_id=project_id, status=TestRunStatus.STARTED
    ).exists():
        raise QuickCheckRefusedError(_RUNNING_MESSAGE)
    if live_turn_exists(project_id, now):
        raise QuickCheckRefusedError(MACHINE_SETUP_RUNNING_MESSAGE)
    if runner_update_in_progress(project_id, now):
        raise QuickCheckRefusedError(RUNNER_UPDATE_RUNNING_MESSAGE)
    if not Project.objects.filter(pk=project_id, agent_connected=True).exists():
        raise QuickCheckRefusedError(_NOT_CONNECTED_MESSAGE)


def request_quick_check(*, project_id: int, run_id: int | None, actor_id: int) -> None:
    """Queue a check (``assist`` queue) after the same gates as the check."""
    ensure_quick_check_allowed(project_id=project_id)
    celery_app.send_task(QUICK_CHECK, args=[project_id, run_id, actor_id])


def check_requirements(
    *, project_id: int, run_id: int | None, actor_id: int | None
) -> QuickCheckOutcome:
    """Check the run's (or the project's) requirements on the connected runner.

    No LLM. Raises ``QuickCheckRefusedError`` (see
    ``ensure_quick_check_allowed``). With ``run_id`` the run group gets the
    ``stage0`` event once the statuses are written.
    """
    ensure_quick_check_allowed(project_id=project_id)
    runner = _load_runner(project_id)
    deadline = time.monotonic() + float(settings.STAGE0_QUICK_CHECK_BUDGET_SECONDS)
    targets = _load_targets(project_id=project_id, run_id=run_id)
    decided: dict[str, RequirementStatusUpdate] = {}
    stopped_early = False

    path_checks = _plan_path_checks(project_id, targets, runner, decided)
    if path_checks and runner.scan_supported:
        stopped_early = not _run_inspect(project_id, path_checks, decided, deadline)
    if not stopped_early:
        stopped_early = not _run_app_checks(project_id, targets, decided, deadline)

    if decided:
        author = (
            CustomUser.objects.filter(pk=actor_id).first()
            if actor_id is not None
            else None
        )
        record_statuses(
            project_id=project_id,
            items=list(decided.values()),
            source=RequirementStatusSource.QUICK_CHECK,
            author=author,
            host=runner.host,
        )
    counts = {status: 0 for status in RequirementStatus.values}
    for update in decided.values():
        counts[update.status] += 1
    outcome = QuickCheckOutcome(
        total=len(targets),
        present=counts[RequirementStatus.PRESENT],
        missing=counts[RequirementStatus.MISSING],
        mismatch=counts[RequirementStatus.MISMATCH],
        undecided=len(targets) - len(decided),
        stopped_early=stopped_early,
        host=runner.host,
    )
    if run_id is not None:
        _broadcast(project_id, run_id, runner.host or None, outcome)
    return outcome


def _load_runner(project_id: int) -> _Runner:
    info, capabilities = (
        Project.objects.filter(pk=project_id)
        .values_list("agent_system_info", "agent_capabilities")
        .get()
    )
    os_value = info.get("os") if isinstance(info, dict) else None
    return _Runner(
        host=runner_host(info) or "",
        os_name=artifact_os_for(os_value) if isinstance(os_value, str) else None,
        scan_supported=ClientCapability.ARTIFACT_SCAN_V1.value
        in (capabilities if isinstance(capabilities, list) else []),
    )


def _load_targets(*, project_id: int, run_id: int | None) -> list[_Target]:
    if run_id is not None:
        rows = load_run_stage0_rows(project_id=project_id, run_id=run_id)
        return [
            _Target(key=row.key, kind=row.kind, check_target=row.check_target.strip())
            for row in rows.requirements
        ]
    return [
        _Target(key=key, kind=kind, check_target=target.strip())
        for key, kind, target in Stage0Requirement.objects.filter(
            project_id=project_id, ignored=False
        )
        .order_by("key")
        .values_list("key", "kind", "check_target")
    ]


# --- paths and artifacts ----------------------------------------------------


@dataclass(frozen=True)
class _PathCheck:
    key: str
    path: str
    # For an artifact with a fixed path: the expected version text.
    expected_version: str = ""


def _plan_path_checks(
    project_id: int,
    targets: Sequence[_Target],
    runner: _Runner,
    decided: dict[str, RequirementStatusUpdate],
) -> list[_PathCheck]:
    """Paths to stat; artifacts a fresh inventory decides go to ``decided``."""
    path_targets = [t for t in targets if t.kind == RequirementKind.PATH]
    artifact_targets = [t for t in targets if t.kind == RequirementKind.ARTIFACT]
    if not path_targets and not artifact_targets:
        return []
    profile = (
        load_profile_snapshot(project_id).data if artifact_targets else ProfileData()
    )
    base_dir = (
        profile.base_dir_for(runner.os_name).strip()
        if runner.os_name is not None
        else ""
    )
    checks: list[_PathCheck] = []
    for target in path_targets:
        path = _absolute(target.check_target, base_dir, runner.os_name)
        if path is not None:
            checks.append(_PathCheck(key=target.key, path=path))
    if artifact_targets:
        items = {item.key: item for item in profile.items}
        wanted = [
            (target, items[normalise_key(target.check_target)])
            for target in artifact_targets
            if normalise_key(target.check_target) in items
        ]
        inventory = _fresh_inventory(project_id, runner, base_dir)
        if inventory is not None:
            _decide_from_inventory(wanted, inventory, base_dir, decided)
        else:
            for target, item in wanted:
                pattern = item.path_pattern.strip()
                if not pattern or _GLOB_CHARS & set(pattern):
                    continue
                path = _absolute(pattern, base_dir, runner.os_name)
                if path is not None:
                    checks.append(
                        _PathCheck(
                            key=target.key,
                            path=path,
                            expected_version=item.expected_version.strip(),
                        )
                    )
    return checks[:INSPECT_PATHS_MAX]


def _absolute(path: str, base_dir: str, os_name: ArtifactOs | None) -> str | None:
    """``path`` as an absolute path on the runner, or None when unresolvable."""
    if not path or len(path) > ARTIFACT_PATH_MAX_CHARS or "\x00" in path:
        return None
    if _is_absolute(path):
        return path
    if not base_dir:
        return None
    if os_name == ArtifactOs.WINDOWS:
        return ntpath.join(base_dir, path.replace("/", "\\"))
    return posixpath.join(base_dir, path.replace("\\", "/"))


def _is_absolute(path: str) -> bool:
    if path.startswith(("/", "~/", "\\\\")):
        return True
    return len(path) >= 3 and path[0].isalpha() and path[1] == ":" and path[2] in "\\/"


@dataclass(frozen=True)
class _Inventory:
    entries: tuple[InventoryEntry, ...]
    truncated: bool


def _fresh_inventory(
    project_id: int, runner: _Runner, base_dir: str
) -> _Inventory | None:
    if runner.os_name is None or not base_dir:
        return None
    row = (
        ArtifactInventory.objects.filter(
            project_id=project_id,
            os_name=runner.os_name,
            base_dir=base_dir,
            host=runner.host,
            scanned_at__gte=timezone.now() - INVENTORY_FRESH_FOR,
            error="",
        )
        .values_list("entries", "truncated")
        .first()
    )
    if row is None:
        return None
    raw_entries, truncated = row
    entries = tuple(
        entry
        for entry in (
            InventoryEntry.from_json(raw)
            for raw in (raw_entries if isinstance(raw_entries, list) else [])
        )
        if entry is not None
    )
    return _Inventory(entries=entries, truncated=bool(truncated))


def _decide_from_inventory(
    wanted: Sequence[tuple[_Target, ProfileItemData]],
    inventory: _Inventory,
    base_dir: str,
    decided: dict[str, RequirementStatusUpdate],
) -> None:
    matches = match_inventory(
        [item for _, item in wanted],
        inventory.entries,
        base_dir=base_dir,
        truncated=inventory.truncated,
    )
    for (target, _item), match in zip(wanted, matches, strict=True):
        if match.status == MatchStatus.FOUND:
            version = (
                f" (version {format_version(match.found_version)})"
                if match.found_version is not None
                else ""
            )
            paths = ", ".join(match.paths[:3])
            decided[target.key] = RequirementStatusUpdate(
                key=target.key,
                status=RequirementStatus.PRESENT,
                detail=f"Found in the base directory: {paths}{version}.",
            )
        elif match.status == MatchStatus.MISSING:
            decided[target.key] = RequirementStatusUpdate(
                key=target.key,
                status=RequirementStatus.MISSING,
                detail="Not found in the base directory.",
            )
        elif match.status == MatchStatus.VERSION_MISMATCH:
            decided[target.key] = RequirementStatusUpdate(
                key=target.key,
                status=RequirementStatus.MISMATCH,
                detail=f"Expected {match.expected_version}. {match.detail}",
            )


def _run_inspect(
    project_id: int,
    checks: Sequence[_PathCheck],
    decided: dict[str, RequirementStatusUpdate],
    deadline: float,
) -> bool:
    """Stat ``checks`` in one call; False when the runner went away or no time."""
    remaining = deadline - time.monotonic()
    if remaining < _MIN_ACTION_SECONDS:
        return False
    try:
        infos = controller_inspect_paths(
            project_id,
            [check.path for check in checks],
            timeout=min(_ACTION_TIMEOUT_SECONDS, remaining),
        )
    except (ControllerNotConnectedError, TestRunnerUnavailableError):
        return False
    except (ControllerActionError, ValueError) as exc:
        logger.warning("Quick check inspect_paths failed for %d: %s", project_id, exc)
        return True
    for check, info in zip(checks, infos, strict=False):
        if not info.exists:
            decided[check.key] = RequirementStatusUpdate(
                key=check.key,
                status=RequirementStatus.MISSING,
                detail=f"{check.path} does not exist.",
            )
            continue
        decided[check.key] = _present_or_mismatch(check)
    return True


def _present_or_mismatch(check: _PathCheck) -> RequirementStatusUpdate:
    expected_text = check.expected_version
    expected = (
        parse_expected_version(expected_text)
        if expected_text and expected_text.lower() != LATEST_VERSION
        else None
    )
    name = ntpath.basename(check.path.replace("/", "\\"))
    candidates = extract_versions(name)
    if expected is not None and candidates:
        if not any(version_satisfies(found, expected) for found in candidates):
            found_text = ", ".join(format_version(v) for v in candidates)
            return RequirementStatusUpdate(
                key=check.key,
                status=RequirementStatus.MISMATCH,
                detail=f"{check.path} exists; expected {expected_text}, "
                f"found {found_text}.",
            )
    return RequirementStatusUpdate(
        key=check.key, status=RequirementStatus.PRESENT, detail=f"{check.path} exists."
    )


# --- applications -----------------------------------------------------------


def _run_app_checks(
    project_id: int,
    targets: Sequence[_Target],
    decided: dict[str, RequirementStatusUpdate],
    deadline: float,
) -> bool:
    """One check_app_installed per application; False when stopped early."""
    for target in targets:
        if target.kind != RequirementKind.APPLICATION or not target.check_target:
            continue
        remaining = deadline - time.monotonic()
        if remaining < _MIN_ACTION_SECONDS:
            return False
        try:
            result = controller_check_app_installed(
                project_id,
                target.check_target,
                timeout=min(_ACTION_TIMEOUT_SECONDS, remaining),
            )
        except (ControllerNotConnectedError, TestRunnerUnavailableError):
            return False
        except ControllerActionError as exc:
            logger.warning(
                "Quick check of %s failed for %d: %s", target.key, project_id, exc
            )
            continue
        message = result["message"]
        if not result["success"]:
            continue
        if message.startswith("NOT INSTALLED"):
            status = RequirementStatus.MISSING
        elif message.startswith("INSTALLED"):
            status = RequirementStatus.PRESENT
        else:
            continue
        decided[target.key] = RequirementStatusUpdate(
            key=target.key, status=status, detail=message
        )
    return True


def _broadcast(
    project_id: int, run_id: int, host: str | None, outcome: QuickCheckOutcome
) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return
    checklist = get_run_stage0(project_id=project_id, run_id=run_id, runner_host=host)
    async_to_sync(layer.group_send)(
        f"test_run_{run_id}",
        {
            "type": "test_run.stage0",
            "checklist": checklist.to_payload(),
            "outcome": outcome.to_payload(),
        },
    )
