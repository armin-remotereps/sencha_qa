from __future__ import annotations

from pathlib import Path

from decouple import config as decouple_config


def _env_float(key: str, default: float) -> float:
    return float(str(decouple_config(key, default=str(default))))


def _env_int(key: str, default: int) -> int:
    return int(str(decouple_config(key, default=str(default))))


def _env_str(key: str, default: str) -> str:
    return str(decouple_config(key, default=default))


_DEFAULT_WEIGHTS_DIR = str(Path(__file__).resolve().parent / "omniparser" / "weights")


def omniparser_weights_dir() -> str:
    return _env_str("OMNIPARSER_WEIGHTS_DIR", _DEFAULT_WEIGHTS_DIR)


def omniparser_box_threshold() -> float:
    return _env_float("OMNIPARSER_BOX_THRESHOLD", 0.05)


def omniparser_iou_threshold() -> float:
    return _env_float("OMNIPARSER_IOU_THRESHOLD", 0.7)


def omniparser_caption_batch_size() -> int:
    return _env_int("OMNIPARSER_CAPTION_BATCH_SIZE", 64)


def omniparser_max_result_bytes() -> int:
    return _env_int("OMNIPARSER_MAX_RESULT_BYTES", 8 * 1024 * 1024)
