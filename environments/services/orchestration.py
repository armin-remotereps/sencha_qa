from __future__ import annotations

import logging
import time

import docker
import paramiko

from environments.services.container import ensure_container_running, remove_container
from environments.services.health_check import wait_for_container_ready
from environments.services.image import ensure_environment_image
from environments.services.playwright import verify_playwright_connection
from environments.services.ssh import (
    close_ssh_connection,
    create_ssh_connection,
    execute_ssh_command,
)
from environments.services.vnc import check_vnc_connection
from environments.types import ContainerInfo, ContainerPorts, HealthCheckResult

logger = logging.getLogger(__name__)


def provision_environment(
    client: docker.DockerClient, *, name_suffix: str | None = None
) -> ContainerInfo:
    ensure_environment_image(client)
    container_info = ensure_container_running(client, name_suffix=name_suffix)
    wait_for_container_ready(container_info.ports)
    return container_info


def teardown_environment(client: docker.DockerClient, container_id: str) -> None:
    remove_container(client, container_id, force=True)


def verify_ssh_service(ports: ContainerPorts, *, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            ssh_client = create_ssh_connection(ports)
            result = execute_ssh_command(ssh_client, "echo hello")
            close_ssh_connection(ssh_client)
            return result.exit_code == 0 and "hello" in result.stdout
        except (OSError, paramiko.SSHException) as e:
            logger.debug("SSH verification attempt %d failed: %s", attempt + 1, e)
            if attempt < retries - 1:
                time.sleep(2)
    return False


def verify_vnc_service(ports: ContainerPorts) -> bool:
    return check_vnc_connection(ports)


def verify_playwright_service(ports: ContainerPorts) -> bool:
    return verify_playwright_connection(ports)


def full_verification(container_info: ContainerInfo) -> HealthCheckResult:
    # Chromium/CDP is started on-demand by browser tools, not at boot.
    # Only verify SSH and VNC at provisioning time.
    return HealthCheckResult(
        ssh=verify_ssh_service(container_info.ports),
        vnc=verify_vnc_service(container_info.ports),
    )
