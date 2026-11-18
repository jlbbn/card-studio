from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from card_studio.transactions.model import FunctionalResult

from .data import DataSensitivity, DataSource, OwnershipDomain


class CryptogramError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CryptogramType(str, Enum):
    AAC = "AAC"
    TC = "TC"
    ARQC = "ARQC"


class AcEvidencePolicy(str, Enum):
    SUPPRESS = "SUPPRESS"
    DIGEST = "DIGEST"


@dataclass(frozen=True)
class Cid:
    raw: int

    def __post_init__(self) -> None:
        if not isinstance(self.raw, int) or isinstance(self.raw, bool) or not 0 <= self.raw <= 255:
            raise CryptogramError("INVALID_CID", "CID debe ser un byte.")

    @property
    def cryptogram_type(self) -> CryptogramType:
        value = self.raw & 0xC0
        if value == 0x00:
            return CryptogramType.AAC
        if value == 0x40:
            return CryptogramType.TC
        if value == 0x80:
            return CryptogramType.ARQC
        raise CryptogramError("UNSUPPORTED_CRYPTOGRAM_TYPE", "AAR y otros tipos no están permitidos en LAB_ONLY.")

    @property
    def advice_required(self) -> bool:
        return bool(self.raw & 0x08)

    @property
    def reason_advice_code(self) -> int:
        return self.raw & 0x07

    def to_evidence(self) -> dict[str, Any]:
        return {
            "cryptogram_type": self.cryptogram_type.value,
            "advice_required": self.advice_required,
            "reason_advice_code": self.reason_advice_code,
        }


@dataclass(frozen=True)
class CryptogramResult:
    cid: Cid
    application_cryptogram: bytes
    atc: bytes
    issuer_application_data: bytes = b""
    source: DataSource = DataSource.CARD
    ownership: OwnershipDomain = OwnershipDomain.KERNEL

    def __post_init__(self) -> None:
        object.__setattr__(self, "application_cryptogram", bytes(self.application_cryptogram))
        object.__setattr__(self, "atc", bytes(self.atc))
        object.__setattr__(self, "issuer_application_data", bytes(self.issuer_application_data))
        if len(self.application_cryptogram) != 8:
            raise CryptogramError("INVALID_AC_LENGTH", "Application Cryptogram debe ocupar ocho bytes.")
        if len(self.atc) != 2:
            raise CryptogramError("INVALID_ATC_LENGTH", "ATC debe ocupar dos bytes.")
        if len(self.issuer_application_data) > 32:
            raise CryptogramError("INVALID_IAD_LENGTH", "IAD supera 32 bytes.")
        object.__setattr__(self, "source", DataSource(self.source))
        object.__setattr__(self, "ownership", OwnershipDomain(self.ownership))
        _ = self.cid.cryptogram_type

    @property
    def cryptogram_type(self) -> CryptogramType:
        return self.cid.cryptogram_type

    @property
    def functional_result(self) -> FunctionalResult:
        return {
            CryptogramType.AAC: FunctionalResult.DECLINED,
            CryptogramType.TC: FunctionalResult.APPROVED,
            CryptogramType.ARQC: FunctionalResult.ONLINE_REQUIRED,
        }[self.cryptogram_type]

    @property
    def sensitivity(self) -> DataSensitivity:
        return DataSensitivity.SECRET

    def to_evidence(self, policy: AcEvidencePolicy = AcEvidencePolicy.SUPPRESS) -> dict[str, Any]:
        selected = AcEvidencePolicy(policy)
        evidence: dict[str, Any] = {
            "cryptogram_type": self.cryptogram_type.value,
            "functional_result": self.functional_result.value,
            "cid": self.cid.to_evidence(),
            "atc": self.atc.hex().upper(),
            "ac_length": len(self.application_cryptogram),
            "iad_length": len(self.issuer_application_data),
            "iad_sensitivity": DataSensitivity.SENSITIVE.value,
            "ac_sensitivity": self.sensitivity.value,
            "source": self.source.value,
            "ownership": self.ownership.value,
            "mode": "LAB_ONLY",
        }
        if selected == AcEvidencePolicy.DIGEST:
            evidence["ac_sha256"] = hashlib.sha256(self.application_cryptogram).hexdigest()
        return evidence
