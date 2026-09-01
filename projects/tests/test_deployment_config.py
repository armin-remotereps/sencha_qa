from __future__ import annotations

import re

from django.conf import settings
from django.test import SimpleTestCase

# Upper bound the controller enforces on a single find_element_result message
# (controller_client/omniparser_config.py OMNIPARSER_MAX_RESULT_BYTES default).
_CONTROLLER_MAX_RESULT_BYTES = 8 * 1024 * 1024


def _daphne_entrypoint() -> str:
    compose = (settings.BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(r'entrypoint:\s*"(python -m daphne[^"]*)"', compose)
    assert match is not None, "docker-compose.yml has no daphne entrypoint"
    return match.group(1)


def _flag_value(command: str, flag: str) -> int | None:
    match = re.search(rf"{re.escape(flag)}\s+(\d+)", command)
    return int(match.group(1)) if match else None


class DaphneWebSocketLimitTests(SimpleTestCase):
    """Daphne defaults both limits to 1 MiB and closes the socket (1009) on
    anything larger; controller screenshots and OmniParser results exceed that."""

    def test_incoming_message_limit_covers_controller_results(self) -> None:
        limit = _flag_value(_daphne_entrypoint(), "--websocket-max-message-size")
        self.assertIsNotNone(limit, "api entrypoint lacks --websocket-max-message-size")
        assert limit is not None
        self.assertGreater(limit, _CONTROLLER_MAX_RESULT_BYTES)

    def test_frame_limit_matches_message_limit(self) -> None:
        entrypoint = _daphne_entrypoint()
        message_limit = _flag_value(entrypoint, "--websocket-max-message-size")
        frame_limit = _flag_value(entrypoint, "--websocket-max-frame-size")
        self.assertIsNotNone(
            frame_limit, "api entrypoint lacks --websocket-max-frame-size"
        )
        self.assertEqual(frame_limit, message_limit)
