"""Channel layer group names and event types of a Test Runner (a leaf).

- ``runner_{id}``: the runner's live controller socket (the owning consumer and,
  during a takeover, the superseded one). Every ``controller.*`` action, the
  person window nudge and the close events below go there.
- ``runner_status_{id}``: pages showing one runner (run card, setup chat header,
  runner detail); event ``runner.status``.
- ``runner_owner_{user_id}``: the owner's ``/runners/`` list; event
  ``runner.status``.
"""

from __future__ import annotations

from typing import Final

# Handled by ``ControllerConsumer.controller_force_disconnect``: closes the
# socket with ``CloseCode.FORCE_DISCONNECTED`` (4002). Sent by Force disconnect,
# token reissue and archive.
FORCE_DISCONNECT_EVENT: Final = "controller.force_disconnect"
# Sent to the replaced channel itself (not the group) when a newer socket of the
# same runner takes over: ``ControllerConsumer.controller_superseded`` closes it
# with ``CloseCode.SUPERSEDED`` (4001).
SUPERSEDED_EVENT: Final = "controller.superseded"
# ``RunnerStatusConsumer.runner_status``.
RUNNER_STATUS_EVENT: Final = "runner.status"


def runner_group(runner_id: int) -> str:
    """The runner's controller socket group."""
    return f"runner_{runner_id}"


def runner_status_group(runner_id: int) -> str:
    return f"runner_status_{runner_id}"


def runner_owner_group(user_id: int) -> str:
    return f"runner_owner_{user_id}"
