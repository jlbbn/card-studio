from __future__ import annotations

from enum import Enum
from typing import Any


class StatusBitmapError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TvrFlag(str, Enum):
    OFFLINE_DATA_AUTH_NOT_PERFORMED = "OFFLINE_DATA_AUTH_NOT_PERFORMED"
    SDA_FAILED = "SDA_FAILED"
    ICC_DATA_MISSING = "ICC_DATA_MISSING"
    CARD_ON_EXCEPTION_FILE = "CARD_ON_EXCEPTION_FILE"
    DDA_FAILED = "DDA_FAILED"
    CDA_FAILED = "CDA_FAILED"
    DIFFERENT_APPLICATION_VERSIONS = "DIFFERENT_APPLICATION_VERSIONS"
    EXPIRED_APPLICATION = "EXPIRED_APPLICATION"
    APPLICATION_NOT_YET_EFFECTIVE = "APPLICATION_NOT_YET_EFFECTIVE"
    REQUESTED_SERVICE_NOT_ALLOWED = "REQUESTED_SERVICE_NOT_ALLOWED"
    NEW_CARD = "NEW_CARD"
    CARDHOLDER_VERIFICATION_NOT_SUCCESSFUL = "CARDHOLDER_VERIFICATION_NOT_SUCCESSFUL"
    UNRECOGNIZED_CVM = "UNRECOGNIZED_CVM"
    PIN_TRY_LIMIT_EXCEEDED = "PIN_TRY_LIMIT_EXCEEDED"
    PIN_PAD_NOT_PRESENT_OR_NOT_WORKING = "PIN_PAD_NOT_PRESENT_OR_NOT_WORKING"
    PIN_NOT_ENTERED = "PIN_NOT_ENTERED"
    ONLINE_PIN_ENTERED = "ONLINE_PIN_ENTERED"
    FLOOR_LIMIT_EXCEEDED = "FLOOR_LIMIT_EXCEEDED"
    LOWER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED = "LOWER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED"
    UPPER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED = "UPPER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED"
    RANDOM_TRANSACTION_SELECTION_PERFORMED = "RANDOM_TRANSACTION_SELECTION_PERFORMED"
    MERCHANT_FORCED_TRANSACTION_ONLINE = "MERCHANT_FORCED_TRANSACTION_ONLINE"
    DEFAULT_TDOL_USED = "DEFAULT_TDOL_USED"
    ISSUER_AUTHENTICATION_FAILED = "ISSUER_AUTHENTICATION_FAILED"
    SCRIPT_PROCESSING_FAILED_BEFORE_FINAL_GAC = "SCRIPT_PROCESSING_FAILED_BEFORE_FINAL_GAC"
    SCRIPT_PROCESSING_FAILED_AFTER_FINAL_GAC = "SCRIPT_PROCESSING_FAILED_AFTER_FINAL_GAC"


class TsiFlag(str, Enum):
    OFFLINE_DATA_AUTHENTICATION_PERFORMED = "OFFLINE_DATA_AUTHENTICATION_PERFORMED"
    CARDHOLDER_VERIFICATION_PERFORMED = "CARDHOLDER_VERIFICATION_PERFORMED"
    CARD_RISK_MANAGEMENT_PERFORMED = "CARD_RISK_MANAGEMENT_PERFORMED"
    ISSUER_AUTHENTICATION_PERFORMED = "ISSUER_AUTHENTICATION_PERFORMED"
    TERMINAL_RISK_MANAGEMENT_PERFORMED = "TERMINAL_RISK_MANAGEMENT_PERFORMED"
    SCRIPT_PROCESSING_PERFORMED = "SCRIPT_PROCESSING_PERFORMED"


