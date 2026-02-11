from __future__ import annotations

import paramiko
from django.conf import settings

from environments.types import ContainerPorts, SSHResult

ENV_SSH_TIMEOUT: int = getattr(settings, "ENV_SSH_TIMEOUT", 10)


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
