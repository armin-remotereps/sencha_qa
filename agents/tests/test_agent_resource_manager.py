from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services.agent_resource_manager import AgentResourceManager
from agents.services.playwright_session import PlaywrightSessionManager
from agents.services.ssh_session import SSHSessionManager
from environments.types import ContainerPorts


@pytest.fixture
def test_ports() -> ContainerPorts:
    return ContainerPorts(ssh=2222, vnc=5900, playwright_cdp=9223)


# ============================================================================
# Context manager
# ============================================================================


@patch("agents.services.agent_resource_manager.PlaywrightSessionManager")
@patch("agents.services.agent_resource_manager.SSHSessionManager")
def test_enter_enters_both_sessions(
    mock_ssh_cls: MagicMock,
    mock_pw_cls: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """__enter__ calls __enter__ on both ssh and playwright."""
    mock_ssh_inst = MagicMock(spec=SSHSessionManager)
    mock_pw_inst = MagicMock(spec=PlaywrightSessionManager)
    mock_ssh_cls.return_value = mock_ssh_inst
    mock_pw_cls.return_value = mock_pw_inst

    mgr = AgentResourceManager(test_ports)
    mgr.__enter__()

    mock_ssh_inst.__enter__.assert_called_once()
    mock_pw_inst.__enter__.assert_called_once()

    mgr.__exit__(None, None, None)


@patch("agents.services.agent_resource_manager.PlaywrightSessionManager")
@patch("agents.services.agent_resource_manager.SSHSessionManager")
def test_exit_closes_both_sessions(
    mock_ssh_cls: MagicMock,
    mock_pw_cls: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """__exit__ calls __exit__ on both ssh and playwright."""
    mock_ssh_inst = MagicMock(spec=SSHSessionManager)
    mock_pw_inst = MagicMock(spec=PlaywrightSessionManager)
    mock_ssh_cls.return_value = mock_ssh_inst
    mock_pw_cls.return_value = mock_pw_inst

    mgr = AgentResourceManager(test_ports)
    mgr.__enter__()
    mgr.__exit__(None, None, None)

    mock_pw_inst.__exit__.assert_called_once_with(None, None, None)
    mock_ssh_inst.__exit__.assert_called_once_with(None, None, None)


@patch("agents.services.agent_resource_manager.PlaywrightSessionManager")
@patch("agents.services.agent_resource_manager.SSHSessionManager")
def test_exit_closes_both_even_if_playwright_fails(
    mock_ssh_cls: MagicMock,
    mock_pw_cls: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """If playwright __exit__ raises, ssh still gets closed."""
    mock_ssh_inst = MagicMock(spec=SSHSessionManager)
    mock_pw_inst = MagicMock(spec=PlaywrightSessionManager)
    mock_pw_inst.__exit__.side_effect = RuntimeError("playwright boom")
    mock_ssh_cls.return_value = mock_ssh_inst
    mock_pw_cls.return_value = mock_pw_inst

    mgr = AgentResourceManager(test_ports)
    mgr.__enter__()
    mgr.__exit__(None, None, None)  # Should not raise

    mock_pw_inst.__exit__.assert_called_once()
    mock_ssh_inst.__exit__.assert_called_once_with(None, None, None)


@patch("agents.services.agent_resource_manager.PlaywrightSessionManager")
@patch("agents.services.agent_resource_manager.SSHSessionManager")
def test_exit_closes_both_even_if_ssh_fails(
    mock_ssh_cls: MagicMock,
    mock_pw_cls: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """If ssh __exit__ raises, no exception propagates."""
    mock_ssh_inst = MagicMock(spec=SSHSessionManager)
    mock_pw_inst = MagicMock(spec=PlaywrightSessionManager)
    mock_ssh_inst.__exit__.side_effect = RuntimeError("ssh boom")
    mock_ssh_cls.return_value = mock_ssh_inst
    mock_pw_cls.return_value = mock_pw_inst

    mgr = AgentResourceManager(test_ports)
    mgr.__enter__()
    mgr.__exit__(None, None, None)  # Should not raise

    mock_pw_inst.__exit__.assert_called_once_with(None, None, None)
    mock_ssh_inst.__exit__.assert_called_once()


# ============================================================================
# Properties
# ============================================================================


@patch("agents.services.agent_resource_manager.PlaywrightSessionManager")
@patch("agents.services.agent_resource_manager.SSHSessionManager")
def test_ssh_property_returns_ssh_manager(
    mock_ssh_cls: MagicMock,
    mock_pw_cls: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """ssh property returns SSHSessionManager instance."""
    mock_ssh_inst = MagicMock(spec=SSHSessionManager)
    mock_ssh_cls.return_value = mock_ssh_inst

    mgr = AgentResourceManager(test_ports)
    assert mgr.ssh is mock_ssh_inst


@patch("agents.services.agent_resource_manager.PlaywrightSessionManager")
@patch("agents.services.agent_resource_manager.SSHSessionManager")
def test_playwright_property_returns_playwright_manager(
    mock_ssh_cls: MagicMock,
    mock_pw_cls: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """playwright property returns PlaywrightSessionManager instance."""
    mock_pw_inst = MagicMock(spec=PlaywrightSessionManager)
    mock_pw_cls.return_value = mock_pw_inst

    mgr = AgentResourceManager(test_ports)
    assert mgr.playwright is mock_pw_inst
