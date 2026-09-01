from __future__ import annotations

import asyncio

from django.test import SimpleTestCase

from projects.controller_reply_tracker import ReplyTracker


class _FakeChannelLayer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    async def send(self, channel: str, message: dict[str, object]) -> None:
        self.sent.append((channel, message))


class SendErrorTests(SimpleTestCase):
    def test_forwards_error_to_the_pending_reply_channel(self) -> None:
        layer = _FakeChannelLayer()
        # _FakeChannelLayer duck-types BaseChannelLayer's .send() without
        # subclassing its ABC; mypy can't see the structural match.
        tracker = ReplyTracker(layer)  # type: ignore[arg-type]
        tracker.register_reply_channel("req-1", "reply-channel-1")

        delivered = asyncio.run(
            tracker.send_error(
                "req-1", {"code": "FIND_ELEMENT_FAILED", "message": "weights missing"}
            )
        )

        self.assertTrue(delivered)
        self.assertEqual(len(layer.sent), 1)
        channel, message = layer.sent[0]
        self.assertEqual(channel, "reply-channel-1")
        self.assertEqual(message["type"], "error.result")
        self.assertEqual(message["message"], "weights missing")
        self.assertFalse(tracker.has_pending_reply("req-1"))

    def test_forwards_details_when_the_controller_provides_them(self) -> None:
        layer = _FakeChannelLayer()
        # _FakeChannelLayer duck-types BaseChannelLayer's .send() without
        # subclassing its ABC; mypy can't see the structural match.
        tracker = ReplyTracker(layer)  # type: ignore[arg-type]
        tracker.register_reply_channel("req-1", "reply-channel-1")

        asyncio.run(
            tracker.send_error(
                "req-1",
                {
                    "code": "FIND_ELEMENT_FAILED",
                    "message": "weights missing",
                    "details": "phase=weights; device=cpu",
                },
            )
        )

        _, message = layer.sent[0]
        self.assertEqual(message["details"], "phase=weights; device=cpu")

    def test_defaults_details_to_empty_string_when_absent(self) -> None:
        layer = _FakeChannelLayer()
        # _FakeChannelLayer duck-types BaseChannelLayer's .send() without
        # subclassing its ABC; mypy can't see the structural match.
        tracker = ReplyTracker(layer)  # type: ignore[arg-type]
        tracker.register_reply_channel("req-1", "reply-channel-1")

        asyncio.run(
            tracker.send_error("req-1", {"code": "TIMEOUT", "message": "timed out"})
        )

        _, message = layer.sent[0]
        self.assertEqual(message["details"], "")

    def test_returns_false_when_no_pending_reply(self) -> None:
        layer = _FakeChannelLayer()
        # _FakeChannelLayer duck-types BaseChannelLayer's .send() without
        # subclassing its ABC; mypy can't see the structural match.
        tracker = ReplyTracker(layer)  # type: ignore[arg-type]

        delivered = asyncio.run(
            tracker.send_error("unknown-req", {"message": "orphaned error"})
        )

        self.assertFalse(delivered)
        self.assertEqual(layer.sent, [])
