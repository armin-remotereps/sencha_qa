"""Use case: "Check machine", a zero-LLM check of stage-0 requirements.

Plan: docs/plan/feat-machine-setup-intake.md › Services › quick_check.py,
Pages › machine-check/. Application requirements go through the runner's
``check_app_installed``; path and artifact requirements through one
``inspect_paths`` call (ARTIFACT_SCAN_V1, at most INSPECT_PATHS_MAX paths),
or through the inventory when it is under ten minutes old. Bounded by
``STAGE0_QUICK_CHECK_BUDGET_SECONDS``. What the check cannot decide keeps its
stored status (trusted until changed, D5). Results are written in one
``bulk_update`` stamped with the runner's hostname; with a run, the run page
gets the ``stage0`` event (also when the worker refuses the check, with
``refused``). Runner calls and gates come through ``RunnerPorts`` (plan D2);
the check runs on one Test Runner (the run's, or one picked), one check per
runner at a time (``project_locks.RUNNER_QUICK_CHECK_LOCK_PREFIX``).
A network (UNC) path is inspected only under the profile's network base
directory (``network_paths``); otherwise it stays unknown.
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
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from accounts.models import CustomUser
from controller_client.protocol import ARTIFACT_PATH_MAX_CHARS, INSPECT_PATHS_MAX
from machine_readiness import project_locks
from machine_readiness.artifact_matching import (
    extract_versions,
    format_version,
    match_for_os,
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
from machine_readiness.network_paths import (
    NETWORK_PATH_NOT_CHECKED,
    is_network_app_target,
    is_network_path,
    network_inspection_allowed,
)
from machine_readiness.profile_text import load_profile_snapshot
from machine_readiness.run_stage0_state import load_run_stage0_rows
from machine_readiness.runner_info import RunnerInfo, load_runner_info
from machine_readiness.runner_ports import ReadinessWork, RunnerPorts
from machine_readiness.services.requirement_status import (
    RequirementStatusUpdate,
    record_statuses,
)
from machine_readiness.services.run_stage0 import get_run_stage0
from machine_readiness.services.runner_gates import (
    NO_RUNNER_MESSAGE,
    runner_action_refusal,
)
from machine_readiness.types import (
    InventoryEntry,
    MatchStatus,
    ProfileItemData,
)
from projects.exceptions import (
    ControllerActionError,
    ControllerNotConnectedError,
    TestRunnerUnavailableError,
)
from runners.locks import lock_runner_row
from runners.types import RunnerId

logger = logging.getLogger(__name__)

# An inventory younger than this stands in for inspect_paths.
INVENTORY_FRESH_FOR = timedelta(minutes=10)
# Never start a runner action with less time than this left in the budget.
_MIN_ACTION_SECONDS = 2.0
# Per action; the runner answers check_app_installed and inspect_paths fast.
_ACTION_TIMEOUT_SECONDS = 30.0
_GLOB_CHARS = frozenset("*?[")
# The check task: the budget, then the writes and the broadcast.
QUICK_CHECK_TASK_SOFT_LIMIT_SECONDS = (
    int(settings.STAGE0_QUICK_CHECK_BUDGET_SECONDS) + 30
)
QUICK_CHECK_TASK_TIME_LIMIT_SECONDS = QUICK_CHECK_TASK_SOFT_LIMIT_SECONDS + 30
# The per-runner check lock outlives the task's hard limit only a little.
QUICK_CHECK_LOCK_TTL_SECONDS = QUICK_CHECK_TASK_TIME_LIMIT_SECONDS + 30
QUICK_CHECK_BUSY_MESSAGE = (
    "This machine is being checked already; the card updates when it ends."
)


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
        """The run socket's ``stage0`` outcome; it reaches every viewer of
        the run, so it never carries the machine's ``host``."""
        return {
            "total": self.total,
            "checked": self.checked,
            "present": self.present,
            "missing": self.missing,
            "mismatch": self.mismatch,
            "undecided": self.undecided,
            "stopped_early": self.stopped_early,
        }


@dataclass(frozen=True)
class _Target:
    key: str
    kind: str
    check_target: str
    # RequirementStatus value as stored.
    stored_status: str


def ensure_quick_check_allowed(
    *, project_id: int, runner_id: RunnerId | None, ports: RunnerPorts
) -> RunnerInfo:
    """The runner, or ``QuickCheckRefusedError`` when a check may not run now.

    Refused with no runner, while an agent-driven run is Started on it, a
    manual case is being tested on it, a setup turn drives it, it is
    updating, another project's run holds it, or it is not connected
    (``runner_gates``). A few small queries; call under the runner row lock.
    """
    runner = load_runner_info(runner_id)
    refusal = runner_action_refusal(
        project_id=project_id,
        runner_id=runner_id,
        ports=ports,
        connected=runner.connected,
        turn_id=None,
        work=ReadinessWork.CHECK,
    )
    if refusal is not None:
        raise QuickCheckRefusedError(refusal)
    return runner


