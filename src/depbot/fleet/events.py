"""Release-event ingestion log with deduplication.

Backed by the fleet's SQLite store (``<state_dir>/depbot.db``); the unique dedup
key makes recording idempotent even when two processes race. A legacy
``events.json`` next to the database is imported once. Reading never creates
state: a dry run on a fresh fleet leaves no files behind.
"""

from __future__ import annotations

import json
from pathlib import Path

from depbot.models import ReleaseEvent
from depbot.persistence.store import DB_NAME, Store


class EventLog:
    def __init__(self, path: Path, store: Store | None = None) -> None:
        self.path = Path(path)  # legacy JSON location; its directory holds the database
        self._store = store

    def _open(self, *, create: bool) -> Store | None:
        if self._store is None:
            db_path = self.path.parent / DB_NAME
            if not create and not db_path.is_file() and not self.path.is_file():
                return None
            self._store = Store(db_path)
            if self.path.is_file():  # one-time import of the pre-SQLite log (idempotent)
                for raw in json.loads(self.path.read_text(encoding="utf-8")):
                    self._store.record_event(ReleaseEvent.model_validate(raw))
        return self._store

    def find_duplicate(self, event: ReleaseEvent) -> ReleaseEvent | None:
        """Same event_id, or same (dependency, from, to, type) under another id."""
        store = self._open(create=False)
        return store.find_duplicate_event(event) if store is not None else None

    def record(self, event: ReleaseEvent) -> bool:
        """Persist the event. Returns False (and stores nothing) for a duplicate."""
        return self._open(create=True).record_event(event)

    @property
    def events(self) -> list[ReleaseEvent]:
        store = self._open(create=False)
        return store.events() if store is not None else []
