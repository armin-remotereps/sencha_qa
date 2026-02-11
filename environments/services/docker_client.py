from __future__ import annotations

import docker
from django.conf import settings


def get_docker_client() -> docker.DockerClient:
    return docker.DockerClient(base_url=settings.DOCKER_HOST)


def close_docker_client(client: docker.DockerClient) -> None:
    client.close()
