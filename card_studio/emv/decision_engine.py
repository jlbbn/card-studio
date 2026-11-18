from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from card_studio.interfaces.base import InterfaceKind

from .cvm import (
    CvmCapabilities,
    CvmContext,
    CvmEvaluation,
    CvmList,
    CvmRule,
    CvmTransactionType,
    PinHandles,
    evaluate_cvm,
)
from .data import DataRecord, DataSource, EmvDataStore, OwnershipDomain
from .restrictions import (
    EmvDate,
    RestrictionInput,
    RestrictionPolicy,
    RestrictionResult,
    ServiceType,
    TerminalChannel,
    TransactionRegion,
    evaluate_processing_restrictions,
)
from .risk import RiskEvaluation, RiskInput, RiskProfile, evaluate_risk
from .status import Tsi, Tvr
from .taa import ActionCodes, TaaError, TaaEvaluation, TaaInput, TaaProfile, TerminalAction, evaluate_taa
from .velocity import VelocityDecision


EMV_DECISION_ENGINE = "EMV_DECISION_ENGINE"


@dataclass(frozen=True)
class DecisionProfile:
    terminal_application_version: bytes
    cvm_capabilities: CvmCapabilities
    risk: RiskProfile
    tac: ActionCodes
    taa: TaaProfile
    terminal_online_capable: bool = True
    iac: ActionCodes | None = None
    restrictions: RestrictionPolicy = RestrictionPolicy()


@dataclass(frozen=True)
class DecisionInput:
    terminal_date: EmvDate
    interface: InterfaceKind
    supported_interfaces: tuple[InterfaceKind, ...]
    amount_minor: int
    transaction_type: int
    cvm_transaction_type: CvmTransactionType
    operation_id: str
    profile_ref: str
    currency: str
    region: TransactionRegion = TransactionRegion.DOMESTIC
    service: ServiceType = ServiceType.GOODS
    channel: TerminalChannel = TerminalChannel.OTHER_THAN_ATM
    new_card: bool = False
    pin_handles: PinHandles = PinHandles()
    transaction_currency_matches_application: bool = True
    cdcvm_performed: bool = False
    online_pin_entered: bool = False
    exception_file_match: bool = False
    merchant_forced_online: bool = False
    random_sample_basis_points: int | None = None
    velocity: VelocityDecision | None = None


@dataclass(frozen=True)
class DecisionResult:
    restrictions: RestrictionResult
    cvm: CvmEvaluation
    risk: RiskEvaluation
    taa: TaaEvaluation
    store: EmvDataStore

    @property
    def proposed_action(self) -> TerminalAction:
        return self.taa.proposed_action

    @property
    def gate_status(self) -> str:
        return "PASS"

    def to_evidence(self) -> dict[str, Any]:
        return {
            "mode": "LAB_ONLY",
            "gate": EMV_DECISION_ENGINE,
            "status": self.gate_status,
            "history": [
                {"stage": "PROCESSING_RESTRICTIONS", "failed_checks": list(self.restrictions.failed_checks)},
                {"stage": "CVM_PROCESSING", "outcome": self.cvm.outcome.value},
                {"stage": "TERMINAL_RISK_MANAGEMENT", "online_required": self.risk.online_required},
                {"stage": "TERMINAL_ACTION_ANALYSIS", "proposed_action": self.proposed_action.value},
            ],
            "restrictions": self.restrictions.to_evidence(),
            "cvm": self.cvm.to_evidence(),
            "risk": self.risk.to_evidence(),
            "taa": self.taa.to_evidence(),
            "store": self.store.to_evidence(),
        }


