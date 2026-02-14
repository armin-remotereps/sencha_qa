from __future__ import annotations

import logging
from pathlib import Path

import docker
import docker.errors
from django.conf import settings

logger = logging.getLogger(__name__)


def image_exists(client: docker.DockerClient) -> bool:
    image_tag = _get_image_tag()
    try:
        client.images.get(image_tag)
        return True
    except docker.errors.ImageNotFound:
        return False


def build_environment_image(
    client: docker.DockerClient, *, nocache: bool = False
) -> str:
    image_tag = _get_image_tag()
    dockerfile_path = Path(__file__).resolve().parent.parent.parent

    stream = client.api.build(
        path=str(dockerfile_path),
        dockerfile="environments/docker/Dockerfile",
        tag=image_tag,
        buildargs={
            "VNC_PASSWORD": settings.ENV_VNC_PASSWORD,
        },
        nocache=nocache,
        rm=True,
        decode=True,
    )

    for chunk in stream:
        if "stream" in chunk:
            line = chunk["stream"].rstrip("\n")
            if line:
                logger.info(line)
        elif "error" in chunk:
            error_msg = chunk["error"].rstrip("\n")
            logger.error(error_msg)
            raise docker.errors.BuildError(error_msg, [])

    return image_tag


def ensure_environment_image(client: docker.DockerClient) -> str:
    if not image_exists(client):
        return build_environment_image(client)
    return _get_image_tag()


def _get_image_tag() -> str:
    return f"{settings.ENV_IMAGE_NAME}:{settings.ENV_IMAGE_TAG}"
