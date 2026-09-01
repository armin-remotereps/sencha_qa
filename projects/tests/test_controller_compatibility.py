from __future__ import annotations

from django.test import SimpleTestCase

from controller_client.protocol import CLIENT_VERSION, ClientCapability
from projects.services import check_controller_compatibility

_ALL_CAPABILITY_VALUES = tuple(c.value for c in ClientCapability)


class CheckControllerCompatibilityTests(SimpleTestCase):
    def test_accepts_client_reporting_every_required_capability(self) -> None:
        reason = check_controller_compatibility(CLIENT_VERSION, _ALL_CAPABILITY_VALUES)

        self.assertIsNone(reason)

    def test_rejects_stale_client_with_no_capabilities(self) -> None:
        reason = check_controller_compatibility("0.1.0", [])

        assert reason is not None
        for capability in _ALL_CAPABILITY_VALUES:
            self.assertIn(capability, reason)
        self.assertIn("0.1.0", reason)
        self.assertIn("download", reason.lower())

    def test_rejects_client_missing_a_single_capability(self) -> None:
        capabilities = [
            value
            for value in _ALL_CAPABILITY_VALUES
            if value != ClientCapability.FIND_ELEMENT_LOCAL_V1.value
        ]

        reason = check_controller_compatibility(CLIENT_VERSION, capabilities)

        assert reason is not None
        self.assertIn(ClientCapability.FIND_ELEMENT_LOCAL_V1.value, reason)
        for value in capabilities:
            self.assertNotIn(f"missing capabilities {value}", reason)

    def test_treats_missing_version_as_unknown(self) -> None:
        reason = check_controller_compatibility("", [])

        assert reason is not None
        self.assertIn("unknown", reason)
