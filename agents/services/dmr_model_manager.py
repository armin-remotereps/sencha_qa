from __future__ import annotations

import logging

import httpx

from agents.types import ChatMessage, DMRConfig

logger = logging.getLogger(__name__)

_DOCKER_IO_PREFIX = "docker.io/"


def _normalize_model_id(model_id: str) -> str:
    if model_id.startswith(_DOCKER_IO_PREFIX):
        return model_id[len(_DOCKER_IO_PREFIX) :]
    return model_id


def list_models(config: DMRConfig) -> list[str]:
    url = f"http://{config.host}:{config.port}/engines/llama.cpp/v1/models"
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url)
        response.raise_for_status()
    data = response.json()
    models: list[str] = []
    raw_data = data.get("data")
    if isinstance(raw_data, list):
        for item in raw_data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str):
                    models.append(_normalize_model_id(model_id))
    return models


def is_model_available(config: DMRConfig) -> bool:
    try:
        models = list_models(config)
        return config.model in models
    except Exception as e:
        logger.debug("Failed to check model availability: %s", e)
        return False


def ensure_model_available(config: DMRConfig) -> None:
    if config.api_key is not None:
        return
    if is_model_available(config):
        logger.debug("Model already available: %s", config.model)
        return
    logger.warning(
        "Model %s not found on %s:%s. "
        "Please ensure the model is installed on the DMR instance.",
        config.model,
        config.host,
        config.port,
    )


def warm_up_model(config: DMRConfig) -> None:
    if config.api_key is not None:
        return

    from agents.services.dmr_client import send_chat_completion

    logger.info("Warming up model: %s", config.model)
    try:
        messages = (ChatMessage(role="user", content="hi"),)
        send_chat_completion(config, messages, keep_alive=-1)
        logger.info("Model warm-up complete: %s", config.model)
    except Exception as e:
        logger.warning("Model warm-up failed for %s: %s", config.model, e)
