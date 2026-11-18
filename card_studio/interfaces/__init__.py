from .base import CardInterface, InterfaceKind, InterfaceState
from .contact import ContactInterface
from .contactless import ContactlessInterface
from .magnetic_simulated import MagneticSimulatedInterface
from .virtual import VirtualInterface

__all__ = [
    "CardInterface",
    "ContactInterface",
    "ContactlessInterface",
    "InterfaceKind",
    "InterfaceState",
    "MagneticSimulatedInterface",
    "VirtualInterface",
]
