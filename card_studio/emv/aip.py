from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class AipError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AipCapability(str, Enum):
    SDA_SUPPORTED = "SDA_SUPPORTED"
    DDA_SUPPORTED = "DDA_SUPPORTED"
    CARDHOLDER_VERIFICATION_SUPPORTED = "CARDHOLDER_VERIFICATION_SUPPORTED"
    ISSUER_AUTHENTICATION_SUPPORTED = "ISSUER_AUTHENTICATION_SUPPORTED"
    CDA_SUPPORTED = "CDA_SUPPORTED"


_CAPABILITY_BITS = {
    AipCapability.SDA_SUPPORTED: (0, 0x80),
    AipCapability.DDA_SUPPORTED: (0, 0x40),
    AipCapability.CARDHOLDER_VERIFICATION_SUPPORTED: (0, 0x20),
    AipCapability.ISSUER_AUTHENTICATION_SUPPORTED: (0, 0x10),
    AipCapability.CDA_SUPPORTED: (1, 0x80),
}
_ALLOWED_MASKS = (0xFC, 0x80)


@dataclass(frozen=True)
class ApplicationInterchangeProfile:
    raw: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", bytes(self.raw))
        if len(self.raw) != 2:
            raise AipError("INVALID_AIP_LENGTH", "AIP debe ocupar exactamente dos bytes.")
        if any(byte & ~allowed for byte, allowed in zip(self.raw, _ALLOWED_MASKS)):
            raise AipError("AIP_RFU_SET", "AIP contiene bits RFU fuera del alcance LAB_ONLY.")

    @classmethod
    def parse(cls, raw: bytes) -> "ApplicationInterchangeProfile":
        return cls(raw)

    def supports(self, capability: AipCapability) -> bool:
        selected = AipCapability(capability)
        index, mask = _CAPABILITY_BITS[selected]
        return bool(self.raw[index] & mask)

    @property
    def oda_required(self) -> bool:
        return any(self.supports(item) for item in (
            AipCapability.SDA_SUPPORTED,
            AipCapability.DDA_SUPPORTED,
            AipCapability.CDA_SUPPORTED,
        ))

    def to_bytes(self) -> bytes:
        return self.raw

    def to_evidence(self) -> dict[str, Any]:
        return {
            "length": len(self.raw),
            "capabilities": [item.value for item in AipCapability if self.supports(item)],
            "rfu_valid": True,
            "mode": "LAB_ONLY",
        }