@dataclass(frozen=True)
class DecisionEngine:
    profile: DecisionProfile

    def _iac(self, store: EmvDataStore) -> ActionCodes:
        if self.profile.iac is not None:
            return self.profile.iac
        values = []
        for tag in ("9F0E", "9F0F", "9F0D"):
            record = store.require(tag)
            if record.source != DataSource.CARD or record.ownership != OwnershipDomain.CARD:
                raise TaaError("INVALID_CARD_PROVENANCE", f"IAC {tag} no pertenece a CARD.")
            values.append(record.value)
        return ActionCodes(*values)

    def evaluate(self, store: EmvDataStore, input: DecisionInput, stage_handler: Any = None) -> DecisionResult:
        iac = self._iac(store)
        if stage_handler is not None:
            stage_handler("PROCESSING_RESTRICTIONS")
        restrictions = evaluate_processing_restrictions(
            store,
            RestrictionInput(
                terminal_date=input.terminal_date,
                terminal_application_version=self.profile.terminal_application_version,
                interface=input.interface,
                supported_interfaces=input.supported_interfaces,
                region=input.region,
                service=input.service,
                channel=input.channel,
                new_card=input.new_card,
            ),
            policy=self.profile.restrictions,
        )
        if stage_handler is not None:
            stage_handler("CVM_PROCESSING")
        cvm = evaluate_cvm(
            store,
            CvmContext(
                amount=input.amount_minor,
                transaction_type=input.cvm_transaction_type,
                capabilities=self.profile.cvm_capabilities,
                handles=input.pin_handles,
                transaction_currency_matches_application=input.transaction_currency_matches_application,
                cdcvm_performed=input.cdcvm_performed,
                online_pin_entered=input.online_pin_entered,
            ),
            tvr=restrictions.tvr,
            tsi=restrictions.tsi,
        )
        if stage_handler is not None:
            stage_handler("TERMINAL_RISK_MANAGEMENT")
        risk = evaluate_risk(
            RiskInput(
                amount_minor=input.amount_minor,
                transaction_type=input.transaction_type,
                interface=input.interface,
                operation_id=input.operation_id,
                profile_ref=input.profile_ref,
                currency=input.currency,
                profile=self.profile.risk,
                exception_file_match=input.exception_file_match,
                merchant_forced_online=input.merchant_forced_online,
                random_sample_basis_points=input.random_sample_basis_points,
                velocity=input.velocity,
            ),
            tvr=cvm.tvr,
            tsi=cvm.tsi,
        )
        if stage_handler is not None:
            stage_handler("TERMINAL_ACTION_ANALYSIS")
        taa = evaluate_taa(TaaInput(
            tvr=risk.tvr,
            tsi=risk.tsi,
            tac=self.profile.tac,
            iac=iac,
            terminal_online_capable=self.profile.terminal_online_capable,
            profile=self.profile.taa,
            cvm=cvm,
            risk=risk,
        ))
        return DecisionResult(restrictions, cvm, risk, taa, store)


def run_decision_engine(
    store: EmvDataStore,
    profile: DecisionProfile,
    input: DecisionInput,
    stage_handler: Any = None,
) -> DecisionResult:
    return DecisionEngine(profile).evaluate(store, input, stage_handler=stage_handler)


def install_synthetic_decision_data(
    store: EmvDataStore,
    cvm_rules: tuple[CvmRule, ...] = (CvmRule(0x1F, 0x00),),
    amount_x: int = 0,
    amount_y: int = 0,
    iac: ActionCodes | None = None,
    application_version: bytes = b"\x00\x01",
    effective_date: bytes = b"\x25\x01\x01",
    expiration_date: bytes = b"\x30\x12\x31",
    auc: bytes = b"\xff\x80",
) -> EmvDataStore:
    cvm_list = CvmList(amount_x, amount_y, cvm_rules).encode()
    action_codes = iac or ActionCodes.zeroed()
    store.merge((
        DataRecord("9F08", application_version, DataSource.CARD, OwnershipDomain.CARD),
        DataRecord("5F25", effective_date, DataSource.CARD, OwnershipDomain.CARD),
        DataRecord("5F24", expiration_date, DataSource.CARD, OwnershipDomain.CARD),
        DataRecord("9F07", auc, DataSource.CARD, OwnershipDomain.CARD),
        DataRecord("8E", cvm_list, DataSource.CARD, OwnershipDomain.CARD),
        DataRecord("9F0D", action_codes.default, DataSource.CARD, OwnershipDomain.CARD),
        DataRecord("9F0E", action_codes.denial, DataSource.CARD, OwnershipDomain.CARD),
        DataRecord("9F0F", action_codes.online, DataSource.CARD, OwnershipDomain.CARD),
    ))
    return store


def synthetic_decision_profile(**overrides: Any) -> DecisionProfile:
    values: dict[str, Any] = {
        "terminal_application_version": b"\x00\x01",
        "cvm_capabilities": CvmCapabilities(),
        "risk": RiskProfile(),
        "tac": ActionCodes.zeroed(),
        "taa": TaaProfile(),
        "terminal_online_capable": True,
        "iac": None,
        "restrictions": RestrictionPolicy(),
    }
    values.update(overrides)
    return DecisionProfile(**values)


def synthetic_decision_input(**overrides: Any) -> DecisionInput:
    values: dict[str, Any] = {
        "terminal_date": EmvDate(26, 1, 1),
        "interface": InterfaceKind.CONTACT,
        "supported_interfaces": (InterfaceKind.CONTACT, InterfaceKind.CONTACTLESS),
        "amount_minor": 1000,
        "transaction_type": 0,
        "cvm_transaction_type": CvmTransactionType.NON_CASH,
        "operation_id": "decision-op-1",
        "profile_ref": "lab-decision:1:" + "a" * 64,
        "currency": "MXN",
        "region": TransactionRegion.DOMESTIC,
        "service": ServiceType.GOODS,
        "channel": TerminalChannel.OTHER_THAN_ATM,
        "new_card": False,
        "pin_handles": PinHandles(),
        "transaction_currency_matches_application": True,
        "cdcvm_performed": False,
        "online_pin_entered": False,
        "exception_file_match": False,
        "merchant_forced_online": False,
        "random_sample_basis_points": None,
    }
    values.update(overrides)
    return DecisionInput(**values)


def decision_input_with(input: DecisionInput, **overrides: Any) -> DecisionInput:
    return replace(input, **overrides)
