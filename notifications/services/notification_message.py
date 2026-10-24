"""What one notification says, and how each kind is delivered.

The kind-to-delivery mapping (ntfy priority, emoji tags) and the kind-to-
preference-flag mapping are domain decisions kept in this one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

TITLE_MAX_LENGTH = 120
BODY_MAX_LENGTH = 300
_ELLIPSIS = "…"


class NotificationKind(StrEnum):
    NEED_HELP = "need_help"
    FAILED = "failed"
    CHAT_REPLY = "chat_reply"
    TEST = "test"


@dataclass(frozen=True)
class NotificationDelivery:
    # ntfy priority: 5 max, 4 high, 3 default, 2 low, 1 min.
    priority: int
    # ntfy tags; a tag matching an emoji short code renders as that emoji.
    tags: tuple[str, ...]


_DELIVERY: dict[NotificationKind, NotificationDelivery] = {
    NotificationKind.NEED_HELP: NotificationDelivery(priority=4, tags=("sos",)),
    NotificationKind.FAILED: NotificationDelivery(priority=3, tags=("x",)),
    NotificationKind.CHAT_REPLY: NotificationDelivery(
        priority=3, tags=("speech_balloon",)
    ),
    NotificationKind.TEST: NotificationDelivery(priority=3, tags=("white_check_mark",)),
}

# NotificationPreference boolean field a kind is opted into with; TEST has none
# (it goes to the requesting user whenever their notifications are on).
_PREFERENCE_FLAG: dict[NotificationKind, str | None] = {
    NotificationKind.NEED_HELP: "notify_need_help",
    NotificationKind.FAILED: "notify_failed",
    NotificationKind.CHAT_REPLY: "notify_chat_reply",
    NotificationKind.TEST: None,
}


def delivery_for(kind: NotificationKind) -> NotificationDelivery:
    return _DELIVERY[kind]


def preference_flag(kind: NotificationKind) -> str | None:
    """The ``NotificationPreference`` field that must be true for ``kind``."""
    return _PREFERENCE_FLAG[kind]


def _clean(text: str, max_length: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max_length - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


@dataclass(frozen=True)
class NotificationMessage:
    """One notification. Build it with :meth:`create` to normalise the text."""

    kind: NotificationKind
    title: str
    body: str
    # Same-origin path (from reverse()) the notification opens when clicked.
    path: str

    @classmethod
    def create(
        cls, *, kind: NotificationKind, title: str, body: str, path: str
    ) -> NotificationMessage:
        """Collapse whitespace and truncate the title and body to their limits."""
        return cls(
            kind=kind,
            title=_clean(title, TITLE_MAX_LENGTH),
            body=_clean(body, BODY_MAX_LENGTH),
            path=path,
        )

    @property
    def delivery(self) -> NotificationDelivery:
        return delivery_for(self.kind)
