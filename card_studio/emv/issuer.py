from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from card_studio.interfaces.base import CardInterface
from card_studio.readers.base import TransportResult
from card_studio.tlv import TlvNode, encode_tlv, parse_tlv
from card_studio.transactions.model import ExecutionState, FunctionalResult

from .apdu import ApduContext, CommandApdu, ResponseApdu, StatusAction, exchange_with_followups
from .status import Tsi, TsiFlag, Tvr, TvrFlag


MAX_ISSUER_SCRIPT_COMMANDS = 32
MAX_ISSUER_SCRIPT_COMMAND_LEN = 261
SUPPORTED_ARC = frozenset({"00", "05", "Y3", "Z3"})


class IssuerError(ValueError):
    def __init__(self, code: str, message: str, command_sent: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.command_sent = command_sent


class ScriptPhase(str, Enum):
    BEFORE_FINAL_GAC = "BEFORE_FINAL_GAC"
    AFTER_FINAL_GAC = "AFTER_FINAL_GAC"


@dataclass(frozen=True)
class IssuerScript:
    phase: ScriptPhase
    identifier: bytes | None
    commands: tuple[CommandApdu, ...]

    def __post_init__(self) -> None:
        if self.identifier is not None and len(self.identifier) != 4:
            raise IssuerError("INVALID_SCRIPT_IDENTIFIER", "9F18 debe medir cuatro bytes.")
        if not self.commands:
            raise IssuerError("EMPTY_ISSUER_SCRIPT", "Script requiere al menos un comando 86.")

    def to_evidence(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "identifier_present": self.identifier is not None,
            "command_count": len(self.commands),
        }


@dataclass(frozen=True)
class HostEnvelope:
    arc: str
    execution_state: ExecutionState
    functional_result: FunctionalResult | None
    host_reference: str
    response_template_sha256: str
    issuer_authentication_data: bytes | None
    scripts_before: tuple[IssuerScript, ...]
    scripts_after: tuple[IssuerScript, ...]

    def to_evidence(self) -> dict[str, Any]:
        return {
            "arc": self.arc,
            "execution_state": self.execution_state.value,
            "functional_result": self.functional_result.value if self.functional_result else None,
            "host_reference": self.host_reference,
            "response_template_sha256": self.response_template_sha256,
            "issuer_authentication_present": self.issuer_authentication_data is not None,
            "scripts_before": [script.to_evidence() for script in self.scripts_before],
            "scripts_after": [script.to_evidence() for script in self.scripts_after],
        }


def _root_objects(data: bytes) -> dict[str, TlvNode]:
    try:
        nodes = parse_tlv(data)
    except ValueError as exc:
        raise IssuerError("MALFORMED_RESPONSE_TEMPLATE", "response_template BER truncado o no canónico.") from exc
    if not nodes:
        raise IssuerError("MALFORMED_RESPONSE_TEMPLATE", "response_template vacío.")
    objects: dict[str, TlvNode] = {}
    for node in nodes:
        if node.tag not in {"8A", "91", "71", "72"}:
            raise IssuerError("UNSUPPORTED_RESPONSE_OBJECT", f"Objeto no soportado: {node.tag}.")
        if node.tag in objects:
            raise IssuerError("DUPLICATE_RESPONSE_OBJECT", f"Objeto duplicado: {node.tag}.")
        objects[node.tag] = node
    return objects


def _arc(node: TlvNode | None) -> str:
    if node is None or node.constructed or len(node.value) != 2:
        raise IssuerError("INVALID_AUTHORIZATION_RESPONSE_CODE", "8A debe ser primitivo ASCII de dos bytes.")
    try:
        arc = node.value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise IssuerError("INVALID_AUTHORIZATION_RESPONSE_CODE", "ARC no es ASCII.") from exc
    if arc not in SUPPORTED_ARC:
        raise IssuerError("INVALID_AUTHORIZATION_RESPONSE_CODE", "ARC desconocido en política LAB.")
    return arc


def _script(node: TlvNode | None, phase: ScriptPhase) -> tuple[IssuerScript, ...]:
    if node is None:
        return ()
    if not node.constructed:
        raise IssuerError("INVALID_ISSUER_SCRIPT", f"{node.tag} debe ser construido.")
    identifier = None
    commands: list[CommandApdu] = []
    for child in node.children:
        if child.constructed:
            raise IssuerError("NESTED_RESPONSE_OBJECT", f"Objeto anidado en {node.tag}.")
        if child.tag == "9F18":
            if identifier is not None:
                raise IssuerError("DUPLICATE_RESPONSE_OBJECT", f"9F18 duplicado en {node.tag}.")
            if len(child.value) != 4:
                raise IssuerError("INVALID_SCRIPT_IDENTIFIER", "9F18 debe medir cuatro bytes.")
            identifier = child.value
        elif child.tag == "86":
            if not 4 <= len(child.value) <= MAX_ISSUER_SCRIPT_COMMAND_LEN:
                raise IssuerError("INVALID_ISSUER_SCRIPT", "Comando 86 fuera de longitud permitida.")
            try:
                commands.append(CommandApdu.parse(child.value))
            except ValueError as exc:
                raise IssuerError("INVALID_ISSUER_SCRIPT", "Comando 86 APDU inválido.") from exc
        else:
            raise IssuerError("UNSUPPORTED_RESPONSE_OBJECT", f"Objeto {child.tag} no soportado en {node.tag}.")
    if not commands:
        raise IssuerError("EMPTY_ISSUER_SCRIPT", f"{node.tag} requiere al menos un comando 86.")
    return (IssuerScript(phase, identifier, tuple(commands)),)


def parse_host_envelope(host_response: Mapping[str, Any]) -> HostEnvelope:
    if not isinstance(host_response, Mapping):
        raise IssuerError("INVALID_HOST_RESPONSE", "host_response debe ser Mapping.")
    if host_response.get("mode") != "SIMULATED" or host_response.get("simulation") is not True:
        raise IssuerError("INVALID_HOST_MODE", "mode=SIMULATED y simulation=True son obligatorios.")
    try:
        execution_state = ExecutionState(host_response["execution_state"])
    except (KeyError, ValueError) as exc:
        raise IssuerError("INVALID_HOST_EXECUTION_STATE", "execution_state transporte inválido.") from exc
    raw_functional = host_response.get("functional_result")
    try:
        functional = FunctionalResult(raw_functional) if raw_functional is not None else None
    except ValueError as exc:
        raise IssuerError("INVALID_HOST_FUNCTIONAL_RESULT", "functional_result transporte inválido.") from exc
    reference = host_response.get("host_reference")
    if not isinstance(reference, str) or not reference.startswith("hr-") or len(reference) > 128:
        raise IssuerError("INVALID_HOST_REFERENCE", "host_reference debe usar identidad hr- segura.")
    template = host_response.get("response_template")
    if not isinstance(template, str) or not template or len(template) % 2:
        raise IssuerError("MALFORMED_RESPONSE_TEMPLATE", "response_template debe ser hexadecimal BER completo.")
    try:
        data = bytes.fromhex(template)
    except ValueError as exc:
        raise IssuerError("MALFORMED_RESPONSE_TEMPLATE", "response_template no es hexadecimal.") from exc
    objects = _root_objects(data)
    arc = _arc(objects.get("8A"))
    issuer_data_node = objects.get("91")
    if issuer_data_node is not None and (issuer_data_node.constructed or not 8 <= len(issuer_data_node.value) <= 16):
        raise IssuerError("INVALID_ISSUER_AUTHENTICATION_DATA", "91 debe ser primitivo de 8 a 16 bytes.")
    issuer_data = issuer_data_node.value if issuer_data_node is not None else None
    before = _script(objects.get("71"), ScriptPhase.BEFORE_FINAL_GAC)
    after = _script(objects.get("72"), ScriptPhase.AFTER_FINAL_GAC)
    command_count = sum(len(script.commands) for script in before + after)
    if command_count > MAX_ISSUER_SCRIPT_COMMANDS:
        raise IssuerError("ISSUER_SCRIPT_LIMIT_EXCEEDED", "Demasiados comandos de issuer script.")
    expected: dict[str, tuple[ExecutionState, FunctionalResult | None]] = {
        "00": (ExecutionState.SUCCEEDED, FunctionalResult.APPROVED),
        "05": (ExecutionState.FAILED, FunctionalResult.DECLINED),
        "Y3": (ExecutionState.SUCCEEDED, FunctionalResult.APPROVED),
    }
    if arc in expected and (execution_state, functional) != expected[arc]:
        raise IssuerError("HOST_TRANSPORT_MISMATCH", "ARC no coincide con el resultado transporte.")
    if arc == "Z3" and not (
        (execution_state == ExecutionState.FAILED and functional == FunctionalResult.DECLINED)
        or (execution_state == ExecutionState.UNCERTAIN and functional is None)
    ):
        raise IssuerError("HOST_TRANSPORT_MISMATCH", "Z3 requiere fallback declined o uncertain.")
    return HostEnvelope(
        arc,
        execution_state,
        functional,
        reference,
        hashlib.sha256(data).hexdigest(),
        issuer_data,
        before,
        after,
    )


def external_authenticate_command(issuer_authentication_data: bytes) -> CommandApdu:
    data = bytes(issuer_authentication_data)
    if not 8 <= len(data) <= 16:
        raise IssuerError("INVALID_ISSUER_AUTHENTICATION_DATA", "ARPC sintético debe medir 8 a 16 bytes.")
    return CommandApdu(0x00, 0x82, 0x00, 0x00, data)


@dataclass(frozen=True)
class IssuerAuthenticationResult:
    success: bool
    tvr: Tvr
    tsi: Tsi

    def to_evidence(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "arpc_present": True,
            "tvr": self.tvr.to_evidence(),
            "tsi": self.tsi.to_evidence(),
        }


def execute_external_authenticate(
    card_interface: CardInterface,
    issuer_authentication_data: bytes,
    tvr: Tvr,
    tsi: Tsi,
) -> IssuerAuthenticationResult:
    exchange = exchange_with_followups(
        card_interface,
        external_authenticate_command(issuer_authentication_data),
        context=ApduContext.GENERIC,
    )
    next_tvr = Tvr.parse(tvr.to_bytes())
    next_tsi = Tsi.parse(tsi.to_bytes())
    next_tsi.mark_processed(TsiFlag.ISSUER_AUTHENTICATION_PERFORMED)
    if exchange.action == StatusAction.SUCCESS and not exchange.response.data:
        return IssuerAuthenticationResult(True, next_tvr, next_tsi)
    if not exchange.response.is_success and not exchange.response.data:
        next_tvr.set(TvrFlag.ISSUER_AUTHENTICATION_FAILED)
        return IssuerAuthenticationResult(False, next_tvr, next_tsi)
    raise IssuerError("INVALID_EXTERNAL_AUTHENTICATE_RESPONSE", "EXTERNAL AUTHENTICATE devolvió datos o estado inválido.", command_sent=True)


@dataclass(frozen=True)
class ScriptCommandStatus:
    sw1: int
    sw2: int
    succeeded: bool

    def to_evidence(self) -> dict[str, Any]:
        return {"sw1": self.sw1, "sw2": self.sw2, "succeeded": self.succeeded}


@dataclass(frozen=True)
class IssuerScriptExecutionResult:
    success: bool
    statuses: tuple[ScriptCommandStatus, ...]
    tvr: Tvr
    tsi: Tsi

    def to_evidence(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "statuses": [status.to_evidence() for status in self.statuses],
            "tvr": self.tvr.to_evidence(),
            "tsi": self.tsi.to_evidence(),
        }


def execute_issuer_scripts(
    card_interface: CardInterface,
    scripts: tuple[IssuerScript, ...],
    tvr: Tvr,
    tsi: Tsi,
) -> IssuerScriptExecutionResult:
    next_tvr = Tvr.parse(tvr.to_bytes())
    next_tsi = Tsi.parse(tsi.to_bytes())
    statuses: list[ScriptCommandStatus] = []
    for script in scripts:
        for command in script.commands:
            exchange = exchange_with_followups(card_interface, command, context=ApduContext.GENERIC)
            succeeded = exchange.action == StatusAction.SUCCESS
            statuses.append(ScriptCommandStatus(exchange.response.sw1, exchange.response.sw2, succeeded))
            next_tsi.mark_processed(TsiFlag.SCRIPT_PROCESSING_PERFORMED)
            if not succeeded:
                failure = (
                    TvrFlag.SCRIPT_PROCESSING_FAILED_BEFORE_FINAL_GAC
                    if script.phase == ScriptPhase.BEFORE_FINAL_GAC
                    else TvrFlag.SCRIPT_PROCESSING_FAILED_AFTER_FINAL_GAC
                )
                next_tvr.set(failure)
                return IssuerScriptExecutionResult(False, tuple(statuses), next_tvr, next_tsi)
    return IssuerScriptExecutionResult(True, tuple(statuses), next_tvr, next_tsi)


def synthetic_script_apdu(ins: int = 0xDA, payload: bytes = b"\x01") -> CommandApdu:
    return CommandApdu(0x00, ins, 0x00, 0x00, payload)


def synthetic_script_response(command: CommandApdu, status: str = "9000", data: bytes = b"") -> TransportResult:
    return TransportResult(data, status)
