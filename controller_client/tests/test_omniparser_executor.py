from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from controller_client.exceptions import ExecutionError
from controller_client.omniparser_executor import (
    _build_draw_config,
    _detect_device,
    _OmniParserModel,
    _RawElementDict,
    _to_pixel_element,
)


@pytest.fixture(autouse=True)
def _reset_omniparser_singleton() -> Iterator[None]:
    _OmniParserModel._instance = None
    yield
    _OmniParserModel._instance = None


def test_to_pixel_element_scales_ratio_bbox_to_pixels() -> None:
    raw: _RawElementDict = {
        "type": "icon",
        "content": "Save",
        "bbox": [0.1, 0.2, 0.3, 0.4],
        "interactivity": True,
    }
    element = _to_pixel_element(2, raw, width=1000, height=500)

    assert element.index == 2
    assert element.type == "icon"
    assert element.content == "Save"
    assert element.interactivity is True
    assert element.bbox.x_min == 100
    assert element.bbox.y_min == 100
    assert element.bbox.x_max == 300
    assert element.bbox.y_max == 200
    assert element.center_x == 200
    assert element.center_y == 150


def test_to_pixel_element_defaults_missing_fields() -> None:
    raw: _RawElementDict = {"bbox": [0.0, 0.0, 1.0, 1.0]}
    element = _to_pixel_element(0, raw, width=100, height=100)

    assert element.type == "unknown"
    assert element.content == ""
    assert element.interactivity is False


def test_build_draw_config_scales_with_image_size() -> None:
    small = _build_draw_config((800, 600))
    large = _build_draw_config((3200, 2400))

    assert large["thickness"] >= small["thickness"]
    assert large["text_scale"] >= small["text_scale"]


def test_detect_device_returns_a_known_device_string() -> None:
    assert _detect_device() in ("cuda", "mps", "cpu")


def test_load_raises_execution_error_when_weights_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "controller_client.omniparser_executor.omniparser_weights_dir",
        lambda: str(tmp_path / "does-not-exist"),
    )

    model = _OmniParserModel()

    with pytest.raises(ExecutionError, match="OmniParser weights not found"):
        model._load()

    assert model._parser is None
