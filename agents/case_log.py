"""One line in the case log and the server log."""

from __future__ import annotations

import logging

from agents.types import LogCallback


def log_line(on_log: LogCallback | None, message: str, logger: logging.Logger) -> None:
    """Write ``message`` to ``logger`` (info) and to the case log, if any."""
    logger.info(message)
    if on_log is not None:
        on_log(message)
