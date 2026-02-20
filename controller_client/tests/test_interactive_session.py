import pytest

from controller_client.exceptions import ExecutionError
from controller_client.interactive_session import (
    InteractiveSession,
    InteractiveSessionManager,
)


class TestInteractiveSession:
    def test_start_and_read_output(self) -> None:
        session = InteractiveSession("echo hello", overall_timeout=30.0)
        output = session.start(read_timeout=5)
        assert "hello" in output
        assert session.is_alive() is False

    def test_send_input(self) -> None:
        session = InteractiveSession("cat", overall_timeout=30.0)
        session.start(read_timeout=2)
        assert session.is_alive() is True
        output = session.send_input("test line", read_timeout=3)
        assert "test line" in output
        session.terminate()

    def test_is_alive_after_exit(self) -> None:
        session = InteractiveSession("true", overall_timeout=30.0)
        session.start(read_timeout=3)
        assert session.is_alive() is False

    def test_exit_code_success(self) -> None:
        session = InteractiveSession("true", overall_timeout=30.0)
        session.start(read_timeout=3)
        assert session.exit_code() == 0

    def test_exit_code_failure(self) -> None:
        session = InteractiveSession("false", overall_timeout=30.0)
        session.start(read_timeout=3)
        assert session.exit_code() == 1

    def test_exit_code_none_before_start(self) -> None:
        session = InteractiveSession("echo hi", overall_timeout=30.0)
        assert session.exit_code() is None

    def test_terminate(self) -> None:
        session = InteractiveSession("sleep 60", overall_timeout=30.0)
        session.start(read_timeout=1)
        assert session.is_alive() is True
        session.terminate()
        assert session.is_alive() is False

    def test_session_id_is_set(self) -> None:
        session = InteractiveSession("true", overall_timeout=30.0)
        assert len(session.session_id) > 0

    def test_send_input_before_start_raises(self) -> None:
        session = InteractiveSession("cat", overall_timeout=30.0)
        with pytest.raises(ExecutionError, match="Session not started"):
            session.send_input("hello")

    def test_overall_timeout_terminates_session(self) -> None:
        session = InteractiveSession("cat", overall_timeout=0.0)
        session.start(read_timeout=1)
        with pytest.raises(ExecutionError, match="overall timeout"):
            session.send_input("hello")

    def test_elapsed_ms(self) -> None:
        session = InteractiveSession("true", overall_timeout=30.0)
        session.start(read_timeout=3)
        assert session.elapsed_ms() > 0


class TestInteractiveSessionManager:
    def test_start_and_get_session(self) -> None:
        manager = InteractiveSessionManager()
        session = manager.start_session("cat", timeout=30.0)
        session.start(read_timeout=1)
        retrieved = manager.get_session(session.session_id)
        assert retrieved.session_id == session.session_id
        manager.terminate_all()

    def test_get_session_unknown_id_raises(self) -> None:
        manager = InteractiveSessionManager()
        with pytest.raises(ExecutionError, match="No active session"):
            manager.get_session("nonexistent-id")

    def test_max_one_session_enforcement(self) -> None:
        manager = InteractiveSessionManager()
        first = manager.start_session("sleep 60", timeout=30.0)
        first.start(read_timeout=1)
        second = manager.start_session("cat", timeout=30.0)
        second.start(read_timeout=1)
        with pytest.raises(ExecutionError, match="No active session"):
            manager.get_session(first.session_id)
        retrieved = manager.get_session(second.session_id)
        assert retrieved.session_id == second.session_id
        manager.terminate_all()

    def test_terminate_session(self) -> None:
        manager = InteractiveSessionManager()
        session = manager.start_session("sleep 60", timeout=30.0)
        session.start(read_timeout=1)
        manager.terminate_session(session.session_id)
        with pytest.raises(ExecutionError, match="No active session"):
            manager.get_session(session.session_id)

    def test_terminate_all(self) -> None:
        manager = InteractiveSessionManager()
        session = manager.start_session("sleep 60", timeout=30.0)
        session.start(read_timeout=1)
        manager.terminate_all()
        with pytest.raises(ExecutionError, match="No active session"):
            manager.get_session(session.session_id)

    def test_terminate_all_when_empty(self) -> None:
        manager = InteractiveSessionManager()
        manager.terminate_all()
