from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from card_studio.interfaces.base import InterfaceKind


class ReaderError(Exception):
    def __init__(self, code: str, message: str, outcome: str = "CERTAIN_FAILURE"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.outcome = outcome


class CapabilityState(str, Enum):
    UNKNOWN = "UNKNOWN"
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class Capability:
    state: CapabilityState
    source: str = "NONE"
    evidence_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", CapabilityState(self.state))
        if self.state == CapabilityState.SUPPORTED and self.source == "NONE":
            raise ValueError("Una capacidad SUPPORTED necesita una fuente explícita.")


@dataclass(frozen=True)
class ReaderCapabilities:
    interfaces: Mapping[str, Capability]

    def __post_init__(self) -> None:
        values: dict[str, Capability] = {}
        for key, value in self.interfaces.items():
            interface = key.value if isinstance(key, InterfaceKind) else str(key).upper()
            InterfaceKind(interface)
            values[interface] = value if isinstance(value, Capability) else Capability(CapabilityState(value))
        object.__setattr__(self, "interfaces", MappingProxyType(values))

    def for_interface(self, interface: InterfaceKind | str) -> Capability:
        selected = interface.value if isinstance(interface, InterfaceKind) else interface.upper()
        return self.interfaces.get(selected, Capability(CapabilityState.UNKNOWN))


@dataclass(frozen=True)
class ReaderInfo:
    reader_id: str
    display_name: str
    connection_type: str
    adapter_name: str
    adapter_version: str
    simulated: bool


@dataclass(frozen=True)
class ReaderSession:
    session_id: str
    reader_id: str
    operation_id: str
    simulated: bool


@dataclass(frozen=True)
class CardObservation:
    card_present: bool
    atr: bytes | None = None
    protocol: str = "UNKNOWN"
    simulated: bool = True


@dataclass(frozen=True)
class CardConnection:
    session_id: str
    reader_id: str
    interface: str
    simulated: bool
    protocol: str = "UNKNOWN"


@dataclass(frozen=True)
class TransportResult:
    data: bytes = b""
    sw1sw2: str | None = None


@dataclass(frozen=True)
class CloseEvidence:
    session_id: str
    closed: bool
    simulated: bool


class ReaderAdapter(ABC):
    adapter_name = "abstract"
    adapter_version = "1"
    simulated = False

    @abstractmethod
    def enumerate_readers(self) -> list[ReaderInfo]:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self, reader_id: str | None = None) -> ReaderCapabilities:
        raise NotImplementedError

    def require_capability(self, reader_id: str, interface: InterfaceKind | str) -> Capability:
        selected = interface.value if isinstance(interface, InterfaceKind) else interface.upper()
        capability = self.capabilities(reader_id).for_interface(selected)
        if capability.state == CapabilityState.UNKNOWN:
            raise ReaderError(
                "CAPABILITY_UNKNOWN",
                f"La capacidad {selected} es desconocida; no se asumirá compatibilidad.",
            )
        if capability.state == CapabilityState.UNSUPPORTED:
            raise ReaderError("UNSUPPORTED_INTERFACE", f"La interfaz {selected} no está soportada.")
        return capability

    @abstractmethod
    def open(self, reader_id: str, operation_id: str, deadline: float | None = None) -> ReaderSession:
        raise NotImplementedError

    @abstractmethod
    def detect_card(self, session: ReaderSession) -> CardObservation:
        raise NotImplementedError

    @abstractmethod
    def connect(self, session: ReaderSession, interface: InterfaceKind | str) -> CardConnection:
        raise NotImplementedError

    @abstractmethod
    def transmit(
        self,
        connection: CardConnection,
        command: bytes | str,
        sensitivity: str = "REDACTED",
    ) -> TransportResult:
        raise NotImplementedError

    @abstractmethod
    def card_present(self, session: ReaderSession) -> bool:
        raise NotImplementedError

    @abstractmethod
    def close(self, session: ReaderSession | CardConnection) -> CloseEvidence:
        raise NotImplementedError
