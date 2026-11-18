from __future__ import annotations

from .base import CardInterface, InterfaceKind


class ContactInterface(CardInterface):
    kind = InterfaceKind.CONTACT
    simulated = False

    def interface_kind(self) -> InterfaceKind:
        return self.kind
