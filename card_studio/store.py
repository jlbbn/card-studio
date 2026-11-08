from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from .model import CardState, Operation
from .system import data_directory


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class JsonStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or data_directory()
        self.card_path = self.directory / "virtual-card.json"
        self.history_path = self.directory / "history.json"
        self.lock = threading.RLock()

    def load_card(self) -> CardState:
        with self.lock:
            if not self.card_path.exists():
                return CardState("SL-DEMO-5968", 0)
            try:
                value = json.loads(self.card_path.read_text(encoding="utf-8"))
                return CardState.from_dict(value)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError("No se pudo leer la tarjeta virtual local.") from exc

    def save_card(self, state: CardState) -> None:
        with self.lock:
            write_json_atomic(self.card_path, {"version": 1, **state.to_dict()})

    def history(self) -> list[Operation]:
        with self.lock:
            if not self.history_path.exists():
                return []
            try:
                values = json.loads(self.history_path.read_text(encoding="utf-8"))
                return [Operation.from_dict(value) for value in values[:100]]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError("No se pudo leer el historial local.") from exc

    def add_operation(self, operation: Operation) -> None:
        with self.lock:
            values = [operation.to_dict(), *(item.to_dict() for item in self.history())]
            write_json_atomic(self.history_path, values[:100])

    def update_operation(self, operation: Operation) -> None:
        with self.lock:
            values = [item.to_dict() for item in self.history()]
            for index, value in enumerate(values):
                if value["operationId"] == operation.operation_id:
                    values[index] = operation.to_dict()
                    break
            else:
                values.insert(0, operation.to_dict())
            write_json_atomic(self.history_path, values[:100])
