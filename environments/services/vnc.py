from __future__ import annotations

import socket

from django.conf import settings

from environments.types import ContainerPorts

ENV_PORT_CHECK_TIMEOUT: int = getattr(settings, "ENV_PORT_CHECK_TIMEOUT", 2)


def check_vnc_connection(ports: ContainerPorts) -> bool:
    try:
        with socket.create_connection(
            ("localhost", ports.vnc), timeout=ENV_PORT_CHECK_TIMEOUT
        ) as sock:
            data = sock.recv(3)
            return data == b"RFB"
    except (OSError, socket.error):
        return False
