from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .data import DataSource, EmvDataStore, OwnershipDomain
from .status import Tsi, TsiFlag, Tvr, TvrFlag


MAX_CVM_RULES = 64


class CvmError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CvmMethod(str, Enum):
    OFFLINE_PLAINTEXT_PIN = "OFFLINE_PLAINTEXT_PIN"
    ONLINE_PIN = "ONLINE_PIN"
    OFFLINE_PLAINTEXT_PIN_AND_SIGNATURE = "OFFLINE_PLAINTEXT_PIN_AND_SIGNATURE"
    OFFLINE_ENCIPHERED_PIN = "OFFLINE_ENCIPHERED_PIN"
    OFFLINE_ENCIPHERED_PIN_AND_SIGNATURE = "OFFLINE_ENCIPHERED_PIN_AND_SIGNATURE"
    SIGNATURE = "SIGNATURE"
    FAIL_CVM = "FAIL_CVM"
    NO_CVM = "NO_CVM"
    SCHEME_SPECIFIC = "SCHEME_SPECIFIC"
    UNKNOWN = "UNKNOWN"


class CvmOutcome(str, Enum):
    NO_CVM = "NO_CVM"
    SIGNATURE = "SIGNATURE"
    ONLINE_PIN_REQUIRED = "ONLINE_PIN_REQUIRED"
    OFFLINE_PIN_REQUIRED = "OFFLINE_PIN_REQUIRED"
    CDCVM = "CDCVM"
    CVM_FAILED = "CVM_FAILED"
    NOT_SUPPORTED = "NOT_SUPPORTED"


class CvmTransactionType(str, Enum):
    NON_CASH = "NON_CASH"
    UNATTENDED_CASH = "UNATTENDED_CASH"
    MANUAL_CASH = "MANUAL_CASH"
    PURCHASE_WITH_CASHBACK = "PURCHASE_WITH_CASHBACK"


_METHOD_CODES = {
    0x01: CvmMethod.OFFLINE_PLAINTEXT_PIN,
    0x02: CvmMethod.ONLINE_PIN,
    0x03: CvmMethod.OFFLINE_PLAINTEXT_PIN_AND_SIGNATURE,
    0x04: CvmMethod.OFFLINE_ENCIPHERED_PIN,
    0x05: CvmMethod.OFFLINE_ENCIPHERED_PIN_AND_SIGNATURE,
    0x06: CvmMethod.SIGNATURE,
    0x1E: CvmMethod.FAIL_CVM,
    0x1F: CvmMethod.NO_CVM,
}


