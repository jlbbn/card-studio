from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .validation import normalize_tag


class DataSource(str, Enum):
    CARD = "CARD"
    TERMINAL = "TERMINAL"
    PROFILE = "PROFILE"
    HOST = "HOST"
    KERNEL = "KERNEL"


class OwnershipDomain(str, Enum):
    CARD = "CARD"
    TERMINAL = "TERMINAL"
    PROFILE = "PROFILE"
    HOST = "HOST"
    KERNEL = "KERNEL"


class DataSensitivity(str, Enum):
    PUBLIC = "PUBLIC"
    SENSITIVE = "SENSITIVE"
    SECRET = "SECRET"


class DataStoreError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DataValidationError(DataStoreError):
    pass


class OwnershipConflictError(DataStoreError):
    pass


class ProvenanceConflictError(DataStoreError):
    pass


class DuplicateDataError(DataStoreError):
    pass


class MandatoryDataError(DataStoreError):
    pass


class DataLimitError(DataStoreError):
    pass


def _timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DataValidationError("INVALID_TIMESTAMP", "timestamp debe incluir zona horaria.")
    return value.astimezone(timezone.utc)


def _context(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 128 or any(ord(char) < 0x20 for char in value):
        raise DataValidationError("INVALID_CONTEXT", "context debe ser texto visible de hasta 128 caracteres.")
    return value


@dataclass(frozen=True)
class DataRecord:
    tag: str
    value: bytes
    source: DataSource
    ownership: OwnershipDomain
    sensitivity: DataSensitivity = DataSensitivity.PUBLIC
    timestamp: datetime | None = None
    context: str | None = None

    def __post_init__(self) -> None:
        try:
            normalized = normalize_tag(self.tag)
        except ValueError as exc:
            raise DataValidationError("INVALID_TAG", str(exc)) from exc
        object.__setattr__(self, "tag", normalized)
        object.__setattr__(self, "source", DataSource(self.source))
        object.__setattr__(self, "ownership", OwnershipDomain(self.ownership))
        object.__setattr__(self, "sensitivity", DataSensitivity(self.sensitivity))
        try:
            object.__setattr__(self, "value", bytes(self.value))
        except (TypeError, ValueError) as exc:
            raise DataValidationError("INVALID_VALUE", "value debe ser bytes.") from exc
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp))
        object.__setattr__(self, "context", _context(self.context))

    @property
    def provenance(self) -> tuple[Any, ...]:
        return self.source, self.ownership, self.sensitivity, self.timestamp, self.context

    def to_evidence(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "source": self.source.value,
            "ownership": self.ownership.value,
            "sensitivity": self.sensitivity.value,
            "value_length": len(self.value),
            "timestamp": self.timestamp.isoformat().replace("+00:00", "Z") if self.timestamp else None,
            "context": self.context,
        }


class EmvDataStore:
    def __init__(self, max_records: int = 512, max_value_length: int = 4096):
        if not 1 <= max_records <= 4096 or not 1 <= max_value_length <= 65536:
            raise ValueError("Límites de DataStore fuera de rango.")
        self._max_records = max_records
        self._max_value_length = max_value_length
        self._records: dict[str, DataRecord] = {}

    def __len__(self) -> int:
        return len(self._records)

    def _validate_record(self, record: DataRecord) -> None:
        if not isinstance(record, DataRecord):
            raise DataValidationError("INVALID_RECORD", "Se requiere DataRecord.")
        if len(record.value) > self._max_value_length:
            raise DataLimitError("VALUE_LIMIT_EXCEEDED", "El valor supera max_value_length.")

    @staticmethod
    def _check_compatibility(current: DataRecord, candidate: DataRecord) -> None:
        if current.ownership != candidate.ownership:
            raise OwnershipConflictError(
                "OWNERSHIP_CONFLICT",
                f"El tag {candidate.tag} pertenece a otro ownership domain.",
            )
        if current.value != candidate.value:
            raise ProvenanceConflictError(
                "VALUE_CONFLICT",
                f"El tag {candidate.tag} ya contiene otro valor.",
            )
        if current.provenance != candidate.provenance:
            raise ProvenanceConflictError(
                "PROVENANCE_CONFLICT",
                f"El tag {candidate.tag} tiene procedencia o contexto distinto.",
            )

    def put(self, record: DataRecord) -> None:
        self.merge((record,))

    def merge(self, records: Iterable[DataRecord]) -> None:
        staged = dict(self._records)
        incoming: set[str] = set()
        for record in records:
            self._validate_record(record)
            if record.tag in incoming:
                raise DuplicateDataError("DUPLICATE_TAG", f"Tag duplicado en merge: {record.tag}.")
            incoming.add(record.tag)
            current = staged.get(record.tag)
            if current is not None:
                self._check_compatibility(current, record)
            staged[record.tag] = record
        if len(staged) > self._max_records:
            raise DataLimitError("RECORD_LIMIT_EXCEEDED", "El merge supera max_records.")
        self._records = staged

    def get(self, tag: str) -> DataRecord | None:
        return self._records.get(normalize_tag(tag))

    def require(self, tag: str) -> DataRecord:
        normalized = normalize_tag(tag)
        try:
            return self._records[normalized]
        except KeyError as exc:
            raise MandatoryDataError("MISSING_MANDATORY_DATA", f"Dato obligatorio ausente: {normalized}.") from exc

    def snapshot(self) -> Mapping[str, DataRecord]:
        return MappingProxyType(dict(self._records))

    def clone(self) -> "EmvDataStore":
        cloned = EmvDataStore(self._max_records, self._max_value_length)
        cloned.merge(self._records.values())
        return cloned

    def to_evidence(self) -> list[dict[str, Any]]:
        return [self._records[tag].to_evidence() for tag in sorted(self._records)]
