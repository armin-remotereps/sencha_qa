from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.services.playwright_session import PlaywrightSessionManager
from environments.types import ContainerPorts


@pytest.fixture
def test_ports() -> ContainerPorts:
    return ContainerPorts(ssh=2222, vnc=5900, playwright_cdp=9223)


def _mock_playwright_stack() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """Create mock objects for the full Playwright CDP stack.

    Returns (sync_playwright_fn, pw_instance, browser, page).
    """
    mock_page = MagicMock()
    mock_page.url = "about:blank"

    mock_context = MagicMock()
    mock_context.pages = [mock_page]

    mock_browser = MagicMock()
    mock_browser.is_connected.return_value = True
    mock_browser.contexts = [mock_context]

    mock_pw = MagicMock()
    mock_pw.chromium.connect_over_cdp.return_value = mock_browser

    mock_sync_pw = MagicMock()
    mock_sync_pw.return_value.start.return_value = mock_pw

    return mock_sync_pw, mock_pw, mock_browser, mock_page


# ============================================================================
# Construction
# ============================================================================


def test_init_does_not_connect(test_ports: ContainerPorts) -> None:
    """After construction, is_connected is False."""
    mgr = PlaywrightSessionManager(test_ports)
    assert not mgr.is_connected


# ============================================================================
# Context manager
# ============================================================================


