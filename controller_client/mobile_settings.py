"""Settings of a phone or tablet target, read from ``.env`` by ``config``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from controller_client.protocol import RunnerTarget


@dataclass(frozen=True)
class MobileSettings:
    # ``android`` or ``ios`` (never ``desktop``).
    target: RunnerTarget
    # Serial (Android) or UDID (iOS); empty: the only connected device.
    device_id: str
    # Empty: the runner starts and stops its own Appium on 127.0.0.1.
    appium_url: str
    # The command that starts Appium (``appium``), split with shlex.
    appium_command: str
    # iOS real devices: WebDriverAgent signing (``xcodeOrgId`` /
    # ``xcodeSigningId``) or a preinstalled WDA bundle id.
    ios_team_id: str
    ios_signing_id: str
    ios_wda_bundle_id: str
    ios_preinstalled_wda: bool
    # Where the managed Appium server writes its log.
    log_dir: Path
