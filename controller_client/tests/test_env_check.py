from __future__ import annotations

import builtins
from typing import Any

import pytest

from controller_client.env_check import verify_environment, verify_screen_capture
from controller_client.exceptions import EnvironmentCheckError


def test_verify_environment_passes_with_working_certifi() -> None:
    verify_environment()


def test_verify_environment_raises_when_certifi_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "certifi":
            raise ImportError("cannot import name 'where' from 'certifi'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(EnvironmentCheckError, match="certifi is broken"):
        verify_environment()


def test_verify_environment_raises_when_certifi_where_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import certifi

    def broken_where() -> str:
        raise AttributeError("module 'certifi' has no attribute 'where'")

    monkeypatch.setattr(certifi, "where", broken_where)

    with pytest.raises(EnvironmentCheckError, match="certifi is broken"):
        verify_environment()


def test_verify_screen_capture_passes_when_capture_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "controller_client.env_check.describe_screen_capture_failure", lambda: None
    )

    verify_screen_capture()


def test_verify_screen_capture_raises_with_the_underlying_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "controller_client.env_check.describe_screen_capture_failure",
        lambda: "could not create image from display. Grant Screen Recording",
    )

    with pytest.raises(EnvironmentCheckError, match="Screen Recording"):
        verify_screen_capture()
