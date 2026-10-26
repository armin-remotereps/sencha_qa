"""Which finished cases get the teardown that closes the apps they left open.

Pure: no ORM. Only a Failed case qualifies. A passing case closes its apps
itself, and an aborted one was stopped on purpose. A fault never blocks a case
any more: it pauses the case for help, and a Need help case gets no teardown
(and no cleanup), so the machine is kept as it is for the person or auto-fix
agent who fixes it. Blocked is set only by a person resolving a help request,
outside the case worker, so it never reaches the teardown either.
"""

from __future__ import annotations

from projects.models import TestRunTestCaseStatus


def needs_teardown(status: str) -> bool:
    return status == TestRunTestCaseStatus.FAILED
