from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Mapping

from card_studio.applet import APPLET_AID_CANDIDATES, build_select_apdu
from card_studio.hardware_validation import SelectAttempt, decide_aid
from card_studio.readers.base import CardObservation, ReaderAdapter, ReaderError


class PhysicalPreparationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PhysicalGate(str, Enum):
    BLANK = "BLANK"
    DETECTED = "DETECTED"
    VALIDATED = "VALIDATED"
    APPLET_READY = "APPLET_READY"
    PERSONALIZING = "PERSONALIZING"
    PERSONALIZED = "PERSONALIZED"
    VERIFIED = "VERIFIED"
    REGISTERED = "REGISTERED"
    TOKEN_BOUND = "TOKEN_BOUND"
    ACTIVE = "ACTIVE"


_GATE_ORDER = tuple(PhysicalGate)


class PhysicalGateMachine:
    def __init__(self):
        self.state = PhysicalGate.BLANK
        self.history: list[dict[str, Any]] = []

    def advance(self, target: PhysicalGate, reason: str, evidence_reference: str | None = None) -> None:
        selected = PhysicalGate(target)
        if self.state == selected:
            return
        expected = _GATE_ORDER[_GATE_ORDER.index(self.state) + 1]
        if selected != expected:
            raise PhysicalPreparationError(
                "INVALID_PHYSICAL_GATE_TRANSITION",
                f"Transición no permitida: {self.state.value} -> {selected.value}.",
            )
        previous = self.state
        self.state = selected
        self.history.append({
            "previous_state": previous.value,
            "new_state": selected.value,
            "reason": reason,
            "evidence_reference": evidence_reference,
            "physical_validation": "NOT_EXECUTED",
        })

    def require(self, state: PhysicalGate) -> None:
        if self.state != state:
            raise PhysicalPreparationError(
                "PHYSICAL_GATE_REQUIRED",
                f"Se requiere {state.value}; estado actual {self.state.value}.",
            )


def resolve_aid(select: Callable[[str], Any], mode: str = "SIMULATED") -> dict[str, Any]:
    if mode not in {"SIMULATED", "PHYSICAL"}:
        raise PhysicalPreparationError("INVALID_VALIDATION_MODE", "Modo de validación AID inválido.")
    attempts = []
    for candidate in APPLET_AID_CANDIDATES:
        try:
            response = select(candidate.aid)
            if isinstance(response, str):
                data, sw1sw2 = b"", response
            else:
                data, sw1sw2 = response.data, response.sw1sw2
            result = "APPLET_SELECTED" if sw1sw2 == "9000" else (
                "APPLET_NOT_FOUND" if sw1sw2 == "6A82" else "INVALID_STATUS_WORD"
            )
            attempts.append(SelectAttempt(
                candidate.aid,
                candidate.source,
                build_select_apdu(candidate.aid).hex().upper(),
                data.hex().upper(),
                sw1sw2[:2],
                sw1sw2[2:],
                sw1sw2,
                result,
            ))
        except Exception as error:
            attempts.append(SelectAttempt(
                candidate.aid,
                candidate.source,
                build_select_apdu(candidate.aid).hex().upper(),
                "",
                "",
                "",
                "",
                getattr(error, "code", "SELECT_FAILED"),
            ))
    fatal = next(
        (
            attempt.result
            for attempt in attempts
            if attempt.result in {"TRANSMIT_TIMEOUT", "CARD_REMOVED", "TRANSMIT_FAILED"}
        ),
        None,
    )
    if fatal is not None:
        result, selected, simulated = fatal, None, None
    else:
        result, selected, simulated = decide_aid(attempts, mode)
    return {
        "result": result,
        "selected_aid": selected,
        "simulated_selected_aid": simulated,
        "attempts": [asdict(attempt) for attempt in attempts],
        "mode": mode,
        "recovery": "QUERY_REQUIRED" if fatal is not None else None,
        "physical_validation": "INCONCLUSIVE" if mode == "PHYSICAL" else "NOT_EXECUTED",
    }


def reader_manifest(
    adapter: ReaderAdapter,
    reader_id: str,
    observation: CardObservation | None = None,
) -> dict[str, Any]:
    readers = {reader.reader_id: reader for reader in adapter.enumerate_readers()}
    try:
        reader = readers[reader_id]
    except KeyError as error:
        raise PhysicalPreparationError("READER_NOT_FOUND", "El lector solicitado no está disponible.") from error
    capabilities = adapter.capabilities(reader_id)
    return {
        "reader_id": reader.reader_id,
        "connection_type": reader.connection_type,
        "adapter_name": reader.adapter_name,
        "adapter_version": reader.adapter_version,
        "simulated": reader.simulated,
        "interfaces": {
            name: {
                "state": capability.state.value,
                "source": capability.source,
                "evidence_reference": capability.evidence_reference,
            }
            for name, capability in capabilities.interfaces.items()
        },
        "card_present": observation.card_present if observation else None,
        "atr": observation.atr.hex().upper() if observation and observation.atr else None,
        "protocol": observation.protocol if observation else "UNKNOWN",
        "physical_validation": "NOT_EXECUTED" if reader.simulated else "PENDING",
    }


