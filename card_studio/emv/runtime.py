from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from card_studio.interfaces.base import CardInterface, InterfaceKind
from card_studio.transactions.model import FunctionalResult, TransactionRequest

from .card_action import CardActionInput, run_card_action_flow
from .contactless import C8Policy, ContactlessError, c8_cryptogram_outcome, c8_start, validate_ctq
from .cvm import CvmCapabilities, CvmTransactionType
from .data import DataRecord, DataSensitivity, DataSource, EmvDataStore, OwnershipDomain
from .data_foundation import DataFoundationResult, run_data_foundation
from .decision_engine import DecisionInput, DecisionProfile, DecisionResult, run_decision_engine
from .dol import DolDataType, DolDocument, DolEntry, DolKind, TruncationPolicy
from .oda import OdaProfile, SyntheticOdaProvider, evaluate_oda
from .restrictions import EmvDate
from .risk import RiskProfile
from .taa import ActionCodes, TaaProfile, TerminalAction
from .transaction_semantics import KernelTransactionSemanticsPolicy


class EmvRuntimeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _dol(raw: Any, kind: DolKind) -> DolDocument:
    if not isinstance(raw, (list, tuple)):
        raise EmvRuntimeError("INVALID_EMV_DOL", f"{kind.value} debe ser una lista de entradas.")
    return DolDocument(kind, tuple(
        DolEntry(
            item["tag"],
            item["length"],
            DolDataType(item.get("data_type", "BINARY")),
            TruncationPolicy(item.get("truncation", "REJECT")),
        )
        for item in raw
    ))


def _records(raw: Any) -> tuple[DataRecord, ...]:
    reserved = {"9F02", "5F2A", "9A", "9C", "95", "9F34", "9F66"}
    values = []
    for item in raw or ():
        record = DataRecord(
            item["tag"],
            bytes.fromhex(item["value"]),
            DataSource(item.get("source", "TERMINAL")),
            OwnershipDomain(item.get("ownership", item.get("source", "TERMINAL"))),
            DataSensitivity(item.get("sensitivity", "PUBLIC")),
            context=item.get("context"),
        )
        if record.tag in reserved:
            raise EmvRuntimeError("RESERVED_TERMINAL_TAG", f"{record.tag} pertenece al contexto dinámico.")
        values.append(record)
    return tuple(values)


def _action_codes(raw: Mapping[str, Any] | None) -> ActionCodes:
    values = raw or {}
    return ActionCodes(
        bytes.fromhex(values.get("denial", "0000000000")),
        bytes.fromhex(values.get("online", "0000000000")),
        bytes.fromhex(values.get("default", "0000000000")),
    )


