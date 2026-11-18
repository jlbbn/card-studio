from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from card_studio.readers.base import CardConnection, ReaderAdapter, ReaderSession, TransportResult


class InterfaceKind(str, Enum):
    CONTACT = "CONTACT"
    CONTACTLESS = "CONTACTLESS"
    VIRTUAL = "VIRTUAL"
    MAGNETIC_SIMULATED = "MAGNETIC_SIMULATED"


class InterfaceState(str, Enum):
    NEW = "NEW"
    OPEN = "OPEN"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


class CardInterface(ABC):
    kind: InterfaceKind
    simulated = False

    def __init__(self, adapter: "ReaderAdapter", session: "ReaderSession"):
        self.adapter = adapter
        self.session = session
        self.connection: CardConnection | None = None
        self.state = InterfaceState.NEW

    def open(self) -> "CardInterface":
        if self.state == InterfaceState.OPEN:
            return self
        if self.state != InterfaceState.NEW:
            raise RuntimeError("La interfaz no puede reabrirse después de cancelarse o cerrarse.")
        self.connection = self.adapter.connect(self.session, self.kind)
        self.state = InterfaceState.OPEN
        return self

    def exchange(self, command: bytes | str, sensitivity: str = "REDACTED") -> "TransportResult":
        if self.state != InterfaceState.OPEN or self.connection is None:
            raise RuntimeError("La interfaz no está abierta.")
        return self.adapter.transmit(self.connection, command, sensitivity)

    def cancel(self) -> None:
        if self.state in {InterfaceState.NEW, InterfaceState.OPEN}:
            self.state = InterfaceState.CANCELLED
            self.close()

    def close(self) -> None:
        if self.state == InterfaceState.CLOSED:
            return
        try:
            self.adapter.close(self.session)
        finally:
            self.state = InterfaceState.CLOSED

    @abstractmethod
    def interface_kind(self) -> InterfaceKind:
        return self.kind
