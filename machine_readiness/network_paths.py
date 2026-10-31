"""Network (UNC, ``//host``) paths: who may introduce them and when they are read.

Opening ``\\\\host\\share\\…`` on Windows makes an SMB connection that can send
the runner user's NTLM credentials to that host, so a network path is only
ever opened when a person chose it (pure rules, no database access):

- The setup agent cannot introduce one: its profile writes may keep a network
  base directory or item path the saved profile already has, never add or
  change one (``agent_network_path_errors``). A person can, on the edit page,
  and a restore is a person's act. So every network base directory in a
  profile was entered by a person.
- The analysis drops network check targets (``stage0_rules``).
- The quick check inspects a network path only when it lies under the
  profile's network base directory for the runner's OS
  (``network_inspection_allowed``); otherwise it is left unknown.
- The scan of a network base directory, and an inspection allowed above, pass
  ``allow_network`` to the runner, which refuses network paths otherwise.
"""

from __future__ import annotations

import ntpath

from controller_client.protocol import is_network_path
from machine_readiness.models import ArtifactOs
from machine_readiness.types import ProfileData

NETWORK_PATH_NOT_CHECKED = "Network path not checked."

__all__ = [
    "NETWORK_PATH_NOT_CHECKED",
    "agent_network_path_errors",
    "is_network_path",
    "network_inspection_allowed",
]


def network_inspection_allowed(path: str, base_dir: str, os_name: ArtifactOs) -> bool:
    """Whether the network ``path`` may be opened: it is the profile's network
    ``base_dir`` (for ``os_name``) or below it, with no ``..`` segment."""
    if not (is_network_path(path) and is_network_path(base_dir)):
        return False
    if os_name == ArtifactOs.WINDOWS:
        target = ntpath.normcase(path.strip())
        base = ntpath.normcase(base_dir.strip()).rstrip("\\")
        separator = "\\"
    else:
        target = path.strip()
        base = base_dir.strip().rstrip("/")
        separator = "/"
    if ".." in target.split(separator):
        return False
    return target == base or target.startswith(base + separator)


def agent_network_path_errors(data: ProfileData, current: ProfileData) -> list[str]:
    """Network paths a setup-agent write would add or change (refused).

    Base directories and item paths equal to the saved profile's are kept.
    """
    errors: list[str] = []
    for os_name in ArtifactOs:
        new = data.base_dir_for(os_name)
        if is_network_path(new) and new != current.base_dir_for(os_name):
            errors.append(
                f"{os_name.label} base directory: a network path (\\\\server or "
                "//server) can only be set by a person on the Machine readiness "
                "tab."
            )
    saved_patterns = {
        (item.key, item.path_pattern)
        for item in current.items
        if is_network_path(item.path_pattern)
    }
    for position, item in enumerate(data.items, start=1):
        if (
            is_network_path(item.path_pattern)
            and (item.key, item.path_pattern) not in saved_patterns
        ):
            errors.append(
                f"Artifact {position} ({item.key}): a network path can only be "
                "set by a person on the Machine readiness tab; use a path "
                "relative to the base directory."
            )
    return errors
