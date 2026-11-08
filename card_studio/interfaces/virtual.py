from __future__ import annotations

from .base import CardInterface, InterfaceKind


class VirtualInterface(CardInterface):
    kind = InterfaceKind.VIRTUAL
    simulated = True

    def interface_kind(self) -> InterfaceKind:
        return self.kind
