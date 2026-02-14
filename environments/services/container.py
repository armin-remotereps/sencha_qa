from __future__ import annotations

from uuid import uuid4

import docker
import docker.errors
from django.conf import settings
from docker.models.containers import Container as DockerContainer

from environments.types import (
    PORT_CONFIG,
    ContainerInfo,
    ContainerPorts,
    DockerPortMapping,
)


def create_container(
    client: docker.DockerClient,
    *,
    name_suffix: str | None = None,
    api_key: str,
) -> ContainerInfo:
    from environments.services.image import _get_image_tag

    image_tag = _get_image_tag()
    container_name = _build_container_name(name_suffix)

    container = client.containers.create(
        image=image_tag,
        name=container_name,
        ports=PORT_CONFIG.to_docker_port_mapping(),
        environment={
            "CONTROLLER_HOST": settings.CONTROLLER_SERVER_HOST,
            "CONTROLLER_PORT": str(settings.CONTROLLER_SERVER_PORT),
            "CONTROLLER_API_KEY": api_key,
        },
        extra_hosts={"host.docker.internal": "host-gateway"},
        detach=True,
    )

    container.start()
    container.reload()
    return _build_container_info(container)


def ensure_container_running(
    client: docker.DockerClient,
    *,
    name_suffix: str | None = None,
    api_key: str,
) -> ContainerInfo:
    container_name = _build_container_name(name_suffix)

    existing = _find_container_by_name(client, container_name)
    if existing is not None:
        if not _is_container_running(existing):
            existing.start()
        existing.reload()
        return _build_container_info(existing)

    return create_container(client, name_suffix=name_suffix, api_key=api_key)


def get_container_info(client: docker.DockerClient, container_id: str) -> ContainerInfo:
    container = client.containers.get(container_id)
    container.reload()
    return _build_container_info(container)


def remove_container(
    client: docker.DockerClient, container_id: str, *, force: bool = True
) -> None:
    try:
        container = client.containers.get(container_id)
        container.remove(force=force)
    except docker.errors.NotFound:
        pass


def list_environment_containers(client: docker.DockerClient) -> list[ContainerInfo]:
    all_containers = client.containers.list(all=True)
    return [
        _build_container_info(container)
        for container in all_containers
        if container.name.startswith(settings.ENV_CONTAINER_PREFIX)
    ]


def _find_container_by_name(
    client: docker.DockerClient, name: str
) -> DockerContainer | None:
    try:
        return client.containers.get(name)
    except docker.errors.NotFound:
        return None


def _is_container_running(container: DockerContainer) -> bool:
    return str(container.status) == "running"


def _build_container_name(name_suffix: str | None) -> str:
    suffix = name_suffix or uuid4().hex[:8]
    return f"{settings.ENV_CONTAINER_PREFIX}-{suffix}"


def _build_container_info(container: DockerContainer) -> ContainerInfo:
    return ContainerInfo(
        container_id=container.id,
        name=container.name,
        ports=_extract_ports(container.ports),
        status=container.status,
    )


def _extract_ports(container_ports: DockerPortMapping) -> ContainerPorts:
    vnc_port = _get_host_port(container_ports, PORT_CONFIG.vnc)

    return ContainerPorts(
        vnc=vnc_port,
    )


def _get_host_port(container_ports: DockerPortMapping, key: str) -> int:
    port_mapping = container_ports.get(key)
    if port_mapping is None or len(port_mapping) == 0:
        return 0
    return int(port_mapping[0]["HostPort"])
