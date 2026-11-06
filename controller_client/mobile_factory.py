"""Build the phone or tablet backend for one discovered device.

Construction only: no Appium server, no session. ``main`` selects the
backend as the target and the device monitor starts its session.
"""

from __future__ import annotations

from controller_client.mobile_android import AndroidPlatform
from controller_client.mobile_app_policy import MobileAppPolicy
from controller_client.mobile_appium_server import ManagedAppiumServer
from controller_client.mobile_backend import MobileBackend
from controller_client.mobile_device import MobileTargetBackend
from controller_client.mobile_discovery import DiscoveredDevice
from controller_client.mobile_ios import IosPlatform
from controller_client.mobile_platform import MobilePlatform
from controller_client.mobile_session import MobileSession
from controller_client.mobile_settings import MobileSettings
from controller_client.protocol import RunnerTarget


def build_platform(settings: MobileSettings) -> MobilePlatform:
    if settings.target is RunnerTarget.ANDROID:
        return AndroidPlatform()
    if settings.target is RunnerTarget.IOS:
        return IosPlatform(settings)
    raise ValueError(f"Not a phone target: {settings.target.value}")


def build_mobile_backend(
    settings: MobileSettings, device: DiscoveredDevice
) -> MobileTargetBackend:
    platform = build_platform(settings)
    return MobileBackend(
        settings=settings,
        device=device,
        platform=platform,
        session=MobileSession(device, platform),
        server=ManagedAppiumServer(
            settings.appium_command, settings.target, settings.log_dir
        ),
        policy=MobileAppPolicy(settings.target, settings.ios_wda_bundle_id),
    )
