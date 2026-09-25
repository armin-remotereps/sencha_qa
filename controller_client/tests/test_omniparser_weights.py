from __future__ import annotations

from pathlib import Path

import pytest

from controller_client.omniparser_weights import (
    find_caption_checkpoint,
    main,
    normalize_caption_weights_layout,
    resolve_omniparser_weights,
    validate_weights_layout,
)


def _write_detector(weights_dir: Path) -> Path:
    detector = weights_dir / "icon_detect" / "model.pt"
    detector.parent.mkdir(parents=True, exist_ok=True)
    detector.write_bytes(b"detector")
    return detector


def _write_caption_dir(
    weights_dir: Path, name: str, checkpoint_name: str | None
) -> Path:
    caption = weights_dir / name
    caption.mkdir(parents=True, exist_ok=True)
    (caption / "config.json").write_text("{}", encoding="utf-8")
    (caption / "generation_config.json").write_text("{}", encoding="utf-8")
    if checkpoint_name is not None:
        (caption / checkpoint_name).write_bytes(b"caption")
    return caption


def test_find_caption_checkpoint_ignores_config_only_directory(
    tmp_path: Path,
) -> None:
    caption = _write_caption_dir(tmp_path, "icon_caption_florence", None)

    assert find_caption_checkpoint(caption) is None


def test_find_caption_checkpoint_accepts_a_sharded_checkpoint_index(
    tmp_path: Path,
) -> None:
    caption = _write_caption_dir(
        tmp_path, "icon_caption_florence", "model.safetensors.index.json"
    )

    assert find_caption_checkpoint(caption) == caption / "model.safetensors.index.json"


def test_normalize_renames_a_freshly_downloaded_caption_folder(
    tmp_path: Path,
) -> None:
    _write_caption_dir(tmp_path, "icon_caption", "model.safetensors")

    normalize_caption_weights_layout(tmp_path)

    assert not (tmp_path / "icon_caption").exists()
    assert (tmp_path / "icon_caption_florence" / "model.safetensors").is_file()
    assert (tmp_path / "icon_caption_florence" / "config.json").is_file()


def test_normalize_repairs_a_checkpointless_destination_from_a_partial_download(
    tmp_path: Path,
) -> None:
    """The exact state an interrupted download leaves behind.

    icon_caption_florence exists but holds only config files, so the old
    shell-level rename guard skipped and the real checkpoint stayed in
    icon_caption.
    """
    _write_caption_dir(tmp_path, "icon_caption_florence", None)
    _write_caption_dir(tmp_path, "icon_caption", "model.safetensors")

    normalize_caption_weights_layout(tmp_path)

    assert not (tmp_path / "icon_caption").exists()
    assert (tmp_path / "icon_caption_florence" / "model.safetensors").is_file()


def test_normalize_keeps_extra_files_already_in_the_destination(
    tmp_path: Path,
) -> None:
    _write_caption_dir(tmp_path, "icon_caption_florence", None)
    (tmp_path / "icon_caption_florence" / "notes.txt").write_text("keep me")
    _write_caption_dir(tmp_path, "icon_caption", "model.safetensors")

    normalize_caption_weights_layout(tmp_path)

    assert (tmp_path / "icon_caption_florence" / "notes.txt").read_text() == "keep me"


def test_normalize_leaves_a_healthy_layout_untouched(tmp_path: Path) -> None:
    healthy = _write_caption_dir(tmp_path, "icon_caption_florence", "model.safetensors")
    (healthy / "model.safetensors").write_bytes(b"the real checkpoint")
    _write_caption_dir(tmp_path, "icon_caption", "model.safetensors")

    normalize_caption_weights_layout(tmp_path)

    assert (healthy / "model.safetensors").read_bytes() == b"the real checkpoint"
    assert (tmp_path / "icon_caption").is_dir()


def test_normalize_is_idempotent(tmp_path: Path) -> None:
    _write_caption_dir(tmp_path, "icon_caption", "model.safetensors")

    first = normalize_caption_weights_layout(tmp_path)
    second = normalize_caption_weights_layout(tmp_path)

    assert "Moved" in first
    assert "already in place" in second
    assert (tmp_path / "icon_caption_florence" / "model.safetensors").is_file()


def test_validate_returns_every_resolved_path(tmp_path: Path) -> None:
    detector = _write_detector(tmp_path)
    caption = _write_caption_dir(tmp_path, "icon_caption_florence", "model.safetensors")

    weights = validate_weights_layout(tmp_path)

    assert weights.som_model_path == detector
    assert weights.caption_model_path == caption
    assert weights.caption_checkpoint_path == caption / "model.safetensors"


def test_validate_rejects_a_caption_folder_without_a_checkpoint(
    tmp_path: Path,
) -> None:
    _write_detector(tmp_path)
    _write_caption_dir(tmp_path, "icon_caption_florence", None)

    with pytest.raises(FileNotFoundError, match="hold no model checkpoint"):
        validate_weights_layout(tmp_path)


def test_validate_rejects_a_missing_detector(tmp_path: Path) -> None:
    _write_caption_dir(tmp_path, "icon_caption_florence", "model.safetensors")

    with pytest.raises(FileNotFoundError, match="detector weights not found"):
        validate_weights_layout(tmp_path)


def test_validate_rejects_a_missing_caption_folder(tmp_path: Path) -> None:
    _write_detector(tmp_path)

    with pytest.raises(FileNotFoundError, match="caption weights not found"):
        validate_weights_layout(tmp_path)


def test_resolve_repairs_the_layout_before_validating(tmp_path: Path) -> None:
    _write_detector(tmp_path)
    _write_caption_dir(tmp_path, "icon_caption_florence", None)
    _write_caption_dir(tmp_path, "icon_caption", "model.safetensors")

    weights = resolve_omniparser_weights(tmp_path)

    assert weights.caption_checkpoint_path.is_file()
    assert not (tmp_path / "icon_caption").exists()


def test_main_reports_success_after_repairing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_detector(tmp_path)
    _write_caption_dir(tmp_path, "icon_caption_florence", None)
    _write_caption_dir(tmp_path, "icon_caption", "model.safetensors")

    exit_code = main([str(tmp_path)])

    assert exit_code == 0
    assert "Weights verified" in capsys.readouterr().out


def test_main_fails_when_the_download_is_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_detector(tmp_path)
    _write_caption_dir(tmp_path, "icon_caption_florence", None)

    exit_code = main([str(tmp_path)])

    assert exit_code == 1
    assert "hold no model checkpoint" in capsys.readouterr().err


def test_main_rejects_wrong_argument_count() -> None:
    assert main([]) == 2