_TVR_BITS = {
    TvrFlag.OFFLINE_DATA_AUTH_NOT_PERFORMED: (0, 0x80),
    TvrFlag.SDA_FAILED: (0, 0x40),
    TvrFlag.ICC_DATA_MISSING: (0, 0x20),
    TvrFlag.CARD_ON_EXCEPTION_FILE: (0, 0x10),
    TvrFlag.DDA_FAILED: (0, 0x08),
    TvrFlag.CDA_FAILED: (0, 0x04),
    TvrFlag.DIFFERENT_APPLICATION_VERSIONS: (1, 0x80),
    TvrFlag.EXPIRED_APPLICATION: (1, 0x40),
    TvrFlag.APPLICATION_NOT_YET_EFFECTIVE: (1, 0x20),
    TvrFlag.REQUESTED_SERVICE_NOT_ALLOWED: (1, 0x10),
    TvrFlag.NEW_CARD: (1, 0x08),
    TvrFlag.CARDHOLDER_VERIFICATION_NOT_SUCCESSFUL: (2, 0x80),
    TvrFlag.UNRECOGNIZED_CVM: (2, 0x40),
    TvrFlag.PIN_TRY_LIMIT_EXCEEDED: (2, 0x20),
    TvrFlag.PIN_PAD_NOT_PRESENT_OR_NOT_WORKING: (2, 0x10),
    TvrFlag.PIN_NOT_ENTERED: (2, 0x08),
    TvrFlag.ONLINE_PIN_ENTERED: (2, 0x04),
    TvrFlag.FLOOR_LIMIT_EXCEEDED: (3, 0x80),
    TvrFlag.LOWER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED: (3, 0x40),
    TvrFlag.UPPER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED: (3, 0x20),
    TvrFlag.RANDOM_TRANSACTION_SELECTION_PERFORMED: (3, 0x10),
    TvrFlag.MERCHANT_FORCED_TRANSACTION_ONLINE: (3, 0x08),
    TvrFlag.DEFAULT_TDOL_USED: (4, 0x80),
    TvrFlag.ISSUER_AUTHENTICATION_FAILED: (4, 0x40),
    TvrFlag.SCRIPT_PROCESSING_FAILED_BEFORE_FINAL_GAC: (4, 0x20),
    TvrFlag.SCRIPT_PROCESSING_FAILED_AFTER_FINAL_GAC: (4, 0x10),
}
_TSI_BITS = {
    TsiFlag.OFFLINE_DATA_AUTHENTICATION_PERFORMED: (0, 0x80),
    TsiFlag.CARDHOLDER_VERIFICATION_PERFORMED: (0, 0x40),
    TsiFlag.CARD_RISK_MANAGEMENT_PERFORMED: (0, 0x20),
    TsiFlag.ISSUER_AUTHENTICATION_PERFORMED: (0, 0x10),
    TsiFlag.TERMINAL_RISK_MANAGEMENT_PERFORMED: (0, 0x08),
    TsiFlag.SCRIPT_PROCESSING_PERFORMED: (0, 0x04),
}
_TVR_ALLOWED = (0xFC, 0xF8, 0xFC, 0xF8, 0xF0)
_TSI_ALLOWED = (0xFC, 0x00)


class _Bitmap:
    SIZE = 0
    ALLOWED = ()
    FLAG_TYPE = None
    BITS = {}

    def __init__(self, raw: bytes | None = None):
        selected = bytes(self.SIZE) if raw is None else bytes(raw)
        if len(selected) != self.SIZE:
            raise StatusBitmapError("INVALID_BITMAP_LENGTH", f"Bitmap debe ocupar {self.SIZE} bytes.")
        if any(byte & ~allowed for byte, allowed in zip(selected, self.ALLOWED)):
            raise StatusBitmapError("BITMAP_RFU_SET", "Bitmap contiene bits RFU.")
        self._bytes = bytearray(selected)

    @classmethod
    def parse(cls, raw: bytes):
        return cls(raw)

    def to_bytes(self) -> bytes:
        return bytes(self._bytes)

    def reset(self) -> None:
        self._bytes[:] = bytes(self.SIZE)

    def _set(self, flag, value: bool) -> None:
        selected = self.FLAG_TYPE(flag)
        index, mask = self.BITS[selected]
        if value:
            self._bytes[index] |= mask & self.ALLOWED[index]
        else:
            self._bytes[index] &= ~mask

    def is_set(self, flag) -> bool:
        selected = self.FLAG_TYPE(flag)
        index, mask = self.BITS[selected]
        return bool(self._bytes[index] & mask)

    def active_flags(self) -> list[str]:
        return [flag.value for flag in self.FLAG_TYPE if self.is_set(flag)]


class Tvr(_Bitmap):
    SIZE = 5
    ALLOWED = _TVR_ALLOWED
    FLAG_TYPE = TvrFlag
    BITS = _TVR_BITS

    @classmethod
    def cleared(cls) -> "Tvr":
        return cls()

    def set(self, flag: TvrFlag) -> None:
        self._set(flag, True)

    def clear(self, flag: TvrFlag) -> None:
        self._set(flag, False)

    def to_evidence(self) -> dict[str, Any]:
        return {"length": self.SIZE, "active_flags": self.active_flags(), "rfu_valid": True}


class Tsi(_Bitmap):
    SIZE = 2
    ALLOWED = _TSI_ALLOWED
    FLAG_TYPE = TsiFlag
    BITS = _TSI_BITS

    @classmethod
    def cleared(cls) -> "Tsi":
        return cls()

    def mark_processed(self, flag: TsiFlag) -> None:
        self._set(flag, True)

    def to_evidence(self) -> dict[str, Any]:
        return {"length": self.SIZE, "processed": self.active_flags(), "rfu_valid": True}
