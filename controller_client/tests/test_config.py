from __future__ import annotations

import pytest

from controller_client import config as config_module
from controller_client.config import DEFAULT_CLEANUP_DIR, load_config


def _fake_decouple(values: dict[str, str]) -> object:
    def _config(key: str, default: object = None) -> object:
        return values.get(key, default)

    return _config


def test_cleanup_dir_defaults_to_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "decouple_config", _fake_decouple({}))

    config = load_config([])

    assert config.cleanup_dir == DEFAULT_CLEANUP_DIR
    assert DEFAULT_CLEANUP_DIR == "~/Downloads"


def test_cleanup_dir_reads_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_module,
        "decouple_config",
        _fake_decouple({"CONTROLLER_CLEANUP_DIR": "D:/qa-downloads"}),
    )

    config = load_config([])

    assert config.cleanup_dir == "D:/qa-downloads"
