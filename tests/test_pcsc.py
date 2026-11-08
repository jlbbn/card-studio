import unittest

from card_studio.model import CardError
from card_studio.pcsc import (
    LinuxPcscBackend,
    MacOSPcscBackend,
    SELECT_APPLET_APDU,
    create_pcsc_backend,
)


class FakeConnection:
    def __init__(self, select_sw=0x9000):
        self.select_sw = select_sw
        self.commands = []
        self.disconnected = False

    def connect(self):
        return None

    def transmit(self, command):
        self.commands.append(bytes(command).hex().upper())
        if self.commands[-1] == SELECT_APPLET_APDU:
            return [], self.select_sw >> 8, self.select_sw & 0xFF
        response = b"SL1|SL-CAJA-001|00000CCB|MXN"
        return list(response), 0x90, 0x00

    def disconnect(self):
        self.disconnected = True


class FakeReader:
    def __init__(self, name="Lector simulado", select_sw=0x9000):
        self.name = name
        self.connection = FakeConnection(select_sw)

    def __str__(self):
        return self.name

    def createConnection(self):
        return self.connection


class PcscTests(unittest.TestCase):
    def test_factory_has_independent_linux_and_macos_adapters(self):
        self.assertIsInstance(create_pcsc_backend("linux"), LinuxPcscBackend)
        self.assertIsInstance(create_pcsc_backend("darwin"), MacOSPcscBackend)
        self.assertIsInstance(create_pcsc_backend("macos"), MacOSPcscBackend)

    def test_linux_and_macos_read_own_applet_and_disconnect(self):
        for platform in ["linux", "darwin"]:
            with self.subTest(platform=platform):
                reader = FakeReader()
                backend = create_pcsc_backend(platform, reader_provider=lambda: [reader])
                state = backend.read()
                self.assertEqual(state.balance_minor, 3275)
                self.assertEqual(reader.connection.commands[0], SELECT_APPLET_APDU)
                self.assertEqual(reader.connection.commands[1], "80CA000200")
                self.assertTrue(reader.connection.disconnected)

    def test_detects_and_selects_named_reader(self):
        first = FakeReader("Lector A")
        second = FakeReader("Lector B")
        backend = MacOSPcscBackend("Lector B", reader_provider=lambda: [first, second])
        self.assertEqual(backend.list_readers(), ["Lector A", "Lector B"])
        backend.read()
        self.assertFalse(first.connection.disconnected)
        self.assertTrue(second.connection.disconnected)

    def test_reports_no_reader_and_missing_named_reader(self):
        with self.assertRaises(CardError) as caught:
            LinuxPcscBackend(reader_provider=list).read()
        self.assertEqual(caught.exception.code, "NO_READER")
        with self.assertRaises(CardError) as caught:
            MacOSPcscBackend("Otro", reader_provider=lambda: [FakeReader()]).read()
        self.assertEqual(caught.exception.code, "READER_NOT_FOUND")

    def test_rejects_card_without_own_applet(self):
        reader = FakeReader(select_sw=0x6A82)
        with self.assertRaises(CardError) as caught:
            LinuxPcscBackend(reader_provider=lambda: [reader]).read()
        self.assertEqual(caught.exception.code, "INCOMPATIBLE_CARD")
        self.assertTrue(reader.connection.disconnected)


if __name__ == "__main__":
    unittest.main()
