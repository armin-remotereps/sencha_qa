from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HandshakeRequest:
    api_key: str
    system_info: dict[str, Any]
    request_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HandshakeRequest:
        return cls(
            api_key=data.get("api_key", ""),
            system_info=data.get("system_info", {}),
            request_id=data.get("request_id", ""),
        )


@dataclass(frozen=True)
class IncomingMessage:
    message_type: str
    request_id: str
    data: dict[str, Any]

    @classmethod
    def from_json(cls, text_data: str) -> IncomingMessage | None:
        import json

        try:
            data: dict[str, Any] = json.loads(text_data)
            return cls(
                message_type=data.get("type", ""),
                request_id=data.get("request_id", ""),
                data=data,
            )
        except json.JSONDecodeError:
            return None