@dataclass(frozen=True)
class CvmRule:
    raw_code: int
    condition_code: int

    def __post_init__(self) -> None:
        for name in ("raw_code", "condition_code"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255:
                raise CvmError("INVALID_CVM_RULE", "Regla CVM fuera de dominio byte.")

    @property
    def method(self) -> CvmMethod:
        code = self.raw_code & 0x3F
        if 0x20 <= code <= 0x3F:
            return CvmMethod.SCHEME_SPECIFIC
        return _METHOD_CODES.get(code, CvmMethod.UNKNOWN)

    @property
    def continue_on_failure(self) -> bool:
        return bool(self.raw_code & 0x40)


@dataclass(frozen=True)
class CvmList:
    amount_x: int
    amount_y: int
    rules: tuple[CvmRule, ...]

    @classmethod
    def parse(cls, raw: bytes) -> "CvmList":
        data = bytes(raw)
        if len(data) < 8 or (len(data) - 8) % 2:
            raise CvmError("MALFORMED_CVM_LIST", "CVM List requiere X/Y y pares regla/condición.")
        if len(data) == 8:
            raise CvmError("EMPTY_CVM_LIST", "CVM List no contiene reglas.")
        if (len(data) - 8) // 2 > MAX_CVM_RULES:
            raise CvmError("CVM_RULE_LIMIT_EXCEEDED", "CVM List supera 64 reglas.")
        rules = tuple(CvmRule(data[offset], data[offset + 1]) for offset in range(8, len(data), 2))
        return cls(
            int.from_bytes(data[:4], "big"),
            int.from_bytes(data[4:8], "big"),
            rules,
        )

    def encode(self) -> bytes:
        return self.amount_x.to_bytes(4, "big") + self.amount_y.to_bytes(4, "big") + b"".join(
            bytes((rule.raw_code, rule.condition_code)) for rule in self.rules
        )


@dataclass(frozen=True)
class ExternalPinHandle:
    reference: str

    def __post_init__(self) -> None:
        if not self.reference.startswith("test://") or len(self.reference) > 128 or any(ord(char) < 0x20 for char in self.reference):
            raise CvmError("INVALID_PIN_HANDLE", "El handle PIN debe ser una referencia test:// externa.")

    def to_evidence(self) -> dict[str, Any]:
        return {"external_handle_present": True, "synthetic": True}


@dataclass(frozen=True)
class PinHandles:
    offline_plaintext: ExternalPinHandle | None = None
    offline_enciphered: ExternalPinHandle | None = None


@dataclass(frozen=True)
class CvmCapabilities:
    offline_pin: bool = False
    online_pin: bool = False
    signature: bool = False
    cdcvm: bool = False

    @property
    def any_method(self) -> bool:
        return self.offline_pin or self.online_pin or self.signature or self.cdcvm


@dataclass(frozen=True)
class CvmContext:
    amount: int
    transaction_type: CvmTransactionType
    capabilities: CvmCapabilities
    handles: PinHandles = PinHandles()
    transaction_currency_matches_application: bool = True
    cdcvm_performed: bool = False
    online_pin_entered: bool = False


@dataclass(frozen=True)
class CvmEvaluation:
    outcome: CvmOutcome
    cvm_results: bytes
    tvr: Tvr
    tsi: Tsi
    rule: CvmRule | None
    signature_required: bool = False

    def __post_init__(self) -> None:
        if len(self.cvm_results) != 3:
            raise CvmError("INVALID_CVM_RESULTS", "CVM Results debe ocupar tres bytes.")

    def to_evidence(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "cvm_results_length": len(self.cvm_results),
            "cvm_results": list(self.cvm_results),
            "signature_required": self.signature_required,
            "pin_captured": False,
            "tvr": self.tvr.to_evidence(),
            "tsi": self.tsi.to_evidence(),
        }


def _condition_matches(rule: CvmRule, values: CvmList, context: CvmContext) -> bool:
    code = rule.condition_code
    if code == 0x00:
        return True
    if code == 0x01:
        return context.transaction_type == CvmTransactionType.UNATTENDED_CASH
    if code == 0x02:
        return context.transaction_type == CvmTransactionType.NON_CASH
    if code == 0x03:
        return context.capabilities.any_method
    if code == 0x04:
        return context.transaction_type == CvmTransactionType.MANUAL_CASH
    if code == 0x05:
        return context.transaction_type == CvmTransactionType.PURCHASE_WITH_CASHBACK
    if code in {0x06, 0x07, 0x08, 0x09}:
        if not context.transaction_currency_matches_application:
            return False
        threshold = values.amount_x if code in {0x06, 0x07} else values.amount_y
        if code in {0x06, 0x08}:
            return context.amount <= threshold
        return context.amount > threshold
    raise CvmError("INVALID_CVM_CONDITION", f"Condition code desconocido: {code:02X}.")


def _offline_handle(method: CvmMethod, handles: PinHandles) -> ExternalPinHandle | None:
    if method in {CvmMethod.OFFLINE_PLAINTEXT_PIN, CvmMethod.OFFLINE_PLAINTEXT_PIN_AND_SIGNATURE}:
        return handles.offline_plaintext
    return handles.offline_enciphered


def _selected_outcome(method: CvmMethod, context: CvmContext) -> tuple[CvmOutcome, bool, str | None]:
    if method == CvmMethod.NO_CVM:
        return CvmOutcome.NO_CVM, False, None
    if method == CvmMethod.SIGNATURE:
        return (CvmOutcome.SIGNATURE, True, None) if context.capabilities.signature else (CvmOutcome.NOT_SUPPORTED, False, "SIGNATURE")
    if method == CvmMethod.ONLINE_PIN:
        return (CvmOutcome.ONLINE_PIN_REQUIRED, False, None) if context.capabilities.online_pin else (CvmOutcome.NOT_SUPPORTED, False, "ONLINE_PIN")
    if method in {
        CvmMethod.OFFLINE_PLAINTEXT_PIN,
        CvmMethod.OFFLINE_PLAINTEXT_PIN_AND_SIGNATURE,
        CvmMethod.OFFLINE_ENCIPHERED_PIN,
        CvmMethod.OFFLINE_ENCIPHERED_PIN_AND_SIGNATURE,
    }:
        if not context.capabilities.offline_pin:
            return CvmOutcome.NOT_SUPPORTED, False, "OFFLINE_PIN"
        if _offline_handle(method, context.handles) is None:
            return CvmOutcome.NOT_SUPPORTED, False, "PIN_HANDLE"
        combined = method in {CvmMethod.OFFLINE_PLAINTEXT_PIN_AND_SIGNATURE, CvmMethod.OFFLINE_ENCIPHERED_PIN_AND_SIGNATURE}
        if combined and not context.capabilities.signature:
            return CvmOutcome.NOT_SUPPORTED, False, "SIGNATURE"
        return CvmOutcome.OFFLINE_PIN_REQUIRED, combined, None
    if method == CvmMethod.SCHEME_SPECIFIC:
        if context.capabilities.cdcvm and context.cdcvm_performed:
            return CvmOutcome.CDCVM, False, None
        return CvmOutcome.NOT_SUPPORTED, False, "CDCVM"
    return CvmOutcome.NOT_SUPPORTED, False, "METHOD"


def evaluate_cvm(
    store: EmvDataStore,
    context: CvmContext,
    tvr: Tvr | None = None,
    tsi: Tsi | None = None,
) -> CvmEvaluation:
    cvm_record = store.require("8E")
    if cvm_record.source != DataSource.CARD or cvm_record.ownership != OwnershipDomain.CARD:
        raise CvmError("INVALID_CARD_PROVENANCE", "CVM List no pertenece a CARD.")
    values = CvmList.parse(cvm_record.value)
    if not isinstance(context.amount, int) or isinstance(context.amount, bool) or context.amount < 0:
        raise CvmError("INVALID_CVM_AMOUNT", "El importe CVM debe ser no negativo.")
    for rule in values.rules:
        if rule.condition_code > 0x09:
            raise CvmError("INVALID_CVM_CONDITION", f"Condition code desconocido: {rule.condition_code:02X}.")
    next_tvr = Tvr.parse(tvr.to_bytes()) if tvr is not None else Tvr.cleared()
    next_tsi = Tsi.parse(tsi.to_bytes()) if tsi is not None else Tsi.cleared()
    selected_rule: CvmRule | None = None
    selected_outcome = CvmOutcome.NOT_SUPPORTED
    signature_required = False
    unavailable: str | None = None
    explicit_failure = False
    for rule in values.rules:
        if not _condition_matches(rule, values, context):
            continue
        if rule.method == CvmMethod.FAIL_CVM:
            selected_rule = rule
            selected_outcome = CvmOutcome.CVM_FAILED
            explicit_failure = True
            break
        outcome, signature_required, reason = _selected_outcome(rule.method, context)
        if outcome != CvmOutcome.NOT_SUPPORTED:
            selected_rule = rule
            selected_outcome = outcome
            break
        next_tvr.set(TvrFlag.UNRECOGNIZED_CVM)
        unavailable = reason
        if not rule.continue_on_failure:
            selected_rule = rule
            break
        selected_rule = None
    if selected_rule is None and unavailable is None:
        next_tvr.set(TvrFlag.UNRECOGNIZED_CVM)
    if selected_outcome in {CvmOutcome.CVM_FAILED, CvmOutcome.NOT_SUPPORTED}:
        next_tvr.set(TvrFlag.CARDHOLDER_VERIFICATION_NOT_SUCCESSFUL)
        if unavailable in {"ONLINE_PIN", "OFFLINE_PIN"}:
            next_tvr.set(TvrFlag.PIN_PAD_NOT_PRESENT_OR_NOT_WORKING)
        elif unavailable == "PIN_HANDLE":
            next_tvr.set(TvrFlag.PIN_NOT_ENTERED)
    if selected_outcome == CvmOutcome.ONLINE_PIN_REQUIRED and context.online_pin_entered:
        next_tvr.set(TvrFlag.ONLINE_PIN_ENTERED)
    if selected_rule is None:
        cvm_results = bytes((0x3F, 0x00, 0x01))
    else:
        status = 0x02 if selected_outcome not in {CvmOutcome.CVM_FAILED, CvmOutcome.NOT_SUPPORTED} else 0x01
        cvm_results = bytes((selected_rule.raw_code, selected_rule.condition_code, status))
    if explicit_failure:
        next_tvr.set(TvrFlag.CARDHOLDER_VERIFICATION_NOT_SUCCESSFUL)
    next_tsi.mark_processed(TsiFlag.CARDHOLDER_VERIFICATION_PERFORMED)
    return CvmEvaluation(selected_outcome, cvm_results, next_tvr, next_tsi, selected_rule, signature_required)
