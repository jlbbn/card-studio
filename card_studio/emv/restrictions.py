from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from card_studio.interfaces.base import InterfaceKind

from .data import DataSource, EmvDataStore, OwnershipDomain
from .status import Tsi, Tvr, TvrFlag


class RestrictionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _bcd(value: int) -> int:
    high, low = value >> 4, value & 0x0F
    if high > 9 or low > 9:
        raise RestrictionError("INVALID_BCD_DATE", "La fecha contiene nibbles no decimales.")
    return high * 10 + low


@dataclass(frozen=True, order=True)
class EmvDate:
    year: int
    month: int
    day: int

    def __post_init__(self) -> None:
        for name in ("year", "month", "day"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 99:
                raise RestrictionError("INVALID_BCD_DATE", f"{name} fuera de dominio BCD.")
        maximum = {
            1: 31, 2: 29 if self.year % 4 == 0 else 28, 3: 31, 4: 30,
            5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
        }.get(self.month)
        if maximum is None or self.day == 0 or self.day > maximum:
            raise RestrictionError("INVALID_BCD_DATE", "Fecha de calendario inválida.")

    @classmethod
    def parse(cls, raw: bytes) -> "EmvDate":
        data = bytes(raw)
        if len(data) != 3:
            raise RestrictionError("INVALID_BCD_DATE", "La fecha debe ocupar tres bytes.")
        return cls(_bcd(data[0]), _bcd(data[1]), _bcd(data[2]))

    def to_bytes(self) -> bytes:
        def encode(value: int) -> int:
            return ((value // 10) << 4) | (value % 10)
        return bytes((encode(self.year), encode(self.month), encode(self.day)))


class TransactionRegion(str, Enum):
    DOMESTIC = "DOMESTIC"
    INTERNATIONAL = "INTERNATIONAL"


class ServiceType(str, Enum):
    CASH = "CASH"
    GOODS = "GOODS"
    SERVICES = "SERVICES"
    CASHBACK = "CASHBACK"


class TerminalChannel(str, Enum):
    ATM = "ATM"
    OTHER_THAN_ATM = "OTHER_THAN_ATM"


@dataclass(frozen=True)
class RestrictionPolicy:
    required_tags: tuple[str, ...] = ("9F08", "5F25", "5F24", "9F07")
    allow_new_card: bool = True


@dataclass(frozen=True)
class RestrictionInput:
    terminal_date: EmvDate
    terminal_application_version: bytes
    interface: InterfaceKind
    supported_interfaces: tuple[InterfaceKind, ...]
    region: TransactionRegion = TransactionRegion.DOMESTIC
    service: ServiceType = ServiceType.GOODS
    channel: TerminalChannel = TerminalChannel.OTHER_THAN_ATM
    new_card: bool = False


@dataclass(frozen=True)
class ApplicationUsageControl:
    raw: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", bytes(self.raw))
        if len(self.raw) != 2:
            raise RestrictionError("INVALID_AUC_LENGTH", "AUC debe ocupar dos bytes.")
        if self.raw[1] & 0x3F:
            raise RestrictionError("AUC_RFU_SET", "AUC contiene bits RFU fuera del alcance LAB_ONLY.")

    def allows(self, region: TransactionRegion, service: ServiceType, channel: TerminalChannel) -> bool:
        channel_mask = 0x02 if channel == TerminalChannel.ATM else 0x01
        service_masks = {
            (TransactionRegion.DOMESTIC, ServiceType.CASH): 0x80,
            (TransactionRegion.INTERNATIONAL, ServiceType.CASH): 0x40,
            (TransactionRegion.DOMESTIC, ServiceType.GOODS): 0x20,
            (TransactionRegion.INTERNATIONAL, ServiceType.GOODS): 0x10,
            (TransactionRegion.DOMESTIC, ServiceType.SERVICES): 0x08,
            (TransactionRegion.INTERNATIONAL, ServiceType.SERVICES): 0x04,
        }
        if service == ServiceType.CASHBACK:
            service_mask = 0x80 if region == TransactionRegion.DOMESTIC else 0x40
            return bool(self.raw[1] & service_mask and self.raw[0] & channel_mask)
        return bool(self.raw[0] & service_masks[(region, service)] and self.raw[0] & channel_mask)

    def to_evidence(self) -> dict[str, Any]:
        return {"length": len(self.raw), "rfu_valid": True}


@dataclass(frozen=True)
class RestrictionResult:
    tvr: Tvr
    tsi: Tsi
    failed_checks: tuple[str, ...]
    auc: ApplicationUsageControl

    @property
    def functional_restriction(self) -> bool:
        return bool(self.failed_checks)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "functional_restriction": self.functional_restriction,
            "failed_checks": list(self.failed_checks),
            "tvr": self.tvr.to_evidence(),
            "tsi": self.tsi.to_evidence(),
        }


def _card_value(store: EmvDataStore, tag: str) -> bytes:
    record = store.require(tag)
    if record.source != DataSource.CARD or record.ownership != OwnershipDomain.CARD:
        raise RestrictionError("INVALID_CARD_PROVENANCE", f"El dato {tag} no pertenece a CARD.")
    return record.value


def evaluate_processing_restrictions(
    store: EmvDataStore,
    input: RestrictionInput,
    policy: RestrictionPolicy = RestrictionPolicy(),
    tvr: Tvr | None = None,
    tsi: Tsi | None = None,
) -> RestrictionResult:
    if input.interface not in input.supported_interfaces:
        raise RestrictionError("UNSUPPORTED_INTERFACE", "La interfaz no es compatible con el perfil.")
    if len(input.terminal_application_version) != 2:
        raise RestrictionError("INVALID_APPLICATION_VERSION", "La versión de terminal debe ocupar dos bytes.")
    for tag in policy.required_tags:
        _card_value(store, tag)
    card_version = _card_value(store, "9F08")
    if len(card_version) != 2:
        raise RestrictionError("INVALID_APPLICATION_VERSION", "La versión CARD debe ocupar dos bytes.")
    expiration = EmvDate.parse(_card_value(store, "5F24"))
    effective = EmvDate.parse(_card_value(store, "5F25"))
    auc = ApplicationUsageControl(_card_value(store, "9F07"))
    next_tvr = Tvr.parse(tvr.to_bytes()) if tvr is not None else Tvr.cleared()
    next_tsi = Tsi.parse(tsi.to_bytes()) if tsi is not None else Tsi.cleared()
    failed: list[str] = []
    if card_version != input.terminal_application_version:
        next_tvr.set(TvrFlag.DIFFERENT_APPLICATION_VERSIONS)
        failed.append(TvrFlag.DIFFERENT_APPLICATION_VERSIONS.value)
    if input.terminal_date > expiration:
        next_tvr.set(TvrFlag.EXPIRED_APPLICATION)
        failed.append(TvrFlag.EXPIRED_APPLICATION.value)
    if input.terminal_date < effective:
        next_tvr.set(TvrFlag.APPLICATION_NOT_YET_EFFECTIVE)
        failed.append(TvrFlag.APPLICATION_NOT_YET_EFFECTIVE.value)
    if not auc.allows(input.region, input.service, input.channel):
        next_tvr.set(TvrFlag.REQUESTED_SERVICE_NOT_ALLOWED)
        failed.append(TvrFlag.REQUESTED_SERVICE_NOT_ALLOWED.value)
    if input.new_card and not policy.allow_new_card:
        next_tvr.set(TvrFlag.NEW_CARD)
        failed.append(TvrFlag.NEW_CARD.value)
    elif input.new_card:
        next_tvr.set(TvrFlag.NEW_CARD)
    return RestrictionResult(next_tvr, next_tsi, tuple(failed), auc)
