from __future__ import annotations

from .base import CardInterface, InterfaceKind


class MagneticSimulatedInterface(CardInterface):
    kind = InterfaceKind.MAGNETIC_SIMULATED
    simulated = True

    def interface_kind(self) -> InterfaceKind:
        return self.kind