@patch(
    "agents.services.playwright_session.get_playwright_cdp_url",
    return_value="http://localhost:9223",
)
@patch("agents.services.playwright_session.sync_playwright")
def test_context_manager_calls_close(
    mock_sync_pw: MagicMock,
    mock_cdp_url: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """__exit__ calls close(), which cleans up browser and playwright."""
    mock_sync_pw_fn, mock_pw, mock_browser, mock_page = _mock_playwright_stack()
    mock_sync_pw.return_value = mock_sync_pw_fn.return_value

    with PlaywrightSessionManager(test_ports) as mgr:
        mgr.connect()

    mock_browser.close.assert_called_once()
    mock_pw.stop.assert_called_once()


# ============================================================================
# get_page / lazy connection
# ============================================================================


@patch(
    "agents.services.playwright_session.get_playwright_cdp_url",
    return_value="http://localhost:9223",
)
@patch("agents.services.playwright_session.sync_playwright")
def test_get_page_connects_lazily(
    mock_sync_pw: MagicMock,
    mock_cdp_url: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """get_page() triggers a connection if not connected."""
    mock_sync_pw_fn, mock_pw, mock_browser, mock_page = _mock_playwright_stack()
    mock_sync_pw.return_value = mock_sync_pw_fn.return_value

    mgr = PlaywrightSessionManager(test_ports)
    assert not mgr.is_connected

    page = mgr.get_page()

    mock_pw.chromium.connect_over_cdp.assert_called_once_with("http://localhost:9223")
    assert page is mock_page
    mgr.close()


@patch(
    "agents.services.playwright_session.get_playwright_cdp_url",
    return_value="http://localhost:9223",
)
@patch("agents.services.playwright_session.sync_playwright")
def test_get_page_returns_cached_page(
    mock_sync_pw: MagicMock,
    mock_cdp_url: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """Second get_page() call returns same page without reconnecting."""
    mock_sync_pw_fn, mock_pw, mock_browser, mock_page = _mock_playwright_stack()
    mock_sync_pw.return_value = mock_sync_pw_fn.return_value

    mgr = PlaywrightSessionManager(test_ports)
    page1 = mgr.get_page()
    page2 = mgr.get_page()

    assert page1 is page2
    # connect_over_cdp called only once (no reconnection)
    mock_pw.chromium.connect_over_cdp.assert_called_once()
    mgr.close()


@patch(
    "agents.services.playwright_session.get_playwright_cdp_url",
    return_value="http://localhost:9223",
)
@patch("agents.services.playwright_session.sync_playwright")
def test_get_page_reconnects_on_dead_browser(
    mock_sync_pw: MagicMock,
    mock_cdp_url: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """If browser.is_connected() returns False, get_page() reconnects."""
    mock_sync_pw_fn, mock_pw, mock_browser, mock_page = _mock_playwright_stack()
    mock_sync_pw.return_value = mock_sync_pw_fn.return_value

    mgr = PlaywrightSessionManager(test_ports)
    page1 = mgr.get_page()

    # Simulate dead browser
    mock_browser.is_connected.return_value = False

    # Create a new set of mocks for reconnection
    mock_page_2 = MagicMock()
    mock_page_2.url = "about:blank"
    mock_context_2 = MagicMock()
    mock_context_2.pages = [mock_page_2]
    mock_browser_2 = MagicMock()
    mock_browser_2.is_connected.return_value = True
    mock_browser_2.contexts = [mock_context_2]
    mock_pw.chromium.connect_over_cdp.return_value = mock_browser_2

    page2 = mgr.get_page()

    assert page2 is mock_page_2
    assert mock_pw.chromium.connect_over_cdp.call_count == 2
    mgr.close()


# ============================================================================
# close
# ============================================================================


@patch(
    "agents.services.playwright_session.get_playwright_cdp_url",
    return_value="http://localhost:9223",
)
@patch("agents.services.playwright_session.sync_playwright")
def test_close_stops_playwright(
    mock_sync_pw: MagicMock,
    mock_cdp_url: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """close() calls browser.close() and playwright.stop()."""
    mock_sync_pw_fn, mock_pw, mock_browser, mock_page = _mock_playwright_stack()
    mock_sync_pw.return_value = mock_sync_pw_fn.return_value

    mgr = PlaywrightSessionManager(test_ports)
    mgr.connect()
    mgr.close()

    mock_browser.close.assert_called_once()
    mock_pw.stop.assert_called_once()


@patch(
    "agents.services.playwright_session.get_playwright_cdp_url",
    return_value="http://localhost:9223",
)
@patch("agents.services.playwright_session.sync_playwright")
def test_close_handles_errors_gracefully(
    mock_sync_pw: MagicMock,
    mock_cdp_url: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """close() doesn't raise even if browser.close() raises."""
    mock_sync_pw_fn, mock_pw, mock_browser, mock_page = _mock_playwright_stack()
    mock_sync_pw.return_value = mock_sync_pw_fn.return_value
    mock_browser.close.side_effect = RuntimeError("browser explosion")

    mgr = PlaywrightSessionManager(test_ports)
    mgr.connect()
    mgr.close()  # Should not raise

    mock_browser.close.assert_called_once()
    mock_pw.stop.assert_called_once()


# ============================================================================
# is_connected
# ============================================================================


@patch(
    "agents.services.playwright_session.get_playwright_cdp_url",
    return_value="http://localhost:9223",
)
@patch("agents.services.playwright_session.sync_playwright")
def test_is_connected_true_when_browser_connected(
    mock_sync_pw: MagicMock,
    mock_cdp_url: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """is_connected returns True when the browser is connected."""
    mock_sync_pw_fn, mock_pw, mock_browser, mock_page = _mock_playwright_stack()
    mock_sync_pw.return_value = mock_sync_pw_fn.return_value

    mgr = PlaywrightSessionManager(test_ports)
    mgr.connect()

    assert mgr.is_connected
    mgr.close()


# ============================================================================
# connect idempotency
# ============================================================================


@patch(
    "agents.services.playwright_session.get_playwright_cdp_url",
    return_value="http://localhost:9223",
)
@patch("agents.services.playwright_session.sync_playwright")
def test_connect_is_idempotent(
    mock_sync_pw: MagicMock,
    mock_cdp_url: MagicMock,
    test_ports: ContainerPorts,
) -> None:
    """Calling connect() when already connected is a no-op."""
    mock_sync_pw_fn, mock_pw, mock_browser, mock_page = _mock_playwright_stack()
    mock_sync_pw.return_value = mock_sync_pw_fn.return_value

    mgr = PlaywrightSessionManager(test_ports)
    mgr.connect()
    mgr.connect()

    # connect_over_cdp should only be called once
    mock_pw.chromium.connect_over_cdp.assert_called_once()
    mgr.close()
