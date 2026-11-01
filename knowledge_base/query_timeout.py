"""A statement timeout around knowledge base searches.

Postgres cancels a search that runs past ``KNOWLEDGE_SEARCH_TIMEOUT_MS`` (so it
stops using the database, unlike a Celery time limit, which only abandons it)
and the connection stays usable. ``set_config(..., true)`` is transaction-local:
the block runs in ``atomic`` and puts the previous value back on success, so a
caller's outer transaction keeps its own timeout; on a failure the savepoint
or transaction rollback reverts it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from django.db import OperationalError, connection, transaction
from psycopg.errors import QueryCanceled

from knowledge_base.exceptions import KnowledgeSearchTimeoutError

_SET_TIMEOUT = "SELECT set_config('statement_timeout', %s, true)"


@contextmanager
def knowledge_search_timeout() -> Iterator[None]:
    """Run the block's queries under the search timeout; raises
    ``KnowledgeSearchTimeoutError`` when Postgres cancels one."""
    timeout_ms = settings.KNOWLEDGE_SEARCH_TIMEOUT_MS
    if timeout_ms <= 0:
        yield
        return
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('statement_timeout')")
                row = cursor.fetchone()
                previous = str(row[0]) if row is not None else "0"
                cursor.execute(_SET_TIMEOUT, [f"{timeout_ms}ms"])
            yield
            with connection.cursor() as cursor:
                cursor.execute(_SET_TIMEOUT, [previous])
    except OperationalError as exc:
        if isinstance(exc.__cause__, QueryCanceled):
            raise KnowledgeSearchTimeoutError(
                "The knowledge base search took too long; try fewer or more "
                "specific words."
            ) from exc
        raise