@dataclass(frozen=True)
class EmvFlowConfig:
    pdol: DolDocument
    cdol1: DolDocument
    terminal_date: EmvDate
    decision_profile: DecisionProfile
    currency_numeric: bytes = b"\x04\x84"
    currency: str = "MXN"
    transaction_type: int = 0
    initial_records: tuple[DataRecord, ...] = field(default_factory=tuple)
    required_tags: tuple[str, ...] = ()
    supported_kernel_ids: tuple[str, ...] = ()
    allow_partial: bool = False
    allow_unknown_tags: bool = True
    oda_profile: OdaProfile = OdaProfile()
    ttq: bytes | None = None
    c8: C8Policy = C8Policy()
    unpredictable_number_required: bool = False
    transaction_semantics: KernelTransactionSemanticsPolicy | None = None

    @classmethod
    def from_profile(cls, profile: Any) -> "EmvFlowConfig | None":
        raw = profile.scheme_parameters.get("emv_execution")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise EmvRuntimeError("INVALID_EMV_CONFIG", "emv_execution debe ser Mapping.")
        decision = raw.get("decision", {})
        if not isinstance(decision, Mapping):
            raise EmvRuntimeError("INVALID_EMV_DECISION", "decision debe ser Mapping.")
        risk_raw = decision.get("risk", profile.scheme_parameters.get("risk", {}))
        risk = RiskProfile(
            floor_limit_minor=risk_raw.get("floor_limit_minor", 0),
            transaction_type_floor_limits=risk_raw.get("transaction_type_floor_limits", {}),
            interface_limits_minor=risk_raw.get("interface_limits_minor", {}),
            random_selection_percent=risk_raw.get("random_selection_percent", 0),
        )
        cvm_raw = decision.get("cvm_capabilities", {})
        capabilities = CvmCapabilities(
            offline_pin=cvm_raw.get("offline_pin", False) is True,
            online_pin=cvm_raw.get("online_pin", False) is True,
            signature=cvm_raw.get("signature", False) is True,
            cdcvm=cvm_raw.get("cdcvm", False) is True,
        )
        tac = _action_codes(decision.get("tac"))
        iac_raw = decision.get("iac")
        iac = _action_codes(iac_raw) if iac_raw is not None else None
        taa_raw = decision.get("taa", {})
        taa = TaaProfile(
            fallback_when_offline_unable_online=taa_raw.get("fallback_when_offline_unable_online", "AAC"),
            no_match_default_when_online_capable=taa_raw.get("no_match_default_when_online_capable", "ARQC"),
            no_match_default_when_offline_only=taa_raw.get("no_match_default_when_offline_only", "AAC"),
        )
        decision_profile = DecisionProfile(
            terminal_application_version=bytes.fromhex(decision.get("terminal_application_version", "0001")),
            cvm_capabilities=capabilities,
            risk=risk,
            tac=tac,
            taa=taa,
            terminal_online_capable=decision.get("terminal_online_capable", True) is True,
            iac=iac,
        )
        currency_numeric = bytes.fromhex(raw.get("currency_numeric", "0484"))
        if len(currency_numeric) != 2:
            raise EmvRuntimeError("INVALID_CURRENCY_NUMERIC", "currency_numeric debe ocupar dos bytes.")
        ttq_raw = raw.get("ttq")
        ttq = bytes.fromhex(ttq_raw) if ttq_raw is not None else None
        if ttq is not None and len(ttq) != 4:
            raise EmvRuntimeError("INVALID_TTQ_LENGTH", "TTQ debe ocupar cuatro bytes.")
        c8_raw = raw.get("c8", {})
        c8 = C8Policy(
            transaction_limit_minor=c8_raw.get("transaction_limit_minor", 0),
            require_ctq=c8_raw.get("require_ctq", False) is True,
            ctq_allowed_mask=bytes.fromhex(c8_raw.get("ctq_allowed_mask", "FFFF")),
            relay_time_limit_ms=c8_raw.get("relay_time_limit_ms", 0),
        )
        oda_raw = raw.get("oda", {})
        oda = OdaProfile(
            allow_sda=oda_raw.get("allow_sda", True) is True,
            allow_dda=oda_raw.get("allow_dda", True) is True,
            allow_cda=oda_raw.get("allow_cda", True) is True,
            terminal_dynamic_authentication_supported=oda_raw.get("terminal_dynamic_authentication_supported", True) is True,
            material_status=oda_raw.get("material_status", "SYNTHETIC"),
        )
        un_raw = raw.get("unpredictable_number", {})
        if not isinstance(un_raw, Mapping):
            raise EmvRuntimeError("INVALID_UN_CONFIG", "unpredictable_number debe ser Mapping.")
        un_required = un_raw.get("required", False) is True
        pdol = _dol(raw.get("pdol"), DolKind.PDOL)
        if un_required and not any(entry.tag == "9F37" for entry in pdol.entries):
            raise EmvRuntimeError("UN_PDOL_REQUIRED", "La UN obligatoria debe estar declarada en PDOL como 9F37.")
        return cls(
            pdol=pdol,
            cdol1=_dol(raw.get("cdol1"), DolKind.CDOL1),
            terminal_date=EmvDate.parse(bytes.fromhex(raw.get("terminal_date", "260101"))),
            decision_profile=decision_profile,
            currency_numeric=currency_numeric,
            currency=raw.get("currency", "MXN"),
            transaction_type=raw.get("transaction_type", 0),
            initial_records=_records(raw.get("data")),
            required_tags=tuple(raw.get("required_tags", ())),
            supported_kernel_ids=tuple(item.upper() for item in raw.get("supported_kernel_ids", ())),
            allow_partial=raw.get("allow_partial", False) is True,
            allow_unknown_tags=raw.get("allow_unknown_tags", True) is True,
            oda_profile=oda,
            ttq=ttq,
            c8=c8,
            unpredictable_number_required=un_required,
            transaction_semantics=KernelTransactionSemanticsPolicy.from_profile(profile),
        )

    def transaction_code(self, request: TransactionRequest) -> int:
        if self.transaction_semantics is not None:
            code = self.transaction_semantics.assert_card_kernel(request).transaction_code
            if code is None:
                raise EmvRuntimeError("CARD_TRANSACTION_CODE_REQUIRED", "La operación HOST_ONLY no tiene 9C de tarjeta.")
            return code
        if not 0 <= self.transaction_type <= 255:
            raise EmvRuntimeError("INVALID_TRANSACTION_TYPE", "transaction_type debe ser un byte.")
        return self.transaction_type

    def new_store(self, request: TransactionRequest, unpredictable_number: bytes | None = None) -> EmvDataStore:
        if request.money.currency != self.currency:
            raise EmvRuntimeError("CURRENCY_MISMATCH", "La moneda del request no coincide con emv_execution.")
        transaction_code = self.transaction_code(request)
        store = EmvDataStore()
        store.merge(self.initial_records)
        amount = f"{request.money.amount_minor:012d}"
        if len(amount) != 12:
            raise EmvRuntimeError("AMOUNT_OUT_OF_RANGE", "El importe no cabe en 9F02.")
        records = [
            DataRecord("9F02", bytes.fromhex(amount), DataSource.TERMINAL, OwnershipDomain.TERMINAL),
            DataRecord("5F2A", self.currency_numeric, DataSource.TERMINAL, OwnershipDomain.TERMINAL),
            DataRecord("9A", self.terminal_date.to_bytes(), DataSource.TERMINAL, OwnershipDomain.TERMINAL),
            DataRecord("9C", bytes((transaction_code,)), DataSource.TERMINAL, OwnershipDomain.TERMINAL),
        ]
        if request.interface == InterfaceKind.CONTACTLESS.value:
            if self.ttq is None:
                raise EmvRuntimeError("TTQ_REQUIRED", "El flujo contactless requiere TTQ de cuatro bytes.")
            records.append(DataRecord("9F66", self.ttq, DataSource.TERMINAL, OwnershipDomain.TERMINAL))
        if self.unpredictable_number_required and unpredictable_number is None:
            raise EmvRuntimeError("UN_REQUIRED", "La configuración exige una UN inyectada antes de GPO.")
        if unpredictable_number is not None:
            value = bytes(unpredictable_number)
            if len(value) != 4:
                raise EmvRuntimeError("INVALID_UN_LENGTH", "UN debe ocupar cuatro bytes.")
            records.append(DataRecord("9F37", value, DataSource.TERMINAL, OwnershipDomain.TERMINAL))
        store.merge(records)
        return store

    def decision_input(self, request: TransactionRequest, profile: Any) -> DecisionInput:
        return DecisionInput(
            terminal_date=self.terminal_date,
            interface=InterfaceKind(request.interface),
            supported_interfaces=tuple(InterfaceKind(item) for item in profile.interfaces if item in {"CONTACT", "CONTACTLESS"}),
            amount_minor=request.money.amount_minor,
            transaction_type=self.transaction_code(request),
            cvm_transaction_type=CvmTransactionType.NON_CASH,
            operation_id=request.operation_id,
            profile_ref=request.profile_ref,
            currency=request.money.currency,
        )


