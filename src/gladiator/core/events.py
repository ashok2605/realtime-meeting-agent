"""EventBus: every event fans out to a JSONL log, an in-memory ring
(dashboard backlog), and live subscriber queues. Events must be
self-describing (`replay` re-drives the dashboard from the file).
emit() is synchronous and never blocks — the Gemini receive loop calls it.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

RING_SIZE = 20_000
SUBSCRIBER_QUEUE_SIZE = 2_000


class EventBus:
    def __init__(self, log_path: Path | None = None) -> None:
        self.ring: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._log_file = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = log_path.open("a", encoding="utf-8")
        self.log_path = log_path

    def emit(self, type_: str, **data: Any) -> dict[str, Any]:
        event = {
            "ts": time.time(),
            "mono": time.monotonic(),
            "type": type_,
            "data": data,
        }
        self.ring.append(event)
        if self._log_file is not None:
            self._log_file.write(json.dumps(event, default=str) + "\n")
            self._log_file.flush()
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Dashboard fell behind; drop oldest so live view stays live.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except asyncio.QueueEmpty:
                    pass
        return event

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def close(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
