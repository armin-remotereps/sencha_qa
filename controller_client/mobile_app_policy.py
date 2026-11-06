"""Which apps on a phone may be terminated, and which ones cleanup closes.

Never terminated: Appium's helper apps (the UiAutomator2 server and Appium
Settings), the WebDriverAgent runner and the launcher / SpringBoard; closing
any of them would end the session or leave the device without a home
screen. Cleanup closes only the apps launched since the last cleanup.
"""

from __future__ import annotations

import threading
from typing import Final

from controller_client.protocol import RunnerTarget

_ANDROID_PROTECTED_PREFIXES: Final[tuple[str, ...]] = (
    "io.appium.uiautomator2.server",
    "io.appium.settings",
)
_ANDROID_PROTECTED: Final[frozenset[str]] = frozenset(
    {
        "android",
        "com.android.systemui",
        # Common launchers; the one actually in use is added at runtime.
        "com.android.launcher",
        "com.android.launcher2",
        "com.android.launcher3",
        "com.google.android.apps.nexuslauncher",
        "com.google.android.launcher",
        "com.sec.android.app.launcher",
        "com.miui.home",
        "com.huawei.android.launcher",
        "com.oppo.launcher",
        "com.coloros.launcher",
        "net.oneplus.launcher",
        "com.motorola.launcher3",
        "com.teslacoilsw.launcher",
    }
)
_IOS_PROTECTED: Final[frozenset[str]] = frozenset({"com.apple.springboard"})
_WDA_RUNNER_MARKER: Final[str] = "webdriveragentrunner"


class MobileAppPolicy:
    def __init__(self, target: RunnerTarget, wda_bundle_id: str) -> None:
        self._target = target
        self._wda_bundle_id = wda_bundle_id.strip().lower()
        self._lock = threading.Lock()
        self._launchers: set[str] = set()
        self._launched: list[str] = []

    def is_protected(self, app_id: str) -> bool:
        lowered = app_id.strip().lower()
        with self._lock:
            if lowered in self._launchers:
                return True
        if self._target is RunnerTarget.ANDROID:
            return lowered in _ANDROID_PROTECTED or lowered.startswith(
                _ANDROID_PROTECTED_PREFIXES
            )
        if lowered in _IOS_PROTECTED or _WDA_RUNNER_MARKER in lowered:
            return True
        # A preinstalled WDA is installed as "<bundle id>.xctrunner".
        return bool(self._wda_bundle_id) and lowered.startswith(self._wda_bundle_id)

    def add_launcher(self, app_id: str) -> None:
        """Protect the app shown after pressing home (the device's launcher)."""
        if app_id:
            with self._lock:
                self._launchers.add(app_id.strip().lower())

    def note_launched(self, app_id: str) -> None:
        if not app_id or self.is_protected(app_id):
            return
        with self._lock:
            if app_id not in self._launched:
                self._launched.append(app_id)

    def note_closed(self, app_id: str) -> None:
        with self._lock:
            if app_id in self._launched:
                self._launched.remove(app_id)

    def take_launched(self) -> list[str]:
        """The apps launched since the last call, oldest first; clears them."""
        with self._lock:
            launched, self._launched = self._launched, []
        return launched
