from __future__ import annotations

import time
from unittest.mock import MagicMock, mock_open, patch

import pytest

from agents.services.vnc_session import VncSessionManager
from environments.types import ContainerPorts

TEST_VNC_PASSWORD = "test_vnc_password_123"


@pytest.fixture
def test_ports() -> ContainerPorts:
    return ContainerPorts(ssh=2222, vnc=5900, playwright_cdp=9223)


# ============================================================================
# Context manager
# ============================================================================


@patch("agents.services.vnc_session.api")
def test_enter_returns_self(mock_api: MagicMock, test_ports: ContainerPorts) -> None:
    mgr = VncSessionManager(test_ports)
    result = mgr.__enter__()
    assert result is mgr


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_exit_disconnects_client(
    mock_settings: MagicMock, mock_api: MagicMock, test_ports: ContainerPorts
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_client = MagicMock()
    mock_api.connect.return_value = mock_client

    mgr = VncSessionManager(test_ports)
    mgr.connect()
    mgr.__exit__(None, None, None)

    mock_client.disconnect.assert_called_once()
    mock_api.shutdown.assert_called_once()


@patch("agents.services.vnc_session.api")
def test_exit_without_connect_does_not_raise(
    mock_api: MagicMock, test_ports: ContainerPorts
) -> None:
    mgr = VncSessionManager(test_ports)
    mgr.__exit__(None, None, None)


# ============================================================================
# Connection
# ============================================================================


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_connect_calls_api_with_correct_server(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_api.connect.return_value = MagicMock()

    mgr = VncSessionManager(test_ports)
    mgr.connect()

    mock_api.connect.assert_called_once_with(
        "localhost::5900", password=TEST_VNC_PASSWORD, timeout=30
    )


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_connect_is_idempotent(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_api.connect.return_value = MagicMock()

    mgr = VncSessionManager(test_ports)
    mgr.connect()
    mgr.connect()

    mock_api.connect.assert_called_once()


@patch("agents.services.vnc_session.api")
def test_is_connected_false_initially(
    mock_api: MagicMock, test_ports: ContainerPorts
) -> None:
    mgr = VncSessionManager(test_ports)
    assert mgr.is_connected is False


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_is_connected_true_after_connect(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_api.connect.return_value = MagicMock()

    mgr = VncSessionManager(test_ports)
    mgr.connect()
    assert mgr.is_connected is True


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_close_sets_is_connected_false(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_api.connect.return_value = MagicMock()

    mgr = VncSessionManager(test_ports)
    mgr.connect()
    mgr.close()
    assert mgr.is_connected is False


# ============================================================================
# capture_screen
# ============================================================================


@patch("agents.services.vnc_session.os")
@patch("agents.services.vnc_session.tempfile")
@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_capture_screen_returns_png_bytes(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    mock_tempfile: MagicMock,
    mock_os: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_client = MagicMock()
    mock_api.connect.return_value = mock_client
    mock_tempfile.mkstemp.return_value = (5, "/tmp/test_screenshot.png")

    png_data = b"\x89PNG\r\n\x1a\n"

    with patch("builtins.open", mock_open(read_data=png_data)):
        mgr = VncSessionManager(test_ports)
        result = mgr.capture_screen()

    assert result == png_data
    mock_client.captureScreen.assert_called_once_with("/tmp/test_screenshot.png")
    mock_os.close.assert_called_once_with(5)
    mock_os.unlink.assert_called_once_with("/tmp/test_screenshot.png")


@patch("agents.services.vnc_session.os")
@patch("agents.services.vnc_session.tempfile")
@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_capture_screen_reconnects_on_failure(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    mock_tempfile: MagicMock,
    mock_os: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_client1 = MagicMock()
    mock_client2 = MagicMock()
    mock_api.connect.side_effect = [mock_client1, mock_client2]
    mock_tempfile.mkstemp.return_value = (5, "/tmp/test.png")

    png_data = b"\x89PNG"
    mock_client1.captureScreen.side_effect = RuntimeError("connection lost")

    with patch("builtins.open", mock_open(read_data=png_data)):
        mgr = VncSessionManager(test_ports)
        result = mgr.capture_screen()

    assert result == png_data
    assert mock_api.connect.call_count == 2
    mock_client2.captureScreen.assert_called_once()


# ============================================================================
# Mouse operations
# ============================================================================


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_mouse_move_calls_vnc_mouse_move(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_client = MagicMock()
    mock_api.connect.return_value = mock_client

    mgr = VncSessionManager(test_ports)
    mgr.mouse_move(100, 200)

    mock_client.mouseMove.assert_called_once_with(100, 200)


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_mouse_click_moves_then_presses(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_client = MagicMock()
    mock_api.connect.return_value = mock_client

    mgr = VncSessionManager(test_ports)
    mgr.mouse_click(300, 400, button=1)

    mock_client.mouseMove.assert_called_once_with(300, 400)
    mock_client.mousePress.assert_called_once_with(1)


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_mouse_click_reconnects_on_failure(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_client1 = MagicMock()
    mock_client2 = MagicMock()
    mock_api.connect.side_effect = [mock_client1, mock_client2]
    mock_client1.mouseMove.side_effect = RuntimeError("dead connection")

    mgr = VncSessionManager(test_ports)
    mgr.mouse_click(10, 20)

    assert mock_api.connect.call_count == 2
    mock_client2.mouseMove.assert_called_once_with(10, 20)
    mock_client2.mousePress.assert_called_once_with(1)


# ============================================================================
# Keyboard operations
# ============================================================================


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_type_text_calls_vnc_type(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_client = MagicMock()
    mock_api.connect.return_value = mock_client

    mgr = VncSessionManager(test_ports)
    mgr.type_text("hello world")

    mock_client.type.assert_called_once_with("hello world")


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_key_press_calls_vnc_key_press(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_client = MagicMock()
    mock_api.connect.return_value = mock_client

    mgr = VncSessionManager(test_ports)
    mgr.key_press("Return")

    mock_client.keyPress.assert_called_once_with("Return")


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_key_press_reconnects_on_failure(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_client1 = MagicMock()
    mock_client2 = MagicMock()
    mock_api.connect.side_effect = [mock_client1, mock_client2]
    mock_client1.keyPress.side_effect = RuntimeError("disconnected")

    mgr = VncSessionManager(test_ports)
    mgr.key_press("ctrl-a")

    assert mock_api.connect.call_count == 2
    mock_client2.keyPress.assert_called_once_with("ctrl-a")


# ============================================================================
# Close / shutdown behaviour
# ============================================================================


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_close_calls_api_shutdown(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_api.connect.return_value = MagicMock()

    mgr = VncSessionManager(test_ports)
    mgr.connect()
    mgr.close()

    mock_api.shutdown.assert_called_once()


@patch("agents.services.vnc_session._DISCONNECT_TIMEOUT_SECONDS", 1)
@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_close_handles_disconnect_timeout(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_client = MagicMock()
    mock_client.disconnect.side_effect = lambda: time.sleep(10)
    mock_api.connect.return_value = mock_client

    mgr = VncSessionManager(test_ports)
    mgr.connect()

    start = time.monotonic()
    mgr.close()
    elapsed = time.monotonic() - start

    assert elapsed < 5
    assert mgr.is_connected is False
    mock_api.shutdown.assert_called_once()


@patch("agents.services.vnc_session.api")
@patch("agents.services.vnc_session.settings")
def test_connect_passes_timeout(
    mock_settings: MagicMock,
    mock_api: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    mock_settings.ENV_VNC_PASSWORD = TEST_VNC_PASSWORD
    mock_api.connect.return_value = MagicMock()

    mgr = VncSessionManager(test_ports)
    mgr.connect()

    mock_api.connect.assert_called_once_with(
        "localhost::5900", password=TEST_VNC_PASSWORD, timeout=30
    )
