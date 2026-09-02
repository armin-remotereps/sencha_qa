from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import cast

import pytest

from controller_client.browser_executor import BrowserSession
from controller_client.cleanup import (
    clear_directory,
    default_protected_paths,
    execute_cleanup,
)
from controller_client.interactive_session import InteractiveSessionManager
from controller_client.process_tracker import ProcessTracker


class FakeBrowserSession:
    def close(self) -> None:
        pass


class FakeSessionManager:
    def terminate_all(self) -> None:
        pass


class FakeProcessTracker:
    def kill_all(self) -> list[int]:
        return []


def _run_cleanup(cleanup_dir: Path) -> tuple[bool, str]:
    result = execute_cleanup(
        cast(BrowserSession, FakeBrowserSession()),
        cast(InteractiveSessionManager, FakeSessionManager()),
        cast(ProcessTracker, FakeProcessTracker()),
        cleanup_dir,
    )
    return result.success, result.message


def test_clear_directory_removes_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "installer.exe").write_bytes(b"x")
    (tmp_path / "unpacked" / "nested").mkdir(parents=True)
    (tmp_path / "unpacked" / "nested" / "file.txt").write_text("x")

    result = clear_directory(tmp_path, protected=())

    assert result.removed == 2
    assert result.skipped == ()
    assert result.failed == ()
    assert list(tmp_path.iterdir()) == []


def test_clear_directory_skips_entry_containing_protected_path(tmp_path: Path) -> None:
    install_dir = tmp_path / "controller_client"
    venv_marker = install_dir / ".venv" / "Lib" / "site-packages" / "annotated_types"
    venv_marker.mkdir(parents=True)
    (venv_marker / "__init__.py").write_text("x")
    (tmp_path / "other.zip").write_bytes(b"x")

    result = clear_directory(tmp_path, protected=(install_dir,))

    assert result.removed == 1
    assert result.skipped == ("controller_client",)
    assert (venv_marker / "__init__.py").is_file()
    assert not (tmp_path / "other.zip").exists()


def test_clear_directory_skips_entries_inside_protected_path(tmp_path: Path) -> None:
    (tmp_path / "client.py").write_text("x")
    (tmp_path / ".venv").mkdir()

    result = clear_directory(tmp_path, protected=(tmp_path,))

    assert result.removed == 0
    assert set(result.skipped) == {"client.py", ".venv"}
    assert (tmp_path / "client.py").is_file()


def test_clear_directory_reports_failures_instead_of_swallowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "locked").mkdir()
    (tmp_path / "plain.txt").write_text("x")

    def _rmtree_fails(path: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(f"[WinError 32] in use: {path}")

    monkeypatch.setattr(shutil, "rmtree", _rmtree_fails)

    result = clear_directory(tmp_path, protected=())

    assert result.removed == 1
    assert len(result.failed) == 1
    name, error = result.failed[0]
    assert name == "locked"
    assert "WinError 32" in error


def test_default_protected_paths_cover_install_dir_and_interpreter() -> None:
    protected = default_protected_paths()

    assert Path(__file__).resolve().parents[1] in protected
    assert Path(sys.prefix).resolve() in protected


def test_execute_cleanup_summarizes_removed_entries(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("x")

    success, message = _run_cleanup(tmp_path)

    assert success is True
    assert f"{tmp_path} cleared (2 removed)" in message


def test_execute_cleanup_fails_and_names_entries_it_could_not_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "locked").mkdir()

    def _rmtree_fails(path: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(shutil, "rmtree", _rmtree_fails)

    success, message = _run_cleanup(tmp_path)

    assert success is False
    assert "1 failed: locked (in use)" in message


def test_execute_cleanup_reports_missing_cleanup_dir(tmp_path: Path) -> None:
    success, message = _run_cleanup(tmp_path / "missing")

    assert success is True
    assert "not found, skipped" in message
