from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping

from card_studio.production.contract import canonical_json
from card_studio.store import write_json_atomic

from .schema import PairingError, STORE_SCHEMA, event_digest, identifier, utc_now


class AtomicEnvelopeStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()

    def _path(self, event_id: str) -> Path:
        identifier(event_id, "event_id")
        return self.directory / "events" / f"{event_id}.json"

    def record(self, envelope: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
        digest = event_digest(event)
        path = self._path(event["event_id"])
        with self.lock:
            if path.exists():
                try:
                    stored = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as error:
                    raise PairingError("EVENT_STORE_CORRUPT") from error
                if stored.get("event_sha256") != digest:
                    raise PairingError("EVENT_ID_CONFLICT")
                return False
            value = {
                "schema": STORE_SCHEMA,
                "stored_at": utc_now(),
                "event_sha256": digest,
                "envelope": dict(envelope),
            }
            if canonical_json(value["envelope"]) != canonical_json(envelope):
                raise PairingError("INVALID_ENVELOPE_STORAGE")
            write_json_atomic(path, value)
            return True

    def ping(self) -> bool:
        return self.directory.is_dir()
