"""Release-event ingestion log with deduplication (JSON-backed)."""

from __future__ import annotations

import json
from pathlib import Path

from depbot.models import ReleaseEvent


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._events: list[ReleaseEvent] = []
        if path.is_file():
            self._events = [
                ReleaseEvent.model_validate(e)
                for e in json.loads(path.read_text(encoding="utf-8"))
            ]

    def find_duplicate(self, event: ReleaseEvent) -> ReleaseEvent | None:
        """Same event_id, or same (dependency, from, to, type) under another id."""
        for seen in self._events:
            if seen.event_id == event.event_id or seen.dedup_key == event.dedup_key:
                return seen
        return None

    def record(self, event: ReleaseEvent) -> bool:
        """Persist the event. Returns False (and stores nothing) for a duplicate."""
        if self.find_duplicate(event) is not None:
            return False
        self._events.append(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [e.model_dump(mode="json") for e in self._events]
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return True

    @property
    def events(self) -> list[ReleaseEvent]:
        return list(self._events)
