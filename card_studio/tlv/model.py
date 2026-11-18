from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TlvLimits:
    max_bytes: int = 4096
    max_nodes: int = 256
    max_depth: int = 8

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_nodes", "max_depth"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} debe ser un entero positivo.")


@dataclass(frozen=True)
class TlvNode:
    tag: str
    value: bytes = b""
    children: tuple["TlvNode", ...] = field(default_factory=tuple)
    offset: int = field(default=0, compare=False)
    depth: int = field(default=1, compare=False)
    context: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        normalized = self.tag.upper()
        if not re.fullmatch(r"(?:[0-9A-F]{2})+", normalized):
            raise ValueError("El tag TLV debe contener bytes hexadecimales.")
        raw = bytes.fromhex(normalized)
        if len(raw) > 4 or raw[0] in {0x00, 0xFF}:
            raise ValueError("El tag TLV contiene un byte inicial inválido o supera cuatro bytes.")
        first = raw[0]
        multibyte = first & 0x1F == 0x1F
        if multibyte:
            if len(raw) == 1 or raw[1] & 0x7F == 0 or raw[-1] & 0x80:
                raise ValueError("El tag TLV multibyte está incompleto o no es canónico.")
            if any(not byte & 0x80 for byte in raw[1:-1]):
                raise ValueError("El tag TLV multibyte tiene una continuación inválida.")
        elif len(raw) != 1:
            raise ValueError("El tag TLV de un byte no es válido.")
        if self.context is not None and (
            not isinstance(self.context, str)
            or not self.context
            or len(self.context) > 128
            or any(ord(char) < 0x20 for char in self.context)
        ):
            raise ValueError("El contexto TLV debe ser texto visible de hasta 128 caracteres.")
        object.__setattr__(self, "tag", normalized)
        object.__setattr__(self, "value", bytes(self.value))
        object.__setattr__(self, "children", tuple(self.children))
        if self.children and self.value:
            raise ValueError("Un template TLV no puede mezclar valor y nodos hijos.")
        if self.children and not self.constructed:
            raise ValueError("Un tag primitivo no puede contener nodos hijos.")

    @property
    def constructed(self) -> bool:
        return bool(int(self.tag[:2], 16) & 0x20)

    @property
    def known(self) -> bool:
        return self.tag in KNOWN_LAB_TAGS


KNOWN_LAB_TAGS = frozenset({
    "4F",
    "50",
    "5F2A",
    "70",
    "77",
    "82",
    "9F02",
    "9F36",
    "9F38",
    "DF01",
    "DF02",
    "DF04",
    "DF10",
    "DF11",
    "DF12",
    "DF13",
})
