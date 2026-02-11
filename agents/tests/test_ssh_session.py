from __future__ import annotations

from unittest.mock import MagicMock, patch

import paramiko
import pytest

from agents.services.ssh_session import _DEFAULT_COMMAND_PREFIX, SSHSessionManager
from environments.types import ContainerPorts, SSHResult


@pytest.fixture
def test_ports() -> ContainerPorts:
    return ContainerPorts(ssh=2222, vnc=5900, playwright_cdp=9223)


def _make_manager(
    ports: ContainerPorts,
    *,
    command_timeout: int = 120,
    keepalive_interval: int = 15,
) -> SSHSessionManager:
    return SSHSessionManager(
        ports,
        command_timeout=command_timeout,
        keepalive_interval=keepalive_interval,
    )


def _mock_ssh_client() -> MagicMock:
    """Create a mock SSH client with active transport."""
    mock_client = MagicMock()
    mock_transport = MagicMock()
    mock_transport.is_active.return_value = True
    mock_client.get_transport.return_value = mock_transport
    return mock_client


# ============================================================================
# Connection lifecycle
# ============================================================================


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_connect_creates_ssh_client(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """connect() creates and configures a paramiko SSHClient."""
    mock_client = _mock_ssh_client()
    mock_ssh_cls.return_value = mock_client

    mgr = _make_manager(test_ports)
    mgr.connect()

    mock_client.set_missing_host_key_policy.assert_called_once()
    mock_client.connect.assert_called_once()
    assert mgr.is_connected


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_connect_sets_keepalive(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """connect() enables keepalive on the transport."""
    mock_client = _mock_ssh_client()
    mock_transport = mock_client.get_transport.return_value
    mock_ssh_cls.return_value = mock_client

    mgr = _make_manager(test_ports, keepalive_interval=30)
    mgr.connect()

    mock_transport.set_keepalive.assert_called_once_with(30)


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_connect_idempotent(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """Calling connect() twice doesn't create a second connection."""
    mock_client = _mock_ssh_client()
    mock_ssh_cls.return_value = mock_client

    mgr = _make_manager(test_ports)
    mgr.connect()
    mgr.connect()

    # Only one SSHClient created
    assert mock_ssh_cls.call_count == 1


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_close_disconnects(mock_ssh_cls: MagicMock, test_ports: ContainerPorts) -> None:
    """close() calls close on the underlying client."""
    mock_client = _mock_ssh_client()
    mock_ssh_cls.return_value = mock_client

    mgr = _make_manager(test_ports)
    mgr.connect()
    mgr.close()

    mock_client.close.assert_called_once()
    assert not mgr.is_connected


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_close_without_connect_is_noop(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """close() without prior connect() is safe."""
    mgr = _make_manager(test_ports)
    mgr.close()  # Should not raise
    assert not mgr.is_connected


# ============================================================================
# Context manager
# ============================================================================


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_context_manager_closes_on_exit(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """Exiting the context manager closes the connection."""
    mock_client = _mock_ssh_client()
    mock_ssh_cls.return_value = mock_client

    with _make_manager(test_ports) as mgr:
        mgr.connect()
        assert mgr.is_connected

    mock_client.close.assert_called_once()


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_context_manager_closes_on_exception(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """Connection is closed even when an exception occurs inside the with block."""
    mock_client = _mock_ssh_client()
    mock_ssh_cls.return_value = mock_client

    with pytest.raises(RuntimeError):
        with _make_manager(test_ports) as mgr:
            mgr.connect()
            raise RuntimeError("boom")

    mock_client.close.assert_called_once()


# ============================================================================
# is_connected
# ============================================================================


def test_is_connected_initially_false(test_ports: ContainerPorts) -> None:
    """A new manager reports not connected."""
    mgr = _make_manager(test_ports)
    assert not mgr.is_connected


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_is_connected_false_when_transport_inactive(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """is_connected returns False when the transport is no longer active."""
    mock_client = _mock_ssh_client()
    mock_transport = mock_client.get_transport.return_value
    mock_ssh_cls.return_value = mock_client

    mgr = _make_manager(test_ports)
    mgr.connect()
    assert mgr.is_connected

    # Simulate transport dying
    mock_transport.is_active.return_value = False
    assert not mgr.is_connected


# ============================================================================
# execute
# ============================================================================


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_execute_lazy_connects(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """execute() connects lazily if not yet connected."""
    mock_client = _mock_ssh_client()
    mock_transport = mock_client.get_transport.return_value

    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_channel.recv.side_effect = [b"hello", b""]
    mock_channel.recv_stderr.side_effect = [b""]
    mock_transport.open_session.return_value = mock_channel
    mock_ssh_cls.return_value = mock_client

    mgr = _make_manager(test_ports)
    result = mgr.execute("echo hello")

    # connect was called (lazy)
    mock_client.connect.assert_called_once()
    assert result.exit_code == 0
    assert result.stdout == "hello"


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_execute_sets_channel_timeout(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """execute() sets the channel timeout from command_timeout."""
    mock_client = _mock_ssh_client()
    mock_transport = mock_client.get_transport.return_value

    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_channel.recv.side_effect = [b""]
    mock_channel.recv_stderr.side_effect = [b""]
    mock_transport.open_session.return_value = mock_channel
    mock_ssh_cls.return_value = mock_client

    mgr = _make_manager(test_ports, command_timeout=60)
    mgr.execute("ls")

    mock_channel.settimeout.assert_called_once_with(60.0)


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_execute_returns_stderr(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """execute() captures stderr output."""
    mock_client = _mock_ssh_client()
    mock_transport = mock_client.get_transport.return_value

    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 1
    mock_channel.recv.side_effect = [b""]
    mock_channel.recv_stderr.side_effect = [b"error message", b""]
    mock_transport.open_session.return_value = mock_channel
    mock_ssh_cls.return_value = mock_client

    mgr = _make_manager(test_ports)
    result = mgr.execute("bad_cmd")

    assert result.exit_code == 1
    assert result.stderr == "error message"


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_execute_reconnects_on_transport_failure(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """execute() reconnects once when the transport fails mid-command."""
    # First client: transport active but open_session fails
    mock_client_1 = _mock_ssh_client()
    mock_transport_1 = mock_client_1.get_transport.return_value
    mock_transport_1.open_session.side_effect = paramiko.SSHException("broken")

    # Second client: works fine
    mock_client_2 = _mock_ssh_client()
    mock_transport_2 = mock_client_2.get_transport.return_value

    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_channel.recv.side_effect = [b"ok", b""]
    mock_channel.recv_stderr.side_effect = [b""]
    mock_transport_2.open_session.return_value = mock_channel

    mock_ssh_cls.side_effect = [mock_client_1, mock_client_2]

    mgr = _make_manager(test_ports)
    result = mgr.execute("echo ok")

    # Two clients should have been created (initial + reconnect)
    assert mock_ssh_cls.call_count == 2
    assert result.exit_code == 0
    assert result.stdout == "ok"


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_execute_propagates_error_after_reconnect_fails(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """execute() raises after both the initial attempt and reconnect fail."""
    mock_client = _mock_ssh_client()
    mock_transport = mock_client.get_transport.return_value
    mock_transport.open_session.side_effect = paramiko.SSHException("broken")
    mock_ssh_cls.return_value = mock_client

    mgr = _make_manager(test_ports)

    with pytest.raises(paramiko.SSHException, match="broken"):
        mgr.execute("echo fail")


# ============================================================================
# Display environment prefix
# ============================================================================


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_run_command_prepends_display_env(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """_run_command wraps every command with DISPLAY and DBUS env setup."""
    mock_client = _mock_ssh_client()
    mock_transport = mock_client.get_transport.return_value

    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_channel.recv.side_effect = [b""]
    mock_channel.recv_stderr.side_effect = [b""]
    mock_transport.open_session.return_value = mock_channel
    mock_ssh_cls.return_value = mock_client

    mgr = _make_manager(test_ports)
    mgr.execute("echo hello")

    actual_cmd = mock_channel.exec_command.call_args[0][0]
    assert actual_cmd.startswith(_DEFAULT_COMMAND_PREFIX)
    assert actual_cmd == f"{_DEFAULT_COMMAND_PREFIX}echo hello"


@patch("agents.services.ssh_session.paramiko.SSHClient")
def test_run_command_uses_custom_prefix(
    mock_ssh_cls: MagicMock, test_ports: ContainerPorts
) -> None:
    """A custom command_prefix replaces the default DISPLAY env setup."""
    mock_client = _mock_ssh_client()
    mock_transport = mock_client.get_transport.return_value

    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_channel.recv.side_effect = [b""]
    mock_channel.recv_stderr.side_effect = [b""]
    mock_transport.open_session.return_value = mock_channel
    mock_ssh_cls.return_value = mock_client

    custom_prefix = "export DISPLAY=:1; "
    mgr = SSHSessionManager(
        test_ports,
        command_timeout=120,
        keepalive_interval=15,
        command_prefix=custom_prefix,
    )
    mgr.execute("echo hello")

    actual_cmd = mock_channel.exec_command.call_args[0][0]
    assert actual_cmd == f"{custom_prefix}echo hello"
