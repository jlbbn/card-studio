from __future__ import annotations

import time
import uuid
from typing import Mapping

from card_studio.interfaces.base import InterfaceKind

from .base import (
    Capability,
    CapabilityState,
    CardConnection,
    CardObservation,
    CloseEvidence,
    ReaderAdapter,
    ReaderCapabilities,
    ReaderError,
    ReaderInfo,
    ReaderSession,
    TransportResult,
)


class MockReaderAdapter(ReaderAdapter):
    adapter_name = "mock-reader"
    adapter_version = "1"
    simulated = True

    def __init__(
        self,
        interface_states: Mapping[str, CapabilityState | str] | None = None,
        responses: Mapping[str, TransportResult | bytes] | None = None,
        card_available: bool = True,
        contact_protocol: str = "T=0",
    ):
        if contact_protocol not in {"T=0", "T=1", "UNKNOWN"}:
            raise ValueError("contact_protocol debe ser T=0, T=1 o UNKNOWN.")
        self.contact_protocol = contact_protocol
        states = dict(interface_states or {})
        capabilities = {}
        for interface in InterfaceKind:
            raw_state = states.get(interface.value, CapabilityState.SUPPORTED)
            state = CapabilityState(raw_state)
            source = "MOCK_DECLARED" if state == CapabilityState.SUPPORTED else "NONE"
            capabilities[interface.value] = Capability(state, source=source)
        self._capabilities = ReaderCapabilities(capabilities)
        self.reader_info = ReaderInfo(
            reader_id="mock-reader-1",
            display_name="Lector mock · SIMULATED",
            connection_type="MOCK",
            adapter_name=self.adapter_name,
            adapter_version=self.adapter_version,
            simulated=True,
        )
        self.responses = {key.upper(): value for key, value in (responses or {}).items()}
        self.card_available = card_available
        self.fail_next_transmit: str | None = None
        self._sessions: dict[str, ReaderSession] = {}
        self._closed: set[str] = set()
        self._connections: dict[str, CardConnection] = {}

    def enumerate_readers(self) -> list[ReaderInfo]:
        return [self.reader_info]

    def capabilities(self, reader_id: str | None = None) -> ReaderCapabilities:
        return self._capabilities

    def open(self, reader_id: str, operation_id: str, deadline: float | None = None) -> ReaderSession:
        if reader_id != self.reader_info.reader_id:
            raise ReaderError("READER_NOT_FOUND", "Lector mock no encontrado.")
        if deadline is not None and deadline <= time.monotonic():
            raise ReaderError("READER_TIMEOUT", "El plazo del lector mock expiró antes de abrir.")
        session = ReaderSession(str(uuid.uuid4()), reader_id, operation_id, simulated=True)
        self._sessions[session.session_id] = session
        return session

    def _require_open(self, session: ReaderSession) -> None:
        if session.session_id not in self._sessions or session.session_id in self._closed:
            raise ReaderError("SESSION_CLOSED", "La sesión mock no está abierta.")

    def detect_card(self, session: ReaderSession) -> CardObservation:
        self._require_open(session)
        return CardObservation(
            card_present=self.card_available,
            atr=bytes.fromhex("3B0000") if self.card_available else None,
            simulated=True,
        )

    def connect(self, session: ReaderSession, interface: InterfaceKind | str) -> CardConnection:
        self._require_open(session)
        selected = interface.value if isinstance(interface, InterfaceKind) else interface.upper()
        self.require_capability(session.reader_id, selected)
        if not self.card_available:
            raise ReaderError("CARD_NOT_PRESENT", "SIMULATED: tarjeta ausente.")
        protocol = self.contact_protocol if selected == InterfaceKind.CONTACT.value else selected
        connection = CardConnection(session.session_id, session.reader_id, selected, simulated=True, protocol=protocol)
        self._connections[session.session_id] = connection
        return connection

    def transmit(
        self,
        connection: CardConnection,
        command: bytes | str,
        sensitivity: str = "REDACTED",
    ) -> TransportResult:
        session = self._sessions.get(connection.session_id)
        if session is None:
            raise ReaderError("SESSION_CLOSED", "La conexión mock no existe.")
        self._require_open(session)
        if self._connections.get(connection.session_id) != connection:
            raise ReaderError("CARD_CONNECTION_FAILED", "La conexión mock no está abierta.")
        if self.fail_next_transmit:
            code = self.fail_next_transmit
            self.fail_next_transmit = None
            if code == "CARD_REMOVED":
                self.card_available = False
            outcome = "UNCERTAIN" if code == "TRANSMIT_TIMEOUT" else "CERTAIN_FAILURE"
            raise ReaderError(code, f"SIMULATED: {code}.", outcome=outcome)
        try:
            raw = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
        except (TypeError, ValueError) as exc:
            raise ReaderError("INVALID_APDU", "El comando no contiene bytes hexadecimales válidos.") from exc
        if len(raw) < 4:
            raise ReaderError("INVALID_APDU", "El comando debe incluir al menos CLA, INS, P1 y P2.")
        response = self.responses.get(raw.hex().upper(), TransportResult(sw1sw2="9000"))
        if isinstance(response, bytes):
            return TransportResult(response, "9000")
        return response

    def card_present(self, session: ReaderSession) -> bool:
        self._require_open(session)
        return self.card_available

    def close(self, session: ReaderSession | CardConnection) -> CloseEvidence:
        session_id = session.session_id
        if session_id not in self._sessions:
            raise ReaderError("SESSION_NOT_FOUND", "La sesión mock no existe.")
        self._closed.add(session_id)
        self._connections.pop(session_id, None)
        return CloseEvidence(session_id, closed=True, simulated=True)
