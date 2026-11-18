from __future__ import annotations

import time
import uuid
from typing import Any, Mapping

from card_studio.card_session import CardSession
from card_studio.interfaces.base import InterfaceKind
from card_studio.model import CardError
from card_studio.pcsc import create_pcsc_backend

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


class PcscReaderAdapter(ReaderAdapter):
    adapter_name = "pcsc-reader"
    adapter_version = "1"

    def __init__(
        self,
        backend: Any | None = None,
        reader_name: str = "",
        interface_capabilities: Mapping[str, Capability] | None = None,
    ):
        self.backend = backend or create_pcsc_backend(reader_name=reader_name)
        self.card_session = CardSession(self.backend)
        self._physical_owner: str | None = None
        self.simulated = self.backend.mode == "SIMULATED"
        declared = dict(interface_capabilities or {})
        self._capabilities = ReaderCapabilities({
            InterfaceKind.CONTACT.value: declared.get(
                InterfaceKind.CONTACT.value,
                Capability(CapabilityState.UNKNOWN),
            ),
            InterfaceKind.CONTACTLESS.value: declared.get(
                InterfaceKind.CONTACTLESS.value,
                Capability(CapabilityState.UNKNOWN),
            ),
            InterfaceKind.VIRTUAL.value: Capability(CapabilityState.UNSUPPORTED),
            InterfaceKind.MAGNETIC_SIMULATED.value: Capability(CapabilityState.UNSUPPORTED),
        })
        self._sessions: dict[str, ReaderSession] = {}
        self._closed: set[str] = set()
        self._connections: dict[str, CardConnection] = {}

    def enumerate_readers(self) -> list[ReaderInfo]:
        try:
            names = self.backend.listReaders()
        except CardError as error:
            raise ReaderError(error.code, error.message) from error
        return [
            ReaderInfo(
                reader_id=name,
                display_name=name,
                connection_type="PCSC",
                adapter_name=self.adapter_name,
                adapter_version=self.adapter_version,
                simulated=self.simulated,
            )
            for name in names
        ]

    def capabilities(self, reader_id: str | None = None) -> ReaderCapabilities:
        return self._capabilities

    def open(self, reader_id: str, operation_id: str, deadline: float | None = None) -> ReaderSession:
        if deadline is not None and deadline <= time.monotonic():
            raise ReaderError("READER_TIMEOUT", "El plazo PC/SC expiró antes de abrir.")
        if reader_id not in {item.reader_id for item in self.enumerate_readers()}:
            raise ReaderError("READER_NOT_FOUND", "No se encontró el lector PC/SC seleccionado.")
        self.backend.reader_name = reader_id
        session = ReaderSession(str(uuid.uuid4()), reader_id, operation_id, self.simulated)
        self._sessions[session.session_id] = session
        return session

    def _require_open(self, session: ReaderSession) -> None:
        if session.session_id not in self._sessions or session.session_id in self._closed:
            raise ReaderError("SESSION_CLOSED", "La sesión PC/SC no está abierta.")

    @staticmethod
    def _raise(error: CardError) -> None:
        outcome = "UNCERTAIN" if error.code == "TRANSMIT_TIMEOUT" else "CERTAIN_FAILURE"
        raise ReaderError(error.code, error.message, outcome=outcome) from error

    def _claim_physical_session(self, session: ReaderSession) -> None:
        if self._physical_owner is None:
            self._physical_owner = session.session_id
            return

        if self._physical_owner != session.session_id:
            raise ReaderError(
                "CARD_SESSION_BUSY",
                "Otra sesión PC/SC ya es dueña de la conexión física.",
            )

    def detect_card(self, session: ReaderSession) -> CardObservation:
        self._require_open(session)
        self._claim_physical_session(session)

        try:
            self.card_session.open()
            atr = self.backend.getATR()
            try:
                protocol = self.backend.getProtocol()
            except AttributeError:
                protocol = "UNKNOWN"

            return CardObservation(
                True,
                atr=atr,
                protocol=protocol,
                simulated=self.simulated,
            )

        except CardError as error:
            if error.code == "CARD_NOT_PRESENT":
                if self._physical_owner == session.session_id:
                    self._physical_owner = None
                return CardObservation(False, simulated=self.simulated)
            self._raise(error)

        raise AssertionError("inaccesible")

    def connect(self, session: ReaderSession, interface: InterfaceKind | str) -> CardConnection:
        self._require_open(session)
        selected = interface.value if isinstance(interface, InterfaceKind) else interface.upper()
        self.require_capability(session.reader_id, selected)
        if session.session_id in self._connections:
            return self._connections[session.session_id]
        self._claim_physical_session(session)
        try:
            self.card_session.open()
        except CardError as error:
            self._raise(error)
        if selected == InterfaceKind.CONTACT.value:
            try:
                protocol = self.backend.getProtocol()
            except AttributeError:
                protocol = "UNKNOWN"
        else:
            protocol = selected
        connection = CardConnection(session.session_id, session.reader_id, selected, self.simulated, protocol=protocol)
        self._connections[session.session_id] = connection
        return connection

    def transmit(
        self,
        connection: CardConnection,
        command: bytes | str,
        sensitivity: str = "REDACTED",
    ) -> TransportResult:
        if connection.session_id not in self._connections:
            raise ReaderError("CARD_CONNECTION_FAILED", "La conexión PC/SC no está abierta.")
        try:
            response = self.card_session.transmit(command)
            return TransportResult(response.data, response.sw1sw2)
        except CardError as error:
            self._raise(error)
        raise AssertionError("inaccesible")

    def card_present(self, session: ReaderSession) -> bool:
        return self.detect_card(session).card_present

    def close(self, session: ReaderSession | CardConnection) -> CloseEvidence:
        session_id = session.session_id
        if session_id not in self._sessions:
            raise ReaderError("SESSION_NOT_FOUND", "La sesión PC/SC no existe.")
        if session_id not in self._closed:
            if self._physical_owner == session_id:
                try:
                    self.card_session.close()
                except CardError as error:
                    self._raise(error)
                self._physical_owner = None

            self._closed.add(session_id)
            self._connections.pop(session_id, None)
        return CloseEvidence(session_id, closed=True, simulated=self.simulated)
