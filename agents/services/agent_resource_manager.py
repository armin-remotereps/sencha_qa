from __future__ import annotations

import logging
from types import TracebackType

from agents.services.playwright_session import PlaywrightSessionManager
from agents.services.ssh_session import SSHSessionManager
from agents.services.vnc_session import VncSessionManager
from environments.types import ContainerPorts

logger = logging.getLogger(__name__)


class AgentResourceManager:
    """Unified lifecycle manager for SSH, Playwright, and VNC sessions.

    Usage::

        with AgentResourceManager(ports) as resources:
            result = resources.ssh.execute("echo hello")
            page = resources.playwright.get_page()
            screenshot = resources.vnc.capture_screen()
    """

    def __init__(self, ports: ContainerPorts) -> None:
        self._ssh = SSHSessionManager(ports)
        self._playwright = PlaywrightSessionManager(ports)
        self._vnc = VncSessionManager(ports)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> AgentResourceManager:
        self._ssh.__enter__()
        self._playwright.__enter__()
        self._vnc.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # Close all, logging but not raising errors
        # Order: Playwright -> VNC -> SSH
        try:
            self._playwright.__exit__(exc_type, exc_val, exc_tb)
        except Exception:
            logger.debug("Error closing Playwright session", exc_info=True)
        try:
            self._vnc.__exit__(exc_type, exc_val, exc_tb)
        except Exception:
            logger.debug("Error closing VNC session", exc_info=True)
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

    @property
    def vnc(self) -> VncSessionManager:
        return self._vnc
