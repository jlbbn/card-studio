from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from card_studio.interfaces.base import CardInterface, InterfaceKind
from card_studio.tlv import TlvNode, encode_tlv
from card_studio.transactions.model import ExecutionState, FunctionalResult
from card_studio.transactions.recovery import RecoveryClass

from .cryptogram import CryptogramResult
from .data import DataRecord, DataSensitivity, DataSource, EmvDataStore, OwnershipDomain
from .dol import DolDataType, DolDocument, DolEntry, DolKind, TruncationPolicy
from .gac import GenerateAcResponse, build_final_generate_ac_command, generate_ac
from .issuer import (
    HostEnvelope,
    IssuerError,
    IssuerScript,
    ScriptPhase,
    execute_external_authenticate,
    execute_issuer_scripts,
    parse_host_envelope,
)
from .status import Tsi, Tvr
from .taa import TerminalAction


class FinalGacPolicy(str, Enum):
    REQUIRED = "REQUIRED"
    OMIT = "OMIT"
    PROFILE = "PROFILE"


class UnablePolicy(str, Enum):
    DECLINED = "DECLINED"
    UNCERTAIN = "UNCERTAIN"


class IssuerAuthenticationPolicy(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    DISABLED = "DISABLED"


class OnlineCompletionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OnlineFlowConfig:
    cdol2: DolDocument | None = None
    final_gac_policy: FinalGacPolicy = FinalGacPolicy.PROFILE
    final_action: TerminalAction | None = None
    unable_policy: UnablePolicy = UnablePolicy.DECLINED
    issuer_authentication: IssuerAuthenticationPolicy = IssuerAuthenticationPolicy.OPTIONAL
    scripts_enabled: bool = True
    initial_records: tuple[DataRecord, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "final_gac_policy", FinalGacPolicy(self.final_gac_policy))
        object.__setattr__(self, "unable_policy", UnablePolicy(self.unable_policy))
        object.__setattr__(self, "issuer_authentication", IssuerAuthenticationPolicy(self.issuer_authentication))
        if self.final_action is not None:
            object.__setattr__(self, "final_action", TerminalAction(self.final_action))
        if self.final_gac_policy == FinalGacPolicy.REQUIRED and self.cdol2 is None:
            raise OnlineCompletionError("CDOL2_REQUIRED", "La política REQUIRED necesita CDOL2.")
        if self.cdol2 is not None and self.cdol2.kind != DolKind.CDOL2:
            raise OnlineCompletionError("INVALID_CDOL_KIND", "Online completion requiere CDOL2.")

    @classmethod
    def from_profile(cls, profile: Any) -> "OnlineFlowConfig | None":
        raw = profile.scheme_parameters.get("online_completion")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise OnlineCompletionError("INVALID_ONLINE_CONFIG", "online_completion debe ser Mapping.")
        entries = []
        for item in raw.get("cdol2", ()):
            entries.append(DolEntry(
                item["tag"],
                item["length"],
                DolDataType(item.get("data_type", "BINARY")),
                TruncationPolicy(item.get("truncation", "REJECT")),
            ))
        cdol2 = DolDocument(DolKind.CDOL2, tuple(entries)) if entries else None
        records = []
        for item in raw.get("data", ()):
            records.append(DataRecord(
                item["tag"],
                bytes.fromhex(item["value"]),
                DataSource(item.get("source", "TERMINAL")),
                OwnershipDomain(item.get("ownership", item.get("source", "TERMINAL"))),
                DataSensitivity(item.get("sensitivity", "PUBLIC")),
                context=item.get("context"),
            ))
        final_action = TerminalAction(raw["final_action"]) if raw.get("final_action") else None
        return cls(
            cdol2=cdol2,
            final_gac_policy=FinalGacPolicy(raw.get("final_gac_policy", "PROFILE")),
            final_action=final_action,
            unable_policy=UnablePolicy(raw.get("unable_policy", "DECLINED")),
            issuer_authentication=IssuerAuthenticationPolicy(raw.get("issuer_authentication", "OPTIONAL")),
            scripts_enabled=raw.get("scripts_enabled", True) is True,
            initial_records=tuple(records),
        )

    def new_store(self) -> EmvDataStore:
        store = EmvDataStore()
        store.merge(self.initial_records)
        return store


@dataclass(frozen=True)
class OnlineCompletionResult:
    execution_state: ExecutionState
    functional_result: FunctionalResult | None
    recovery: RecoveryClass
    error_code: str | None
    host_reference: str
    response_template_sha256: str
    cryptogram_result: CryptogramResult | None
    tvr: Tvr
    tsi: Tsi
    store: EmvDataStore
    history: tuple[dict[str, Any], ...]

    def to_evidence(self) -> dict[str, Any]:
        return {
            "execution_state": self.execution_state.value,
            "functional_result": self.functional_result.value if self.functional_result else None,
            "recovery": self.recovery.value,
            "error_code": self.error_code,
            "host_reference": self.host_reference,
            "response_template_sha256": self.response_template_sha256,
            "cryptogram_result": self.cryptogram_result.to_evidence() if self.cryptogram_result else None,
            "tvr": self.tvr.to_evidence(),
            "tsi": self.tsi.to_evidence(),
            "store": self.store.to_evidence(),
            "history": list(self.history),
            "mode": "LAB_ONLY",
        }


def _script_data(scripts: tuple[IssuerScript, ...]) -> bytes:
    if not scripts:
        return b""
    script = scripts[0]
    children = []
    if script.identifier is not None:
        children.append(TlvNode("9F18", script.identifier))
    children.extend(TlvNode("86", command.to_bytes()) for command in script.commands)
    return encode_tlv(children)


def _host_records(envelope: HostEnvelope) -> tuple[DataRecord, ...]:
    records = [DataRecord(
        "8A",
        envelope.arc.encode("ascii"),
        DataSource.HOST,
        OwnershipDomain.HOST,
        context=envelope.host_reference,
    )]
    if envelope.issuer_authentication_data is not None:
        records.append(DataRecord(
            "91",
            envelope.issuer_authentication_data,
            DataSource.HOST,
            OwnershipDomain.HOST,
            DataSensitivity.SECRET,
            context=envelope.host_reference,
        ))
    for tag, scripts in (("71", envelope.scripts_before), ("72", envelope.scripts_after)):
        if scripts:
            records.append(DataRecord(
                tag,
                _script_data(scripts),
                DataSource.HOST,
                OwnershipDomain.HOST,
                DataSensitivity.SECRET,
                context=envelope.host_reference,
            ))
    return tuple(records)


def _final_action(envelope: HostEnvelope, configured: TerminalAction | None) -> TerminalAction:
    if configured is not None:
        return configured
    return TerminalAction.TC if envelope.arc in {"00", "Y3"} else TerminalAction.AAC


def _technical(
    code: str,
    envelope: HostEnvelope,
    tvr: Tvr,
    tsi: Tsi,
    store: EmvDataStore,
    history: list[dict[str, Any]],
) -> OnlineCompletionResult:
    return OnlineCompletionResult(
        ExecutionState.FAILED,
        None,
        RecoveryClass.OPERATOR_REVIEW,
        code,
        envelope.host_reference,
        envelope.response_template_sha256,
        None,
        tvr,
        tsi,
        store,
        tuple(history),
    )


def process_online_completion(
    card_interface: CardInterface,
    host_response: Mapping[str, Any],
    config: OnlineFlowConfig,
    store: EmvDataStore,
    tvr: Tvr | None = None,
    tsi: Tsi | None = None,
    stage_handler: Any = None,
) -> OnlineCompletionResult:
    envelope = parse_host_envelope(host_response)
    initial_tvr = Tvr.parse(tvr.to_bytes()) if tvr is not None else Tvr.cleared()
    initial_tsi = Tsi.parse(tsi.to_bytes()) if tsi is not None else Tsi.cleared()

    def enter_stage(stage: str) -> None:
        if stage_handler is not None:
            stage_handler(stage)

    if envelope.arc == "Z3" and config.unable_policy == UnablePolicy.UNCERTAIN:
        return OnlineCompletionResult(
            ExecutionState.UNCERTAIN,
            None,
            RecoveryClass.QUERY_REQUIRED,
            "HOST_UNABLE_TO_GO_ONLINE",
            envelope.host_reference,
            envelope.response_template_sha256,
            None,
            initial_tvr,
            initial_tsi,
            store,
            ({"stage": "HOST_UNABLE_TO_GO_ONLINE", "outcome": "UNCERTAIN"},),
        )
    working = store.clone()
    working.merge(_host_records(envelope))
    history: list[dict[str, Any]] = [{"stage": "HOST_RESPONSE_ACCEPTED", "arc": envelope.arc}]
    current_tvr = initial_tvr
    current_tsi = initial_tsi
    if envelope.issuer_authentication_data is not None:
        if config.issuer_authentication == IssuerAuthenticationPolicy.DISABLED:
            raise OnlineCompletionError("ISSUER_AUTHENTICATION_DISABLED", "91 presente cuando issuer authentication está deshabilitado.")
        enter_stage("ISSUER_AUTHENTICATION")
        authentication = execute_external_authenticate(
            card_interface,
            envelope.issuer_authentication_data,
            current_tvr,
            current_tsi,
        )
        current_tvr, current_tsi = authentication.tvr, authentication.tsi
        history.append({"stage": "EXTERNAL_AUTHENTICATE", "success": authentication.success})
        if not authentication.success:
            return _technical("ISSUER_AUTHENTICATION_FAILED", envelope, current_tvr, current_tsi, working, history)
    elif config.issuer_authentication == IssuerAuthenticationPolicy.REQUIRED:
        raise OnlineCompletionError("MISSING_ISSUER_AUTHENTICATION_DATA", "La política requiere 91.")
    if config.scripts_enabled and envelope.scripts_before:
        enter_stage("ISSUER_SCRIPTS_BEFORE_FINAL_GAC")
        before = execute_issuer_scripts(card_interface, envelope.scripts_before, current_tvr, current_tsi)
        current_tvr, current_tsi = before.tvr, before.tsi
        history.append({
            "stage": "ISSUER_SCRIPTS_BEFORE_FINAL_GAC",
            "success": before.success,
            "statuses": [status.to_evidence() for status in before.statuses],
        })
        if not before.success:
            return _technical("ISSUER_SCRIPT_FAILED_BEFORE_FINAL_GAC", envelope, current_tvr, current_tsi, working, history)
    final_gac: GenerateAcResponse | None = None
    required = config.final_gac_policy == FinalGacPolicy.REQUIRED or (
        config.final_gac_policy == FinalGacPolicy.PROFILE and card_interface.kind == InterfaceKind.CONTACT
    )
    if required:
        if config.cdol2 is None:
            raise OnlineCompletionError("CDOL2_REQUIRED", "El flujo contact requiere CDOL2.")
        enter_stage("FINAL_CARD_ACTION_ANALYSIS")
        final_gac = generate_ac(
            card_interface,
            build_final_generate_ac_command(_final_action(envelope, config.final_action), config.cdol2, working),
        )
        history.append({"stage": "FINAL_GENERATE_AC", "cryptogram_type": final_gac.result.cryptogram_type.value})
        expected = _final_action(envelope, config.final_action).value
        if final_gac.result.cryptogram_type.value != expected:
            return _technical("FINAL_OUTCOME_MISMATCH", envelope, current_tvr, current_tsi, working, history)
    if config.scripts_enabled and envelope.scripts_after:
        enter_stage("ISSUER_SCRIPTS_AFTER_FINAL_GAC")
        after = execute_issuer_scripts(card_interface, envelope.scripts_after, current_tvr, current_tsi)
        current_tvr, current_tsi = after.tvr, after.tsi
        history.append({
            "stage": "ISSUER_SCRIPTS_AFTER_FINAL_GAC",
            "success": after.success,
            "statuses": [status.to_evidence() for status in after.statuses],
        })
        if not after.success:
            return _technical("ISSUER_SCRIPT_FAILED_AFTER_FINAL_GAC", envelope, current_tvr, current_tsi, working, history)
    approved = envelope.arc in {"00", "Y3"}
    return OnlineCompletionResult(
        ExecutionState.SUCCEEDED if approved else ExecutionState.FAILED,
        FunctionalResult.APPROVED if approved else FunctionalResult.DECLINED,
        RecoveryClass.NON_RETRYABLE,
        None if approved else "SIMULATED_HOST_DECLINE",
        envelope.host_reference,
        envelope.response_template_sha256,
        final_gac.result if final_gac else None,
        current_tvr,
        current_tsi,
        working,
        tuple(history),
    )


def synthetic_host_response(
    arc: str = "00",
    host_reference: str = "hr-test",
    execution_state: ExecutionState | None = None,
    functional_result: FunctionalResult | None = None,
    issuer_authentication_data: bytes | None = None,
    scripts_before: tuple[IssuerScript, ...] = (),
    scripts_after: tuple[IssuerScript, ...] = (),
) -> dict[str, Any]:
    if execution_state is None:
        execution_state = ExecutionState.UNCERTAIN if arc == "Z3" else (ExecutionState.SUCCEEDED if arc in {"00", "Y3"} else ExecutionState.FAILED)
    if functional_result is None and execution_state != ExecutionState.UNCERTAIN:
        functional_result = FunctionalResult.APPROVED if arc in {"00", "Y3"} else FunctionalResult.DECLINED
    roots = [TlvNode("8A", arc.encode("ascii"))]
    if issuer_authentication_data is not None:
        roots.append(TlvNode("91", issuer_authentication_data))
    if scripts_before:
        roots.append(TlvNode("71", _script_data(scripts_before)))
    if scripts_after:
        roots.append(TlvNode("72", _script_data(scripts_after)))
    template = encode_tlv(roots).hex().upper()
    return {
        "mode": "SIMULATED",
        "simulation": True,
        "execution_state": execution_state.value,
        "functional_result": functional_result.value if functional_result else None,
        "host_reference": host_reference,
        "response_template": template,
    }
