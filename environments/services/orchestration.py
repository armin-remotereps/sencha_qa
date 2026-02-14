from __future__ import annotations

import logging

import docker

from environments.services.container import ensure_container_running, remove_container
from environments.services.health_check import wait_for_container_ready
from environments.services.image import ensure_environment_image
from environments.services.vnc import check_vnc_connection
from environments.types import ContainerInfo, ContainerPorts, HealthCheckResult

logger = logging.getLogger(__name__)


def provision_environment(
    client: docker.DockerClient,
    *,
    name_suffix: str | None = None,
    api_key: str,
) -> ContainerInfo:
    ensure_environment_image(client)
    container_info = ensure_container_running(
        client, name_suffix=name_suffix, api_key=api_key
    )
    wait_for_container_ready(container_info.ports)
    return container_info


def teardown_environment(client: docker.DockerClient, container_id: str) -> None:
    remove_container(client, container_id, force=True)


def verify_vnc_service(ports: ContainerPorts) -> bool:
    return check_vnc_connection(ports)


def full_verification(container_info: ContainerInfo) -> HealthCheckResult:
    return HealthCheckResult(
        vnc=verify_vnc_service(container_info.ports),
    )
