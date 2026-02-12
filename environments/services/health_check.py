from __future__ import annotations

import socket
import time

from django.conf import settings

from environments.types import ContainerPorts, HealthCheckResult

ENV_PORT_CHECK_TIMEOUT: int = getattr(settings, "ENV_PORT_CHECK_TIMEOUT", 2)


def check_container_health(ports: ContainerPorts) -> HealthCheckResult:
    ssh = _check_port("localhost", ports.ssh)
    vnc = _check_port("localhost", ports.vnc)

    # Chromium/CDP is started on-demand by browser tools, not at boot.
    # Skip checking the CDP port during container health checks.
    return HealthCheckResult(
        ssh=ssh,
        vnc=vnc,
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


def _check_port(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=ENV_PORT_CHECK_TIMEOUT):
            return True
    except (OSError, socket.error):
        return False
