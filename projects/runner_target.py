"""What a Test Runner drives: the desktop or a phone or tablet.

A leaf (docs/plan/feat-mobile-device-runner.md, D4 and D6): it imports only
models and the shared protocol. The controller consumer stores ``target`` and
the device keys in ``TestRunner.system_info`` only for a runner that
advertised MOBILE_DEVICE_V1, so ``parse_runner_target`` on the stored system
info is the target. ``claim_runner_connection`` sets the device state to
``starting`` for a phone runner and blank for any other, so a non-blank
``TestRunner.device_state`` marks a phone runner without reading the JSON.

The device rule (``device_offline``, ``device_machine_state``) and
``DEVICE_NOT_READY_MESSAGE`` moved to ``runners.machine_state``; they are
re-exported here for the gates that import them from this module.
"""

from __future__ import annotations

from controller_client.protocol import RunnerTarget, parse_runner_target
from runners.machine_state import DEVICE_NOT_READY_MESSAGE as DEVICE_NOT_READY_MESSAGE
from runners.machine_state import device_machine_state as device_machine_state
from runners.machine_state import device_offline as device_offline
from runners.models import TestRunner
from runners.types import RunnerId


def load_runner_target(runner_id: RunnerId) -> RunnerTarget:
    """The runner's target (one query); desktop when it is unknown."""
    system_info = (
        TestRunner.objects.filter(pk=runner_id)
        .values_list("system_info", flat=True)
        .first()
    )
    return _target_of(system_info)


def runner_target_of(runner: TestRunner | None) -> RunnerTarget:
    """The target from the loaded ``system_info`` (no query); desktop for None."""
    if runner is None:
        return RunnerTarget.DESKTOP
    return _target_of(runner.system_info)


def _target_of(system_info: object) -> RunnerTarget:
    if not isinstance(system_info, dict):
        return RunnerTarget.DESKTOP
    return parse_runner_target(system_info)
