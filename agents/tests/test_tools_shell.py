from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.services.ssh_session import SSHSessionManager
from agents.services.tools_shell import execute_command
from environments.types import ContainerPorts, SSHResult


@pytest.fixture
def mock_ssh_session() -> MagicMock:
    """Fixture providing a mocked SSHSessionManager."""
    return MagicMock(spec=SSHSessionManager)


def test_execute_command_successful(mock_ssh_session: MagicMock) -> None:
    """Test execute_command with successful command execution."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=0,
        stdout="hello world",
        stderr="",
    )

    result = execute_command(mock_ssh_session, command="echo hello world")

    mock_ssh_session.execute.assert_called_once_with("echo hello world")
    assert result.is_error is False
    assert "hello world" in result.content
    assert "Exit code: 0" in result.content
    assert result.tool_call_id == ""
    assert result.image_base64 is None


def test_execute_command_failed(mock_ssh_session: MagicMock) -> None:
    """Test execute_command with failed command (exit_code != 0)."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=1,
        stdout="",
        stderr="command not found",
    )

    result = execute_command(mock_ssh_session, command="nonexistent_cmd")

    mock_ssh_session.execute.assert_called_once_with("nonexistent_cmd")
    assert result.is_error is True
    assert "STDERR: command not found" in result.content
    assert "Exit code: 1" in result.content


def test_execute_command_with_both_stdout_and_stderr(
    mock_ssh_session: MagicMock,
) -> None:
    """Test execute_command with both stdout and stderr output."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=0,
        stdout="processing...\ndone",
        stderr="warning: deprecation notice",
    )

    result = execute_command(mock_ssh_session, command="some_script.sh")

    assert result.is_error is False
    assert "processing...\ndone" in result.content
    assert "STDERR: warning: deprecation notice" in result.content
    assert "Exit code: 0" in result.content


def test_execute_command_ssh_error(mock_ssh_session: MagicMock) -> None:
    """Test execute_command when SSH session raises an exception."""
    mock_ssh_session.execute.side_effect = Exception("Connection refused")

    result = execute_command(mock_ssh_session, command="ls")

    assert result.is_error is True
    assert "SSH error:" in result.content
    assert "Connection refused" in result.content


def test_execute_command_with_only_stdout(mock_ssh_session: MagicMock) -> None:
    """Test execute_command with only stdout (no stderr)."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=0,
        stdout="file1.txt\nfile2.txt",
        stderr="",
    )

    result = execute_command(mock_ssh_session, command="ls")

    assert result.is_error is False
    assert "file1.txt\nfile2.txt" in result.content
    assert "STDERR:" not in result.content
    assert "Exit code: 0" in result.content


def test_execute_command_with_empty_output(mock_ssh_session: MagicMock) -> None:
    """Test execute_command with empty stdout and stderr."""
    mock_ssh_session.execute.return_value = SSHResult(
        exit_code=0,
        stdout="",
        stderr="",
    )

    result = execute_command(mock_ssh_session, command="true")

    assert result.is_error is False
    assert result.content == "Exit code: 0"
