from __future__ import annotations

import pytest

from controller_client.exceptions import ExecutionError, InputBlockedError
from controller_client.input_guard import (
    INTEGRITY_HIGH,
    INTEGRITY_MEDIUM,
    INTEGRITY_SYSTEM,
    ForegroundWindow,
    InputProbe,
    describe_integrity_level,
    ensure_input_not_blocked,
    input_block_reason,
)


def _probe(
    own: int = INTEGRITY_MEDIUM,
    foreground: ForegroundWindow | None = None,
) -> InputProbe:
    return InputProbe(own_integrity_level=own, foreground=foreground)


def _window(level: int | None, title: str = "Task Manager") -> ForegroundWindow:
    return ForegroundWindow(title=title, pid=4242, integrity_level=level)


def test_describe_integrity_level_names_known_levels() -> None:
    assert describe_integrity_level(INTEGRITY_MEDIUM) == "medium"
    assert describe_integrity_level(INTEGRITY_HIGH) == "high (elevated)"


def test_describe_integrity_level_falls_back_to_hex() -> None:
    assert describe_integrity_level(0x2100) == "0x2100"


def test_no_foreground_window_is_not_blocked() -> None:
    assert input_block_reason(_probe(foreground=None)) is None


@pytest.mark.parametrize("level", [INTEGRITY_MEDIUM, 0x1000, 0x0])
def test_equal_or_lower_integrity_is_not_blocked(level: int) -> None:
    assert input_block_reason(_probe(foreground=_window(level))) is None


def test_higher_integrity_foreground_is_blocked_with_actionable_reason() -> None:
    reason = input_block_reason(_probe(foreground=_window(INTEGRITY_HIGH)))

    assert reason is not None
    assert "'Task Manager'" in reason
    assert "pid 4242" in reason
    assert "high (elevated)" in reason
    assert "controller client's medium" in reason
    assert "Run the controller client as Administrator" in reason


def test_uninspectable_foreground_process_is_blocked() -> None:
    reason = input_block_reason(_probe(foreground=_window(None, title="Setup")))

    assert reason is not None
    assert "'Setup'" in reason
    assert "not allowed to inspect" in reason


def test_elevated_controller_is_not_blocked_by_elevated_window() -> None:
    probe = _probe(own=INTEGRITY_HIGH, foreground=_window(INTEGRITY_HIGH))
    assert input_block_reason(probe) is None


def test_elevated_controller_is_still_blocked_by_system_window() -> None:
    probe = _probe(own=INTEGRITY_HIGH, foreground=_window(INTEGRITY_SYSTEM))
    assert input_block_reason(probe) is not None


def test_ensure_input_not_blocked_raises_execution_error_subclass() -> None:
    blocked = _probe(foreground=_window(INTEGRITY_HIGH))

    with pytest.raises(InputBlockedError) as excinfo:
        ensure_input_not_blocked(probe=lambda: blocked)

    assert isinstance(excinfo.value, ExecutionError)
    assert "Windows will discard synthesized input" in str(excinfo.value)


def test_ensure_input_not_blocked_passes_when_probe_unavailable() -> None:
    ensure_input_not_blocked(probe=lambda: None)


def test_ensure_input_not_blocked_passes_for_ordinary_window() -> None:
    ordinary = _probe(foreground=_window(INTEGRITY_MEDIUM, title="Notepad"))
    ensure_input_not_blocked(probe=lambda: ordinary)
