"""Live activity log: a small in-memory event stream describing what the system is doing right now.

Services publish short, human-readable events (readings received, window closed, chunk written,
embedded, stored, chat stages, diagnosis stages). The app's Live view shows them as an animated
pipeline. Events are not persisted: this is a window onto the running system, not a record of it.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

MAX_EVENTS = 400


@dataclass(frozen=True)
class ActivityEvent:
    id: int
    ts: str
    kind: str
    title: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "kind": self.kind, "title": self.title, "data": self.data}


class ActivityLog:
    def __init__(self, maxlen: int = MAX_EVENTS):
        self._events: deque[ActivityEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._next_id = 1
        self.counters: Counter[str] = Counter()

    def publish(self, kind: str, title: str, **data) -> ActivityEvent:
        with self._lock:
            event = ActivityEvent(
                self._next_id, datetime.now(timezone.utc).isoformat(), kind, title, data
            )
            self._next_id += 1
            self._events.append(event)
            return event

    def count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counters[key] += n

    def since(self, after_id: int = 0, limit: int = 100) -> list[ActivityEvent]:
        with self._lock:
            return [e for e in self._events if e.id > after_id][-limit:]

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._next_id - 1

    def snapshot_counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self.counters)


# One log per process. Services publish without needing it passed through every constructor.
ACTIVITY = ActivityLog()

SOURCE_LABELS = {
    "sensor_window": "15-minute window summary",
    "sensor_daily": "daily summary",
    "sensor_sessions": "sensor on/off log",
    "sensor_digest_llm": "AI daily interpretation",
    "diagnosis": "crop diagnosis",
    "agent_finding": "web research finding",
    "farm_note": "farmer note",
    "document": "farm document",
}
