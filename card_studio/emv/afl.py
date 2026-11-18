from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


MAX_AFL_BYTES = 252
MAX_AFL_ENTRIES = MAX_AFL_BYTES // 4
MAX_RECORD_LOCATORS = 256


class AflError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AflEntry:
    sfi: int
    first_record: int
    last_record: int
    offline_auth_record_count: int = 0

    def __post_init__(self) -> None:
        for name in ("sfi", "first_record", "last_record", "offline_auth_record_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255:
                raise AflError("INVALID_AFL_VALUE", f"{name} debe ser un byte.")
        if not 1 <= self.sfi <= 30:
            raise AflError("INVALID_SFI", "SFI debe estar entre 1 y 30.")
        if self.first_record == 0 or self.last_record < self.first_record:
            raise AflError("INVALID_RECORD_RANGE", "Rango de records AFL inválido.")
        if self.offline_auth_record_count > self.record_count:
            raise AflError("INVALID_OFFLINE_AUTH_COUNT", "Offline count supera los records de la entrada.")

    @property
    def record_count(self) -> int:
        return self.last_record - self.first_record + 1

    def encode(self) -> bytes:
        return bytes((self.sfi << 3, self.first_record, self.last_record, self.offline_auth_record_count))

    def to_evidence(self) -> dict[str, Any]:
        return {
            "sfi": self.sfi,
            "first_record": self.first_record,
            "last_record": self.last_record,
            "offline_auth_record_count": self.offline_auth_record_count,
        }


@dataclass(frozen=True)
class RecordLocator:
    sfi: int
    record: int
    contributes_to_offline_auth: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.sfi <= 30 or not 1 <= self.record <= 255:
            raise AflError("INVALID_RECORD_LOCATOR", "Record locator fuera de dominio.")

    def to_evidence(self) -> dict[str, Any]:
        return {
            "sfi": self.sfi,
            "record": self.record,
            "contributes_to_offline_auth": self.contributes_to_offline_auth,
        }


@dataclass(frozen=True)
class ApplicationFileLocator:
    entries: tuple[AflEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        if not self.entries:
            raise AflError("EMPTY_AFL", "AFL no puede estar vacía.")
        if len(self.entries) > MAX_AFL_ENTRIES:
            raise AflError("AFL_LIMIT_EXCEEDED", "Demasiadas entradas AFL.")
        if any(not isinstance(entry, AflEntry) for entry in self.entries):
            raise AflError("INVALID_AFL_ENTRY", "entries debe contener AflEntry.")
        for index, entry in enumerate(self.entries):
            for prior in self.entries[:index]:
                if prior.sfi == entry.sfi and prior.first_record <= entry.last_record and entry.first_record <= prior.last_record:
                    raise AflError("OVERLAPPING_AFL_RANGES", "Rangos AFL solapados para el mismo SFI.")
        if sum(entry.record_count for entry in self.entries) > MAX_RECORD_LOCATORS:
            raise AflError("RECORD_PLAN_LIMIT_EXCEEDED", "El plan AFL supera 256 records.")

    @classmethod
    def parse(cls, raw: bytes) -> "ApplicationFileLocator":
        data = bytes(raw)
        if not data or len(data) % 4:
            raise AflError("MALFORMED_AFL", "AFL debe contener entradas completas de cuatro bytes.")
        if len(data) > MAX_AFL_BYTES:
            raise AflError("AFL_LIMIT_EXCEEDED", "AFL supera 252 bytes.")
        entries = []
        for offset in range(0, len(data), 4):
            sfi_byte, first, last, offline = data[offset:offset + 4]
            if sfi_byte & 0x07:
                raise AflError("INVALID_SFI_ENCODING", "Los bits bajos del byte SFI deben ser cero.")
            entries.append(AflEntry(sfi_byte >> 3, first, last, offline))
        return cls(tuple(entries))

    def encode(self) -> bytes:
        return b"".join(entry.encode() for entry in self.entries)

    def record_plan(self) -> tuple[RecordLocator, ...]:
        plan = []
        for entry in self.entries:
            for offset in range(entry.record_count):
                plan.append(RecordLocator(
                    entry.sfi,
                    entry.first_record + offset,
                    offset < entry.offline_auth_record_count,
                ))
        return tuple(plan)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_evidence() for entry in self.entries],
            "record_count": sum(entry.record_count for entry in self.entries),
            "mode": "LAB_ONLY",
        }


def parse_afl(raw: bytes) -> ApplicationFileLocator:
    return ApplicationFileLocator.parse(raw)


def record_plan(entries: ApplicationFileLocator | Iterable[AflEntry]) -> tuple[RecordLocator, ...]:
    selected = entries if isinstance(entries, ApplicationFileLocator) else ApplicationFileLocator(tuple(entries))
    return selected.record_plan()