def check_requirements(
    *,
    project_id: int,
    runner_id: RunnerId | None,
    run_id: int | None,
    actor_id: int | None,
    ports: RunnerPorts,
    lock_token: str,
) -> QuickCheckOutcome:
    """Check the run's (or the project's) requirements on ``runner_id``.

    No LLM. Runs under the runner's check lock handed over by
    ``request_quick_check`` (``lock_token``), which took it under the runner
    row lock after the gates; there is no other way in. Raises
    ``QuickCheckRefusedError`` (see ``ensure_quick_check_allowed``, or a
    check already running); with ``run_id`` the run group then gets a
    ``stage0`` event with ``refused``, so the card stops waiting. Otherwise
    the run group gets the ``stage0`` event once the statuses are written.
    """
    lock: project_locks.ProjectLock | None = None
    try:
        if runner_id is None:
            raise QuickCheckRefusedError(NO_RUNNER_MESSAGE)
        lock = project_locks.claim(
            project_locks.RUNNER_QUICK_CHECK_LOCK_PREFIX,
            runner_id,
            lock_token,
            ttl=QUICK_CHECK_LOCK_TTL_SECONDS,
        )
        if lock is None:
            raise QuickCheckRefusedError(QUICK_CHECK_BUSY_MESSAGE)
        return _check(
            project_id=project_id,
            runner_id=runner_id,
            run_id=run_id,
            actor_id=actor_id,
            ports=ports,
        )
    except QuickCheckRefusedError as exc:
        if run_id is not None:
            _broadcast_refused(run_id, exc.message)
        raise
    finally:
        if lock is not None:
            project_locks.release(lock)


def _check(
    *,
    project_id: int,
    runner_id: RunnerId,
    run_id: int | None,
    actor_id: int | None,
    ports: RunnerPorts,
) -> QuickCheckOutcome:
    # Re-checked under the runner row lock: a run may have started on it
    # since the check was queued.
    with transaction.atomic():
        lock_runner_row(runner_id)
        runner = ensure_quick_check_allowed(
            project_id=project_id, runner_id=runner_id, ports=ports
        )
    deadline = time.monotonic() + float(settings.STAGE0_QUICK_CHECK_BUDGET_SECONDS)
    targets = _load_targets(project_id=project_id, run_id=run_id)
    decided: dict[str, RequirementStatusUpdate] = {}
    stopped_early = False

    path_checks = _plan_path_checks(project_id, targets, runner, decided)
    if path_checks and runner.scan_supported:
        stopped_early = not _run_inspect(
            runner_id, ports, path_checks, decided, deadline
        )
    if not stopped_early:
        stopped_early = not _run_app_checks(
            runner_id, ports, targets, decided, deadline
        )

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
            host=runner.host or "",
        )
    counts = {status: 0 for status in RequirementStatus.values}
    for update in decided.values():
        counts[update.status] += 1
    checked = (
        counts[RequirementStatus.PRESENT]
        + counts[RequirementStatus.MISSING]
        + counts[RequirementStatus.MISMATCH]
    )
    outcome = QuickCheckOutcome(
        total=len(targets),
        present=counts[RequirementStatus.PRESENT],
        missing=counts[RequirementStatus.MISSING],
        mismatch=counts[RequirementStatus.MISMATCH],
        undecided=len(targets) - checked,
        stopped_early=stopped_early,
        host=runner.host or "",
    )
    if run_id is not None:
        _broadcast(project_id, run_id, runner.host, outcome)
    return outcome


def _load_targets(*, project_id: int, run_id: int | None) -> list[_Target]:
    if run_id is not None:
        rows = load_run_stage0_rows(project_id=project_id, run_id=run_id)
        return [
            _Target(
                key=row.key,
                kind=row.kind,
                check_target=row.check_target.strip(),
                stored_status=row.stored_status,
            )
            for row in rows.requirements
        ]
    return [
        _Target(key=key, kind=kind, check_target=target.strip(), stored_status=status)
        for key, kind, target, status in Stage0Requirement.objects.filter(
            project_id=project_id, ignored=False
        )
        .order_by("key")
        .values_list("key", "kind", "check_target", "status")
    ]


# --- paths and artifacts ----------------------------------------------------


@dataclass(frozen=True)
class _PathCheck:
    key: str
    path: str
    # For an artifact with a fixed path: the expected version text.
    expected_version: str = ""
    # A network path allowed by ``network_inspection_allowed``.
    network: bool = False


