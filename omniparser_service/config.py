from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class OmniParserSettings(BaseSettings):
    api_key: str = ""
    weights_dir: str = str(
        Path(__file__).resolve().parent.parent / "OmniParser" / "weights"
    )
    box_threshold: float = 0.05
    iou_threshold: float = 0.7
    caption_batch_size: int = 64
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    preload_models: bool = False

    model_config = {"env_prefix": "OMNIPARSER_"}


settings = OmniParserSettings()
