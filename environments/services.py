from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path
from uuid import uuid4

import docker
import docker.errors
import paramiko
from django.conf import settings
from playwright.sync_api import sync_playwright

from environments.types import (
    ContainerInfo,
    ContainerPorts,
    DockerPortMapping,
    HealthCheckResult,
    SSHResult,
    VerificationResult,
)

logger = logging.getLogger(__name__)

ENV_SSH_TIMEOUT: int = getattr(settings, "ENV_SSH_TIMEOUT", 10)
ENV_PORT_CHECK_TIMEOUT = 2

# ============================================================================
# DOCKER CLIENT
# ============================================================================


def get_docker_client() -> docker.DockerClient:
    return docker.DockerClient(base_url=settings.DOCKER_HOST)


def close_docker_client(client: docker.DockerClient) -> None:
    client.close()


# ============================================================================
# IMAGE MANAGEMENT
# ============================================================================


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
    dockerfile_path = Path(__file__).resolve().parent / "docker"

    stream = client.api.build(
        path=str(dockerfile_path),
        tag=image_tag,
        buildargs={
            "SSH_PASSWORD": settings.ENV_SSH_PASSWORD,
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


# ============================================================================
# CONTAINER LIFECYCLE
# ============================================================================


def create_container(
    client: docker.DockerClient, *, name_suffix: str | None = None
) -> ContainerInfo:
    image_tag = _get_image_tag()
    suffix = name_suffix or uuid4().hex[:8]
    container_name = f"{settings.ENV_CONTAINER_PREFIX}-{suffix}"

    container = client.containers.create(
        image=image_tag,
        name=container_name,
        ports={
            "22/tcp": None,
            "5900/tcp": None,
            "9223/tcp": None,
        },
        detach=True,
    )

    container.start()
    container.reload()

    ports = _extract_ports(container.ports)
    return ContainerInfo(
        container_id=container.id,
        name=container.name,
        ports=ports,
        status=container.status,
    )


def get_container_info(client: docker.DockerClient, container_id: str) -> ContainerInfo:
    container = client.containers.get(container_id)
    container.reload()

    ports = _extract_ports(container.ports)
    return ContainerInfo(
        container_id=container.id,
        name=container.name,
        ports=ports,
        status=container.status,
    )


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
    env_containers: list[ContainerInfo] = []

    for container in all_containers:
        if container.name.startswith(settings.ENV_CONTAINER_PREFIX):
            ports = _extract_ports(container.ports)
            env_containers.append(
                ContainerInfo(
                    container_id=container.id,
                    name=container.name,
                    ports=ports,
                    status=container.status,
                )
            )

    return env_containers


# ============================================================================
# HEALTH CHECKS
# ============================================================================


def check_container_health(ports: ContainerPorts) -> HealthCheckResult:
    ssh_ok = _check_port("localhost", ports.ssh)
    vnc_ok = _check_port("localhost", ports.vnc)
    playwright_ok = _check_port("localhost", ports.playwright_cdp)

    return HealthCheckResult(
        ssh_ok=ssh_ok,
        vnc_ok=vnc_ok,
        playwright_ok=playwright_ok,
    )


def wait_for_container_ready(
    ports: ContainerPorts, *, timeout: int | None = None, interval: int | None = None
) -> HealthCheckResult:
    timeout_seconds = timeout or settings.ENV_HEALTH_CHECK_TIMEOUT
    interval_seconds = interval or settings.ENV_HEALTH_CHECK_INTERVAL

    start_time = time.monotonic()
    end_time = start_time + timeout_seconds

    while time.monotonic() < end_time:
        result = check_container_health(ports)
        if result.all_ok:
            return result

        time.sleep(interval_seconds)

    msg = f"Container not ready after {timeout_seconds} seconds"
    raise TimeoutError(msg)


# ============================================================================
# SSH
# ============================================================================


def create_ssh_connection(ports: ContainerPorts) -> paramiko.SSHClient:
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh_client.connect(
        hostname="localhost",
        port=ports.ssh,
        username=settings.ENV_SSH_USER,
        password=settings.ENV_SSH_PASSWORD,
        timeout=ENV_SSH_TIMEOUT,
    )
    return ssh_client


def execute_ssh_command(ssh_client: paramiko.SSHClient, command: str) -> SSHResult:
    stdin, stdout, stderr = ssh_client.exec_command(command)
    exit_code = stdout.channel.recv_exit_status()

    return SSHResult(
        exit_code=exit_code,
        stdout=stdout.read().decode("utf-8"),
        stderr=stderr.read().decode("utf-8"),
    )


def close_ssh_connection(ssh_client: paramiko.SSHClient) -> None:
    ssh_client.close()


# ============================================================================
# VNC
# ============================================================================


def check_vnc_connection(ports: ContainerPorts) -> bool:
    try:
        with socket.create_connection(
            ("localhost", ports.vnc), timeout=ENV_PORT_CHECK_TIMEOUT
        ) as sock:
            data = sock.recv(3)
            return data == b"RFB"
    except (OSError, socket.error):
        return False


# ============================================================================
# PLAYWRIGHT
# ============================================================================


def get_playwright_cdp_url(ports: ContainerPorts) -> str:
    return f"http://localhost:{ports.playwright_cdp}"


def verify_playwright_connection(ports: ContainerPorts) -> bool:
    try:
        cdp_url = get_playwright_cdp_url(ports)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            page = browser.new_page()
            page.goto("about:blank")
            page.close()
            browser.close()
        return True
    except Exception as e:
        logger.debug("Playwright connection failed: %s", e)
        return False


# ============================================================================
# ORCHESTRATION
# ============================================================================


def provision_environment(
    client: docker.DockerClient, *, name_suffix: str | None = None
) -> ContainerInfo:
    ensure_environment_image(client)
    container_info = create_container(client, name_suffix=name_suffix)
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
        except Exception as e:
            logger.debug("SSH verification attempt %d failed: %s", attempt + 1, e)
            if attempt < retries - 1:
                time.sleep(2)
    return False


def verify_vnc_service(ports: ContainerPorts) -> bool:
    return check_vnc_connection(ports)


def verify_playwright_service(ports: ContainerPorts) -> bool:
    return verify_playwright_connection(ports)


def full_verification(container_info: ContainerInfo) -> VerificationResult:
    return VerificationResult(
        ssh=verify_ssh_service(container_info.ports),
        vnc=verify_vnc_service(container_info.ports),
        playwright=verify_playwright_service(container_info.ports),
    )


# ============================================================================
# PRIVATE HELPERS
# ============================================================================


def _get_image_tag() -> str:
    return f"{settings.ENV_IMAGE_NAME}:{settings.ENV_IMAGE_TAG}"


def _extract_ports(container_ports: DockerPortMapping) -> ContainerPorts:
    ssh_port = _get_host_port(container_ports, "22/tcp")
    vnc_port = _get_host_port(container_ports, "5900/tcp")
    cdp_port = _get_host_port(container_ports, "9223/tcp")

    return ContainerPorts(
        ssh=ssh_port,
        vnc=vnc_port,
        playwright_cdp=cdp_port,
    )


def _get_host_port(container_ports: DockerPortMapping, key: str) -> int:
    port_mapping = container_ports.get(key)
    if port_mapping is None or len(port_mapping) == 0:
        return 0
    return int(port_mapping[0]["HostPort"])


def _check_port(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=ENV_PORT_CHECK_TIMEOUT):
            return True
    except (OSError, socket.error):
        return False
