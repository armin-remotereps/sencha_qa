from __future__ import annotations

import logging
from types import TracebackType

from agents.services.playwright_session import PlaywrightSessionManager
from agents.services.ssh_session import SSHSessionManager
from environments.types import ContainerPorts

logger = logging.getLogger(__name__)


class AgentResourceManager:
    """Unified lifecycle manager for SSH and Playwright sessions.

    Usage::

        with AgentResourceManager(ports) as resources:
            result = resources.ssh.execute("echo hello")
            page = resources.playwright.get_page()
    """

    def __init__(self, ports: ContainerPorts) -> None:
        self._ssh = SSHSessionManager(ports)
        self._playwright = PlaywrightSessionManager(ports)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> AgentResourceManager:
        self._ssh.__enter__()
        self._playwright.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # Close both, logging but not raising errors
        try:
            self._playwright.__exit__(exc_type, exc_val, exc_tb)
        except Exception:
            logger.debug("Error closing Playwright session", exc_info=True)
        try:
            self._ssh.__exit__(exc_type, exc_val, exc_tb)
        except Exception:
            logger.debug("Error closing SSH session", exc_info=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def ssh(self) -> SSHSessionManager:
        return self._ssh

    @property
    def playwright(self) -> PlaywrightSessionManager:
        return self._playwright
