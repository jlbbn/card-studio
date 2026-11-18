from __future__ import annotations

from .base import CardInterface, InterfaceKind


class ContactlessInterface(CardInterface):
    kind = InterfaceKind.CONTACTLESS
    simulated = False

    def interface_kind(self) -> InterfaceKind:
        return self.kind
