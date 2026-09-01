from __future__ import annotations

import base64
import io
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from controller_client.exceptions import ExecutionError, OmniParserError
from controller_client.omniparser_executor import (
    FIND_PHASE_SCREENSHOT,
    FIND_PHASE_SERIALIZE,
    LOAD_PHASE_IMPORTS,
    LOAD_PHASE_MODEL_LOAD,
    LOAD_PHASE_NOT_STARTED,
    LOAD_PHASE_WEIGHTS,
    MAX_RESULT_BYTES_ENV_VAR,
    _build_draw_config,
    _RawElementDict,
    _to_pixel_element,
    bound_find_element_result,
    detect_device,
    execute_find_element,
    get_omniparser_readiness,
    load_omniparser_model,
    measure_result_message_bytes,
    reset_omniparser_model,
)
from controller_client.protocol import (
    ErrorCode,
    FindElementPayload,
    FindElementResultPayload,
    OmniParserState,
    PixelBBoxPayload,
    PixelElementPayload,
    ScreenshotResponsePayload,
)

EXECUTOR = "controller_client.omniparser_executor"


class FakeOmniparser:
    constructed = 0
    construct_delay = 0.0

    def __init__(self, config: dict[str, object]) -> None:
        time.sleep(type(self).construct_delay)
        type(self).constructed += 1
        self.config = config
        self.som_model = object()
        self.caption_model_processor = object()


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    reset_omniparser_model()
    FakeOmniparser.constructed = 0
    FakeOmniparser.construct_delay = 0.0
    yield
    reset_omniparser_model()