def _plan_path_checks(
    project_id: int,
    targets: Sequence[_Target],
    runner: RunnerInfo,
    decided: dict[str, RequirementStatusUpdate],
) -> list[_PathCheck]:
    """Paths to stat; artifacts a fresh inventory decides go to ``decided``,
    and so do network paths that may not be opened (unknown)."""
    path_targets = [t for t in targets if t.kind == RequirementKind.PATH]
    artifact_targets = [t for t in targets if t.kind == RequirementKind.ARTIFACT]
    if not path_targets and not artifact_targets:
        return []
    profile = load_profile_snapshot(project_id).data
    base_dir = (
        profile.base_dir_for(runner.os_name).strip()
        if runner.os_name is not None
        else ""
    )
    checks: list[_PathCheck] = []

    def add(target: _Target, path: str, expected_version: str = "") -> None:
        if not is_network_path(path):
            checks.append(
                _PathCheck(key=target.key, path=path, expected_version=expected_version)
            )
        elif runner.os_name is not None and network_inspection_allowed(
            path, base_dir, runner.os_name
        ):
            checks.append(
                _PathCheck(
                    key=target.key,
                    path=path,
                    expected_version=expected_version,
                    network=True,
                )
            )
        elif target.stored_status == RequirementStatus.UNKNOWN:
            # Never opened; a status recorded otherwise is kept (D5).
            decided[target.key] = RequirementStatusUpdate(
                key=target.key,
                status=RequirementStatus.UNKNOWN,
                detail=f"{NETWORK_PATH_NOT_CHECKED} Check {path} by hand.",
            )

    for target in path_targets:
        path = _absolute(target.check_target, base_dir, runner.os_name)
        if path is not None:
            add(target, path)
    if artifact_targets:
        items = {item.key: item for item in profile.items}
        wanted = [
            (target, items[normalise_key(target.check_target)])
            for target in artifact_targets
            if normalise_key(target.check_target) in items
        ]
        inventory = _fresh_inventory(project_id, runner, base_dir)
        if inventory is not None and runner.os_name is not None:
            _decide_from_inventory(wanted, inventory, runner.os_name, base_dir, decided)
        else:
            for target, item in wanted:
                pattern = item.path_pattern.strip()
                if not pattern or _GLOB_CHARS & set(pattern):
                    continue
                path = _absolute(pattern, base_dir, runner.os_name)
                if path is not None:
                    add(target, path, item.expected_version.strip())
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
    project_id: int, runner: RunnerInfo, base_dir: str
) -> _Inventory | None:
    if runner.os_name is None or not base_dir:
        return None
    row = (
        ArtifactInventory.objects.filter(
            # The runner's own scan, or one from before user runners of the
            # same host.
            Q(runner__isnull=True) | Q(runner_id=runner.runner_id),
            project_id=project_id,
            os_name=runner.os_name,
            base_dir=base_dir,
            host=runner.host or "",
            scanned_at__gte=timezone.now() - INVENTORY_FRESH_FOR,
            error="",
        )
        .order_by(F("runner_id").asc(nulls_last=True))
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
    os_name: ArtifactOs,
    base_dir: str,
    decided: dict[str, RequirementStatusUpdate],
) -> None:
    matches = match_for_os(
        [item for _, item in wanted],
        inventory.entries,
        os_name=os_name,
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
    runner_id: RunnerId,
    ports: RunnerPorts,
    checks: Sequence[_PathCheck],
    decided: dict[str, RequirementStatusUpdate],
    deadline: float,
) -> bool:
    """Stat ``checks`` in one call; False when the runner went away or no time."""
    remaining = deadline - time.monotonic()
    if remaining < _MIN_ACTION_SECONDS:
        return False
    try:
        infos = ports.inspect_paths(
            runner_id,
            [check.path for check in checks],
            allow_network=any(check.network for check in checks),
            timeout=min(_ACTION_TIMEOUT_SECONDS, remaining),
        )
    except (ControllerNotConnectedError, TestRunnerUnavailableError):
        return False
    except (ControllerActionError, ValueError) as exc:
        logger.warning(
            "Quick check inspect_paths failed for runner %d: %s", runner_id, exc
        )
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
    runner_id: RunnerId,
    ports: RunnerPorts,
    targets: Sequence[_Target],
    decided: dict[str, RequirementStatusUpdate],
    deadline: float,
) -> bool:
    """One check_app_installed per application; False when stopped early.

    A network check target is never sent (the runner would stat it); it is
    left unknown like a network path.
    """
    for target in targets:
        if target.kind != RequirementKind.APPLICATION or not target.check_target:
            continue
        if is_network_app_target(target.check_target):
            if target.stored_status == RequirementStatus.UNKNOWN:
                decided[target.key] = RequirementStatusUpdate(
                    key=target.key,
                    status=RequirementStatus.UNKNOWN,
                    detail=NETWORK_PATH_NOT_CHECKED,
                )
            continue
        remaining = deadline - time.monotonic()
        if remaining < _MIN_ACTION_SECONDS:
            return False
        try:
            result = ports.check_app_installed(
                runner_id,
                target.check_target,
                timeout=min(_ACTION_TIMEOUT_SECONDS, remaining),
            )
        except (ControllerNotConnectedError, TestRunnerUnavailableError):
            return False
        except ControllerActionError as exc:
            logger.warning(
                "Quick check of %s failed for runner %d: %s", target.key, runner_id, exc
            )
            continue
        message = result.message
        if not result.success:
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
            # Sent to every viewer of the run: no machine hostname.
            "checklist": checklist.to_payload(show_hosts=False),
            "outcome": outcome.to_payload(),
            "refused": "",
        },
    )


def _broadcast_refused(run_id: int, message: str) -> None:
    """The check did not run: the card shows why instead of waiting."""
    layer: Any = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(
        f"test_run_{run_id}",
        {
            "type": "test_run.stage0",
            "checklist": None,
            "outcome": None,
            "refused": message,
        },
    )
