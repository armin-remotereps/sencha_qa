from __future__ import annotations

import threading
from collections import deque


class KeptOutputBuffer:
    """Output of a kept session waiting to be read, at most ``max_bytes``
    (UTF-8) of it.

    When full, the oldest text is discarded and the next ``take`` reports it.
    Safe to use from the reader thread and request threads at once.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._chunks: deque[tuple[str, int]] = deque()
        self._size = 0
        self._dropped = False
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        if not text:
            return
        encoded = text.encode("utf-8", errors="replace")
        with self._lock:
            if len(encoded) >= self._max_bytes:
                # The newest text alone fills the buffer: keep its tail.
                tail = encoded[-self._max_bytes :].decode("utf-8", errors="ignore")
                self._chunks.clear()
                self._chunks.append((tail, len(tail.encode("utf-8"))))
                self._size = self._chunks[0][1]
                self._dropped = True
                return
            self._chunks.append((text, len(encoded)))
            self._size += len(encoded)
            while self._size > self._max_bytes:
                _, size = self._chunks.popleft()
                self._size -= size
                self._dropped = True

    def take(self) -> tuple[str, bool]:
        """Everything buffered and whether older output was discarded since
        the last take; empties the buffer."""
        with self._lock:
            text = "".join(chunk for chunk, _ in self._chunks)
            dropped = self._dropped
            self._chunks.clear()
            self._size = 0
            self._dropped = False
        return text, dropped
