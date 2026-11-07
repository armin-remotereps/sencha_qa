"""Use case: clear staging rows no claim needs any more (hourly beat).

Deletes the staged screenshots and step marks whose claim ended: released,
handed in, re-run, reset or the run stopped, so the case's
``manual_claim_token`` no longer matches the row's. Rows of a claim that is
still live are never deleted, however old (a tester may pause for days).
There is no TTL: an ended claim is the only reason to delete. Bulk deletes
only: two queries whatever the number of rows.
"""

from __future__ import annotations

from django.db.models import F, Q

from mcp_gateway.models import McpStagedShot, McpStepMark

# The row's claim is not the case's current claim (the token is never null).
_CLAIM_ENDED = ~Q(claim_token=F("pivot__manual_claim_token"))


def sweep_mcp_staging() -> int:
    """Delete staging rows of ended claims; the number of shots deleted."""
    shots, _ = McpStagedShot.objects.filter(_CLAIM_ENDED).delete()
    McpStepMark.objects.filter(_CLAIM_ENDED).delete()
    return shots
