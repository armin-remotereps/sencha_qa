import ctypes
import os
import sys

from controller_client.exceptions import PrivilegeError


def check_privileges() -> None:
    if sys.platform == "win32":
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise PrivilegeError(
                "Controller client must run as Administrator. "
                "Right-click terminal → 'Run as administrator'"
            )
    elif os.geteuid() != 0:
        raise PrivilegeError("Controller client must run as root. Use: sudo <command>")
