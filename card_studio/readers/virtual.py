from __future__ import annotations

from card_studio.interfaces.base import InterfaceKind

from .base import CapabilityState, ReaderInfo
from .mock import MockReaderAdapter


class VirtualReaderAdapter(MockReaderAdapter):
    adapter_name = "virtual-reader"
    adapter_version = "1"
    simulated = True

    def __init__(self, responses=None):
        super().__init__(
            interface_states={
                InterfaceKind.VIRTUAL.value: CapabilityState.SUPPORTED,
                InterfaceKind.CONTACT.value: CapabilityState.UNSUPPORTED,
                InterfaceKind.CONTACTLESS.value: CapabilityState.UNSUPPORTED,
                InterfaceKind.MAGNETIC_SIMULATED.value: CapabilityState.UNSUPPORTED,
            },
            responses=responses,
        )
        self.reader_info = ReaderInfo(
            reader_id="virtual-reader-1",
            display_name="Lector virtual · SIMULATED",
            connection_type="VIRTUAL",
            adapter_name=self.adapter_name,
            adapter_version=self.adapter_version,
            simulated=True,
        )