@dataclass(frozen=True)
class EmvExecutionResult:
    functional_result: FunctionalResult
    selected_aid: str
    store: EmvDataStore
    cvm_result: dict[str, Any]
    risk_result: dict[str, Any]
    action_result: dict[str, Any]
    evidence: dict[str, Any]


def _stage(handler: Any, stage: str) -> None:
    if handler is not None:
        handler(stage)


def _decision_store(decision: DecisionResult) -> EmvDataStore:
    decision.store.merge((
        DataRecord("95", decision.risk.tvr.to_bytes(), DataSource.KERNEL, OwnershipDomain.KERNEL),
        DataRecord("9F34", decision.cvm.cvm_results, DataSource.KERNEL, OwnershipDomain.KERNEL),
    ))
    return decision.store


def execute_emv_flow(
    card_interface: CardInterface,
    request: TransactionRequest,
    profile: Any,
    config: EmvFlowConfig,
    stage_handler: Any = None,
    unpredictable_number: bytes | None = None,
    velocity: Any = None,
) -> EmvExecutionResult:
    if card_interface.kind not in {InterfaceKind.CONTACT, InterfaceKind.CONTACTLESS}:
        raise EmvRuntimeError("UNSUPPORTED_INTERFACE", "emv_execution requiere CONTACT o CONTACTLESS.")
    connection = card_interface.connection
    protocol = connection.protocol if connection is not None else "UNKNOWN"
    if card_interface.kind == InterfaceKind.CONTACT and protocol not in {"T=0", "T=1"}:
        raise EmvRuntimeError("CONTACT_PROTOCOL_UNKNOWN", "El flujo contact exige protocolo T=0 o T=1.")
    if card_interface.kind == InterfaceKind.CONTACTLESS:
        started = c8_start(request.money.amount_minor, config.c8)
        if started.kind.value == "END_APPLICATION":
            raise ContactlessError("CONTACTLESS_LIMIT_EXCEEDED", "El importe supera el límite C-8.")
    store = config.new_store(request, unpredictable_number=unpredictable_number)
    foundation: DataFoundationResult = run_data_foundation(
        card_interface,
        config.pdol,
        store,
        application_aid=request.application_aid,
        supported_kernel_ids=config.supported_kernel_ids or None,
        allow_partial=config.allow_partial,
        required_tags=config.required_tags,
        allow_unknown_tags=config.allow_unknown_tags,
        stage_handler=stage_handler,
        relay_time_limit_ms=config.c8.relay_time_limit_ms if card_interface.kind == InterfaceKind.CONTACTLESS else 0,
    )
    if card_interface.kind == InterfaceKind.CONTACTLESS:
        validate_ctq(foundation.gpo.ctq, config.c8)
    decision_input = config.decision_input(request, profile)
    if velocity is not None:
        decision_input = replace(decision_input, velocity=velocity)
    decision = run_decision_engine(
        store,
        config.decision_profile,
        decision_input,
        stage_handler=stage_handler,
    )
    store = _decision_store(decision)
    oda_evidence = None
    if card_interface.kind == InterfaceKind.CONTACTLESS and foundation.gpo.cryptogram_result is not None:
        _stage(stage_handler, "OFFLINE_DATA_AUTHENTICATION")
        oda = evaluate_oda(
            store,
            foundation.gpo.aip,
            config.oda_profile,
            SyntheticOdaProvider(),
            request.operation_id,
            request.profile_ref,
            tvr=decision.risk.tvr,
            tsi=decision.risk.tsi,
        )
        if oda.technical_failure:
            raise EmvRuntimeError(oda.reason or "ODA_BLOCKED", "ODA boundary está bloqueada.")
        oda_evidence = oda.to_evidence()
        cryptogram = foundation.gpo.cryptogram_result
        action_evidence = {"cryptogram_source": "GPO", "oda": oda_evidence}
        _stage(stage_handler, "CARD_ACTION_ANALYSIS")
    else:
        action = run_card_action_flow(
            card_interface,
            CardActionInput(
                decision=decision,
                aip=foundation.gpo.aip,
                cdol1=config.cdol1,
                operation_id=request.operation_id,
                profile_ref=request.profile_ref,
                oda_profile=config.oda_profile,
            ),
            SyntheticOdaProvider(),
            stage_handler=stage_handler,
        )
        cryptogram = action.cryptogram_result
        action_evidence = action.to_evidence()
    c8_evidence = None
    if card_interface.kind == InterfaceKind.CONTACTLESS:
        _stage(stage_handler, "C8_OUTCOME")
        c8_evidence = c8_cryptogram_outcome(cryptogram).to_evidence()
    action_result = {
        "terminal_action": decision.proposed_action.value,
        "cryptogram_type": cryptogram.cryptogram_type.value,
        "cryptogram_source": "GPO" if foundation.gpo.cryptogram_result is not None else "GENERATE_AC",
        "interface_protocol": protocol,
        "final_decision_source": "CARD_CRYPTOGRAM",
    }
    if c8_evidence is not None:
        action_result["c8"] = c8_evidence
    evidence = {
        "foundation": foundation.to_evidence(),
        "decision": decision.to_evidence(),
        "card_action": action_evidence,
        "c8": c8_evidence,
        "mode": "LAB_ONLY",
    }
    return EmvExecutionResult(
        functional_result=cryptogram.functional_result,
        selected_aid=foundation.selection.selected.candidate.aid,
        store=store,
        cvm_result=decision.cvm.to_evidence(),
        risk_result=decision.risk.to_evidence(),
        action_result=action_result,
        evidence=evidence,
    )