@pytest.fixture
def weights_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "icon_detect").mkdir()
    (tmp_path / "icon_detect" / "model.pt").write_bytes(b"weights")
    (tmp_path / "icon_caption_florence").mkdir()
    monkeypatch.setattr(f"{EXECUTOR}.omniparser_weights_dir", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_model(
    weights_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> type[FakeOmniparser]:
    monkeypatch.setattr(f"{EXECUTOR}._import_omniparser_class", lambda: FakeOmniparser)
    monkeypatch.setattr(f"{EXECUTOR}.detect_device", lambda: "cpu")
    return FakeOmniparser


def _png_base64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _noisy_png_base64(width: int, height: int) -> str:
    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    return _png_base64(image)


def _decoded_size(image_base64: str) -> tuple[int, int]:
    return Image.open(io.BytesIO(base64.b64decode(image_base64))).size


def _element() -> PixelElementPayload:
    return PixelElementPayload(
        index=0,
        type="icon",
        content="Save",
        bbox=PixelBBoxPayload(x_min=10, y_min=20, x_max=30, y_max=40),
        center_x=20,
        center_y=30,
        interactivity=True,
    )


def _screenshot(width: int = 64, height: int = 48) -> ScreenshotResponsePayload:
    return ScreenshotResponsePayload(
        success=True,
        image_base64=_png_base64(Image.new("RGB", (width, height), "white")),
        width=width,
        height=height,
        format="png",
    )


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
    assert detect_device() in ("cuda", "mps", "cpu")


def test_initial_readiness_is_loading_not_started() -> None:
    readiness = get_omniparser_readiness()

    assert readiness.state is OmniParserState.LOADING
    assert readiness.phase == LOAD_PHASE_NOT_STARTED
    assert readiness.to_status_payload().state is OmniParserState.LOADING


def test_load_success_records_ready_state(
    fake_model: type[FakeOmniparser], weights_dir: Path
) -> None:
    result = load_omniparser_model()

    assert result.device == "cpu"
    assert result.weights_dir == str(weights_dir)
    assert result.load_seconds >= 0
    readiness = get_omniparser_readiness()
    assert readiness.state is OmniParserState.READY
    assert readiness.device == "cpu"
    assert readiness.phase == LOAD_PHASE_MODEL_LOAD
    assert readiness.load_seconds == result.load_seconds
    assert fake_model.constructed == 1
    assert fake_model.construct_delay == 0.0


def test_load_fails_in_weights_phase_when_weights_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(f"{EXECUTOR}.omniparser_weights_dir", lambda: str(missing))

    with pytest.raises(OmniParserError, match="OmniParser weights not found") as info:
        load_omniparser_model()

    assert info.value.phase == LOAD_PHASE_WEIGHTS
    assert info.value.weights_dir == str(missing)
    assert info.value.code == ErrorCode.OMNIPARSER_NOT_READY.value
    assert str(missing) in str(info.value)
    readiness = get_omniparser_readiness()
    assert readiness.state is OmniParserState.FAILED
    assert readiness.phase == LOAD_PHASE_WEIGHTS
    assert str(missing) in readiness.message


def test_load_fails_in_imports_phase_when_dependency_missing(
    weights_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_import() -> Any:
        raise ImportError("No module named 'ultralytics'", name="ultralytics")

    monkeypatch.setattr(f"{EXECUTOR}._import_omniparser_class", broken_import)

    with pytest.raises(OmniParserError, match="ultralytics") as info:
        load_omniparser_model()

    assert info.value.phase == LOAD_PHASE_IMPORTS
    assert "setup" in str(info.value)
    assert get_omniparser_readiness().state is OmniParserState.FAILED


def test_load_fails_in_model_load_phase_when_constructor_raises(
    weights_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingOmniparser:
        def __init__(self, config: dict[str, object]) -> None:
            raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(
        f"{EXECUTOR}._import_omniparser_class", lambda: ExplodingOmniparser
    )
    monkeypatch.setattr(f"{EXECUTOR}.detect_device", lambda: "cpu")

    with pytest.raises(OmniParserError, match="CUDA out of memory") as info:
        load_omniparser_model()

    assert info.value.phase == LOAD_PHASE_MODEL_LOAD
    assert info.value.device == "cpu"
    assert (
        info.value.details()
        == f"phase=model_load; device=cpu; weights_dir={weights_dir}"
    )


def test_second_load_does_not_construct_again(fake_model: type[FakeOmniparser]) -> None:
    first = load_omniparser_model()
    second = load_omniparser_model()

    assert first == second
    assert fake_model.constructed == 1


def test_concurrent_loads_construct_exactly_once(
    fake_model: type[FakeOmniparser],
) -> None:
    fake_model.construct_delay = 0.05
    results: list[object] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(load_omniparser_model())
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 8
    assert len(set(results)) == 1
    assert fake_model.constructed == 1


def test_load_retries_after_failure(
    weights_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"count": 0}

    def flaky_import() -> Any:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ImportError("No module named 'transformers'", name="transformers")
        return FakeOmniparser

    monkeypatch.setattr(f"{EXECUTOR}._import_omniparser_class", flaky_import)
    monkeypatch.setattr(f"{EXECUTOR}.detect_device", lambda: "cpu")

    with pytest.raises(OmniParserError):
        load_omniparser_model()
    assert get_omniparser_readiness().state is OmniParserState.FAILED

    result = load_omniparser_model()

    assert result.device == "cpu"
    assert get_omniparser_readiness().state is OmniParserState.READY
    assert attempts["count"] == 2


def test_find_element_when_failed_raises_not_ready_without_screenshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        f"{EXECUTOR}.omniparser_weights_dir", lambda: str(tmp_path / "missing")
    )
    screenshots = {"count": 0}

    def counting_screenshot() -> ScreenshotResponsePayload:
        screenshots["count"] += 1
        return _screenshot()

    monkeypatch.setattr(f"{EXECUTOR}.execute_screenshot", counting_screenshot)
    with pytest.raises(OmniParserError):
        load_omniparser_model()

    with pytest.raises(OmniParserError, match="restart the controller") as info:
        execute_find_element(FindElementPayload(None, None))

    assert info.value.code == ErrorCode.OMNIPARSER_NOT_READY.value
    assert info.value.phase == LOAD_PHASE_WEIGHTS
    assert screenshots["count"] == 0


def test_find_element_screenshot_failure_has_screenshot_phase(
    fake_model: type[FakeOmniparser], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_screenshot() -> ScreenshotResponsePayload:
        raise ExecutionError("Screenshot failed: no display")

    monkeypatch.setattr(f"{EXECUTOR}.execute_screenshot", failing_screenshot)

    with pytest.raises(OmniParserError, match="no display") as info:
        execute_find_element(FindElementPayload(None, None))

    assert info.value.phase == FIND_PHASE_SCREENSHOT
    assert info.value.code == ErrorCode.FIND_ELEMENT_FAILED.value
    assert info.value.device == "cpu"
    assert get_omniparser_readiness().state is OmniParserState.READY


def test_find_element_loads_lazily_and_returns_bounded_result(
    fake_model: type[FakeOmniparser], monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_elements: list[_RawElementDict] = [
        {
            "type": "icon",
            "content": "Save",
            "bbox": [0.0, 0.0, 0.5, 0.5],
            "interactivity": True,
        }
    ]
    monkeypatch.setattr(f"{EXECUTOR}.execute_screenshot", lambda: _screenshot(200, 100))
    monkeypatch.setattr(f"{EXECUTOR}._run_ocr", lambda image: ([], []))
    monkeypatch.setattr(
        f"{EXECUTOR}._run_som_labeling",
        lambda **kwargs: (
            _png_base64(Image.new("RGB", (200, 100), "red")),
            raw_elements,
        ),
    )

    result = execute_find_element(FindElementPayload(None, None))

    assert fake_model.constructed == 1
    assert result.image_width == 200
    assert result.image_height == 100
    assert result.elements[0].bbox.x_max == 100
    assert result.elements[0].center_y == 25
    assert get_omniparser_readiness().state is OmniParserState.READY


def test_find_element_result_too_large_has_serialize_phase(
    fake_model: type[FakeOmniparser], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{EXECUTOR}.execute_screenshot", lambda: _screenshot())
    monkeypatch.setattr(f"{EXECUTOR}._run_ocr", lambda image: ([], []))
    monkeypatch.setattr(
        f"{EXECUTOR}._run_som_labeling",
        lambda **kwargs: (_noisy_png_base64(1280, 40), []),
    )
    monkeypatch.setattr(f"{EXECUTOR}.omniparser_max_result_bytes", lambda: 1000)

    with pytest.raises(OmniParserError, match=MAX_RESULT_BYTES_ENV_VAR) as info:
        execute_find_element(FindElementPayload(None, None))

    assert info.value.phase == FIND_PHASE_SERIALIZE
    assert info.value.code == ErrorCode.FIND_ELEMENT_FAILED.value


def test_bound_result_downscales_image_but_keeps_element_coordinates() -> None:
    result = FindElementResultPayload(
        success=True,
        annotated_image_base64=_noisy_png_base64(1800, 600),
        elements=(_element(),),
        image_width=1800,
        image_height=600,
    )
    limit = 3_000_000
    assert measure_result_message_bytes(result) > limit

    bounded = bound_find_element_result(result, limit)

    assert measure_result_message_bytes(bounded) <= limit
    assert _decoded_size(bounded.annotated_image_base64) == (1350, 450)
    assert bounded.elements == result.elements
    assert bounded.image_width == 1800
    assert bounded.image_height == 600


def test_bound_result_returns_input_when_already_small() -> None:
    result = FindElementResultPayload(
        success=True,
        annotated_image_base64=_png_base64(Image.new("RGB", (64, 48), "blue")),
        elements=(_element(),),
        image_width=64,
        image_height=48,
    )

    assert bound_find_element_result(result, 8 * 1024 * 1024) is result


def test_bound_result_refuses_to_shrink_below_minimum_edge() -> None:
    result = FindElementResultPayload(
        success=True,
        annotated_image_base64=_noisy_png_base64(1280, 40),
        elements=(),
        image_width=1280,
        image_height=40,
    )

    with pytest.raises(ExecutionError, match=MAX_RESULT_BYTES_ENV_VAR) as info:
        bound_find_element_result(result, 1000)

    assert "1000-byte limit" in str(info.value)