def prepare_physical_session(
    adapter: ReaderAdapter,
    reader_id: str,
    operation_id: str,
) -> dict[str, Any]:
    events = []
    report: dict[str, Any] = {
        "schema_version": 1,
        "operation_id": operation_id,
        "reader_id": reader_id,
        "mode": "SIMULATED" if adapter.simulated else "PHYSICAL",
        "physical_validation": "NOT_EXECUTED" if adapter.simulated else "INCONCLUSIVE",
        "events": events,
        "selected_aid": None,
        "status": "PREPARED",
    }
    session = None
    try:
        session = adapter.open(reader_id, operation_id)
        observation = adapter.detect_card(session)
        events.append({"stage": "DETECTED", "card_present": observation.card_present})
        report["reader"] = reader_manifest(adapter, reader_id, observation)
        if not observation.card_present:
            report["status"] = "CARD_NOT_PRESENT"
            return report
        connection = adapter.connect(session, "CONTACT")
        events.append({"stage": "CONNECTED", "protocol": connection.protocol})
        report["protocol"] = connection.protocol

        def select(aid: str):
            return adapter.transmit(connection, build_select_apdu(aid))

        resolution = resolve_aid(select, report["mode"])
        report["aid_resolution"] = resolution
        report["selected_aid"] = resolution["selected_aid"]
        events.append({"stage": "SELECT", "result": resolution["result"]})
        if resolution["result"] not in {"PHYSICALLY_CONFIRMED", "SIMULATED_MATCH"}:
            report["status"] = resolution["result"]
            if resolution.get("recovery"):
                report["recovery"] = resolution["recovery"]
                events.append({"stage": "UNCERTAIN", "error_code": resolution["result"]})
            return report
        adapter.close(session)
        session = None
        events.append({"stage": "DISCONNECTED"})
        session = adapter.open(reader_id, f"{operation_id}-reconnect")
        adapter.detect_card(session)
        adapter.close(session)
        session = None
        events.append({"stage": "RECONNECTED"})
        report["status"] = "PREPARED"
        return report
    except ReaderError as error:
        report["status"] = error.code
        report["recovery"] = "QUERY_REQUIRED" if error.outcome == "UNCERTAIN" else "OPERATOR_REVIEW"
        events.append({"stage": "UNCERTAIN" if error.outcome == "UNCERTAIN" else "FAILED", "error_code": error.code})
        return report
    finally:
        if session is not None:
            try:
                adapter.close(session)
                events.append({"stage": "DISCONNECTED"})
            except ReaderError as error:
                events.append({"stage": "DISCONNECT_FAILED", "error_code": error.code})


class PersonalizationVerifier:
    def __init__(self, gate: PhysicalGateMachine | None = None):
        self.gate = gate or PhysicalGateMachine()

    def personalize(
        self,
        payload: bytes,
        write: Callable[[bytes], Any],
        read_back: Callable[[], bytes],
        verify: Callable[[bytes, bytes], bool],
        *,
        authorized: bool,
        evidence_reference: str = "synthetic-write",
    ) -> dict[str, Any]:
        self.gate.require(PhysicalGate.APPLET_READY)
        if not authorized:
            raise PhysicalPreparationError(
                "WRITE_NOT_AUTHORIZED",
                "La escritura requiere autorización explícita del operador.",
            )
        self.gate.advance(PhysicalGate.PERSONALIZING, "AUTHORIZED_SYNTHETIC_WRITE", evidence_reference)
        write(bytes(payload))
        returned = read_back()
        if returned != payload or not verify(payload, returned):
            raise PhysicalPreparationError(
                "PERSONALIZATION_VERIFY_FAILED",
                "WRITE/READ-BACK/COMPARE/VERIFY no coinciden.",
            )
        self.gate.advance(PhysicalGate.PERSONALIZED, "WRITE_READ_BACK_COMPARE_PASS", evidence_reference)
        self.gate.advance(PhysicalGate.VERIFIED, "VERIFY_PASS", evidence_reference)
        return {
            "write": "PASS",
            "read_back": "PASS",
            "compare": "PASS",
            "verify": "PASS",
            "state": self.gate.state.value,
            "physical_validation": "NOT_EXECUTED",
        }

    def register(self, evidence_reference: str) -> None:
        self.gate.require(PhysicalGate.VERIFIED)
        self.gate.advance(PhysicalGate.REGISTERED, "REGISTRATION_EVIDENCE_ACCEPTED", evidence_reference)

    def bind_token(self, evidence_reference: str) -> None:
        self.gate.require(PhysicalGate.REGISTERED)
        self.gate.advance(PhysicalGate.TOKEN_BOUND, "SYNTHETIC_TOKEN_BOUND", evidence_reference)

    def activate(self, evidence_reference: str) -> None:
        self.gate.require(PhysicalGate.TOKEN_BOUND)
        self.gate.advance(PhysicalGate.ACTIVE, "SYNTHETIC_ACTIVATION", evidence_reference)
