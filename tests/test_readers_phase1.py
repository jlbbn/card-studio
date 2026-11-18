import unittest

from card_studio.backends import CardBackend
from card_studio.interfaces import (
    ContactInterface,
    ContactlessInterface,
    InterfaceKind,
    InterfaceState,
    MagneticSimulatedInterface,
    VirtualInterface,
)
from card_studio.model import CardError, CardState, read_apdu
from card_studio.pcsc import PcscBackend
from card_studio.readers import (
    Capability,
    CapabilityState,
    MockReaderAdapter,
    PcscReaderAdapter,
    ReaderError,
    TransportResult,
    VirtualReaderAdapter,
)


class FakeResponse:
    def __init__(self, data=b"respuesta", sw1sw2="9000"):
        self.data = data
        self.sw1sw2 = sw1sw2


class FakeLegacyPcsc:
    mode = "SIMULATED"

    def __init__(self):
        self.reader_name = ""
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.commands = []
        self.transmit_error = None

    def listReaders(self):
        return ["Fake PCSC"]

    def connect(self):
        self.connect_calls += 1
        return self

    def getATR(self):
        return bytes.fromhex("3B0000")

    def transmit(self, command):
        self.commands.append(command)
        if self.transmit_error:
            raise self.transmit_error
        return FakeResponse()

    def disconnect(self):
        self.disconnect_calls += 1


class ReaderContractTests(unittest.TestCase):
    def test_mock_reader_full_contract(self):
        adapter = MockReaderAdapter(responses={
            "00A4040000": TransportResult(b"simulado", "9000"),
        })
        info = adapter.enumerate_readers()[0]
        session = adapter.open(info.reader_id, "operation-1")
        self.assertTrue(session.simulated)
        self.assertEqual(session.operation_id, "operation-1")
        self.assertTrue(adapter.detect_card(session).card_present)
        connection = adapter.connect(session, InterfaceKind.CONTACT)
        result = adapter.transmit(connection, "00A4040000")
        self.assertEqual((result.data, result.sw1sw2), (b"simulado", "9000"))
        self.assertTrue(adapter.card_present(session))
        self.assertTrue(adapter.close(connection).closed)
        self.assertTrue(adapter.close(session).closed)
        with self.assertRaises(ReaderError):
            adapter.transmit(connection, "00A4040000")

    def test_unknown_capability_blocks_connection(self):
        adapter = MockReaderAdapter({InterfaceKind.CONTACT.value: CapabilityState.UNKNOWN})
        session = adapter.open("mock-reader-1", "operation-unknown")
        with self.assertRaises(ReaderError) as caught:
            adapter.connect(session, InterfaceKind.CONTACT)
        self.assertEqual(caught.exception.code, "CAPABILITY_UNKNOWN")

    def test_virtual_reader_rejects_physical_interfaces(self):
        adapter = VirtualReaderAdapter()
        session = adapter.open("virtual-reader-1", "operation-virtual")
        connection = adapter.connect(session, InterfaceKind.VIRTUAL)
        self.assertTrue(connection.simulated)
        with self.assertRaises(ReaderError) as caught:
            adapter.connect(session, InterfaceKind.CONTACT)
        self.assertEqual(caught.exception.code, "UNSUPPORTED_INTERFACE")

    def test_mock_timeout_is_uncertain_and_card_removal_is_explicit(self):
        adapter = MockReaderAdapter()
        session = adapter.open("mock-reader-1", "operation-timeout")
        connection = adapter.connect(session, InterfaceKind.CONTACTLESS)
        adapter.fail_next_transmit = "TRANSMIT_TIMEOUT"
        with self.assertRaises(ReaderError) as caught:
            adapter.transmit(connection, "00A4040000")
        self.assertEqual(caught.exception.outcome, "UNCERTAIN")
        adapter.fail_next_transmit = "CARD_REMOVED"
        with self.assertRaises(ReaderError) as caught:
            adapter.transmit(connection, "00A4040000")
        self.assertEqual(caught.exception.code, "CARD_REMOVED")
        self.assertFalse(adapter.card_present(session))


class InterfaceTests(unittest.TestCase):
    def test_all_interface_abstractions_use_the_reader_contract(self):
        classes = [
            ContactInterface,
            ContactlessInterface,
            VirtualInterface,
            MagneticSimulatedInterface,
        ]
        for interface_class in classes:
            with self.subTest(interface=interface_class.__name__):
                adapter = MockReaderAdapter()
                session = adapter.open("mock-reader-1", "operation-interface")
                interface = interface_class(adapter, session).open()
                self.assertEqual(interface.state, InterfaceState.OPEN)
                self.assertEqual(interface.exchange("00A4040000").sw1sw2, "9000")
                interface.close()
                self.assertEqual(interface.state, InterfaceState.CLOSED)
                interface.close()
        self.assertTrue(VirtualInterface.simulated)
        self.assertTrue(MagneticSimulatedInterface.simulated)


class PcscWrapperTests(unittest.TestCase):
    def test_pcsc_defaults_to_unknown_without_touching_legacy_connection(self):
        backend = FakeLegacyPcsc()
        adapter = PcscReaderAdapter(backend)
        session = adapter.open("Fake PCSC", "operation-pcsc")
        with self.assertRaises(ReaderError) as caught:
            adapter.connect(session, InterfaceKind.CONTACT)
        self.assertEqual(caught.exception.code, "CAPABILITY_UNKNOWN")
        self.assertEqual(backend.connect_calls, 0)

    def test_pcsc_wrapper_delegates_after_explicit_capability(self):
        backend = FakeLegacyPcsc()
        adapter = PcscReaderAdapter(backend, interface_capabilities={
            InterfaceKind.CONTACT.value: Capability(
                CapabilityState.SUPPORTED,
                source="MOCK_DECLARED",
                evidence_reference="test://pcsc/capability",
            ),
        })
        session = adapter.open("Fake PCSC", "operation-pcsc")
        connection = adapter.connect(session, InterfaceKind.CONTACT)
        result = adapter.transmit(connection, "00A4040000")
        self.assertEqual((result.data, result.sw1sw2), (b"respuesta", "9000"))
        self.assertEqual(backend.commands, ["00A4040000"])
        self.assertEqual(adapter.detect_card(session).atr, bytes.fromhex("3B0000"))
        self.assertEqual(backend.disconnect_calls, 0)
        adapter.close(session)
        self.assertEqual(backend.disconnect_calls, 1)

    def test_pcsc_timeout_preserves_uncertain_outcome(self):
        backend = FakeLegacyPcsc()
        backend.transmit_error = CardError("TRANSMIT_TIMEOUT", "SIMULATED: timeout")
        adapter = PcscReaderAdapter(backend, interface_capabilities={
            InterfaceKind.CONTACT.value: Capability(CapabilityState.SUPPORTED, source="MOCK_DECLARED"),
        })
        session = adapter.open("Fake PCSC", "operation-pcsc-timeout")
        connection = adapter.connect(session, InterfaceKind.CONTACT)
        with self.assertRaises(ReaderError) as caught:
            adapter.transmit(connection, "00A4040000")
        self.assertEqual(caught.exception.outcome, "UNCERTAIN")


class LegacyCompatibilityTests(unittest.TestCase):
    def test_legacy_classes_and_applet_contract_remain_available(self):
        state = CardState("SL-CAJA-001", 0)
        self.assertEqual(state.currency, "MXN")
        self.assertEqual(read_apdu(), "80CA000200")
        self.assertTrue(issubclass(PcscBackend, CardBackend))
        self.assertFalse(hasattr(CardState, "profile_digest"))


if __name__ == "__main__":
    unittest.main()
