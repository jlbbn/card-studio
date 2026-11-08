from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .data import DataStoreError, EmvDataStore, MandatoryDataError
from .validation import normalize_tag


class DolKind(str, Enum):
    PDOL = "PDOL"
    CDOL1 = "CDOL1"
    CDOL2 = "CDOL2"
    DDOL = "DDOL"
    TDOL = "TDOL"
    DRDOL = "DRDOL"


class DolDataType(str, Enum):
    NUMERIC = "NUMERIC"
    BINARY = "BINARY"
    TEXT = "TEXT"


class TruncationPolicy(str, Enum):
    REJECT = "REJECT"
    ALLOW = "ALLOW"


class DolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DolEntry:
    tag: str
    length: int
    data_type: DolDataType = DolDataType.BINARY
    truncation: TruncationPolicy = TruncationPolicy.REJECT

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "tag", normalize_tag(self.tag))
        except ValueError as exc:
            raise DolError("INVALID_DOL_TAG", str(exc)) from exc
        if bytes.fromhex(self.tag)[0] & 0x20:
            raise DolError("INVALID_DOL_TAG", "DOL solo admite tags primitivos.")
        if not isinstance(self.length, int) or isinstance(self.length, bool) or not 0 <= self.length <= 255:
            raise DolError("INVALID_DOL_LENGTH", "La longitud DOL debe estar entre 0 y 255.")
        object.__setattr__(self, "data_type", DolDataType(self.data_type))
        object.__setattr__(self, "truncation", TruncationPolicy(self.truncation))

    def encode(self) -> bytes:
        return bytes.fromhex(self.tag) + bytes((self.length,))

    def to_evidence(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "length": self.length,
            "data_type": self.data_type.value,
            "truncation": self.truncation.value,
        }


def _parse_entries(raw: bytes, kind: DolKind, max_entries: int) -> tuple[DolEntry, ...]:
    entries: list[DolEntry] = []
    seen: set[str] = set()
    position = 0
    while position < len(raw):
        start = position
        first = raw[position]
        position += 1
        if first & 0x20:
            raise DolError("INVALID_DOL_TAG", "DOL solo admite tags primitivos.")
        if first & 0x1F == 0x1F:
            while True:
                if position >= len(raw):
                    raise DolError("DOL_TRUNCATED", "Tag DOL multibyte truncado.")
                continuation = raw[position]
                position += 1
                if position - start > 4:
                    raise DolError("DOL_LIMIT_EXCEEDED", "Tag DOL demasiado largo.")
                if not continuation & 0x80:
                    break
        if position >= len(raw):
            raise DolError("DOL_TRUNCATED", "Falta longitud DOL.")
        length = raw[position]
        position += 1
        try:
            tag = normalize_tag(raw[start:position - 1].hex())
        except ValueError as exc:
            raise DolError("INVALID_DOL_TAG", str(exc)) from exc
        if tag in seen:
            raise DolError("DUPLICATE_DOL_TAG", f"Tag DOL duplicado: {tag}.")
        seen.add(tag)
        entries.append(DolEntry(tag, length))
        if len(entries) > max_entries:
            raise DolError("DOL_LIMIT_EXCEEDED", "Demasiadas entradas DOL.")
    return tuple(entries)


def _numeric_value(value: bytes) -> None:
    if any((byte >> 4) > 9 or (byte & 0x0F) > 9 for byte in value):
        raise DolError("INVALID_NUMERIC_VALUE", "NUMERIC exige BCD con dígitos 0-9.")


def _text_value(value: bytes) -> None:
    if any(byte < 0x20 or byte > 0x7E for byte in value):
        raise DolError("INVALID_TEXT_VALUE", "TEXT exige ASCII visible.")


def _fit_value(entry: DolEntry, value: bytes) -> bytes:
    if len(value) > entry.length:
        if entry.truncation == TruncationPolicy.REJECT:
            raise DolError("DOL_VALUE_TOO_LONG", f"Valor demasiado largo para {entry.tag}.")
        if entry.data_type == DolDataType.NUMERIC:
            return value[-entry.length:] if entry.length else b""
        return value[:entry.length]
    if len(value) == entry.length:
        return value
    missing = entry.length - len(value)
    if entry.data_type == DolDataType.NUMERIC:
        return bytes(missing) + value
    if entry.data_type == DolDataType.TEXT:
        return value + b" " * missing
    return value + bytes(missing)


@dataclass(frozen=True)
class DolDocument:
    """DOL local TEST/SYNTHETIC con políticas explícitas.

    NUMERIC rellena a la izquierda con ceros y el truncado opt-in conserva los
    dígitos de la derecha. BINARY rellena a la derecha con ceros y TEXT con
    espacios; su truncado opt-in conserva el prefijo. Todo valor largo se rechaza
    por defecto y toda entrada es obligatoria, sin relleno implícito de ausentes.
    """

    kind: DolKind
    entries: tuple[DolEntry, ...]
    max_output: int = 252

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", DolKind(self.kind))
        object.__setattr__(self, "entries", tuple(self.entries))
        if not 1 <= self.max_output <= 255:
            raise DolError("INVALID_DOL_LIMIT", "max_output debe estar entre 1 y 255.")
        seen: set[str] = set()
        for entry in self.entries:
            if not isinstance(entry, DolEntry):
                raise DolError("INVALID_DOL_ENTRY", "entries debe contener DolEntry.")
            if entry.tag in seen:
                raise DolError("DUPLICATE_DOL_TAG", f"Tag DOL duplicado: {entry.tag}.")
            seen.add(entry.tag)
        if sum(entry.length for entry in self.entries) > self.max_output:
            raise DolError("DOL_LIMIT_EXCEEDED", "La salida DOL supera max_output.")

    @classmethod
    def parse(cls, raw: bytes, kind: DolKind, max_entries: int = 128, max_output: int = 252) -> "DolDocument":
        try:
            data = bytes(raw)
        except (TypeError, ValueError) as exc:
            raise DolError("INVALID_DOL", "DOL debe ser bytes.") from exc
        entries = _parse_entries(data, DolKind(kind), max_entries)
        return cls(kind, entries, max_output=max_output)

    def encode(self) -> bytes:
        return b"".join(entry.encode() for entry in self.entries)

    def build(self, store: EmvDataStore) -> bytes:
        output = bytearray()
        for entry in self.entries:
            try:
                value = store.require(entry.tag).value
            except DataStoreError as exc:
                if isinstance(exc, MandatoryDataError):
                    raise DolError("MISSING_MANDATORY_DATA", f"Dato DOL obligatorio ausente: {entry.tag}.") from exc
                raise
            if entry.data_type == DolDataType.NUMERIC:
                _numeric_value(value)
            elif entry.data_type == DolDataType.TEXT:
                _text_value(value)
            output.extend(_fit_value(entry, value))
        if len(output) > self.max_output:
            raise DolError("DOL_LIMIT_EXCEEDED", "La salida DOL supera max_output.")
        return bytes(output)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "entries": [entry.to_evidence() for entry in self.entries],
            "declared_output_length": sum(entry.length for entry in self.entries),
            "max_output": self.max_output,
            "mode": "LAB_ONLY",
        }


def parse_dol(raw: bytes, kind: DolKind, max_entries: int = 128, max_output: int = 252) -> DolDocument:
    return DolDocument.parse(raw, kind, max_entries=max_entries, max_output=max_output)


def build_dol(document: DolDocument, store: EmvDataStore) -> bytes:
    return document.build(store)


def dol_evidence(documents: Iterable[DolDocument]) -> list[dict[str, Any]]:
    return [document.to_evidence() for document in documents]
