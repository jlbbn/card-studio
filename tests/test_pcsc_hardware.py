import unittest

from card_studio.model import CardError
from card_studio.pcsc import LinuxPcscBackend


class FakeError(Exception):
    def __init__(self, hresult):
        super().__init__("physical error")
        self.hresult = hresult


class FakeConnection:
    def __init__(self, connect_error=None, transmit_error=None, invalid_sw=False):
        self.connect_error = connect_error
        self.transmit_error = transmit_error
        self.invalid_sw = invalid_sw
        self.disconnected = False
        self.commands = []

    def connect(self):
        if self.connect_error:
            raise self.connect_error

    def getATR(self):
        return [0x3B, 0x00]

    def transmit(self, command):
        self.commands.append(bytes(command).hex().upper())
        if self.transmit_error:
            raise self.transmit_error
        if self.invalid_sw:
            return [], None, None
        return [0x01], 0x90, 0x00

    def disconnect(self):
        self.disconnected = True


class FakeReader:
    def __init__(self, connection=None, name="Rocketek CSCR3"):
        self.connection = connection or FakeConnection()
        self.name = name

    def __str__(self):
        return self.name

    def createConnection(self):
        return self.connection


class PcscHardwareTests(unittest.TestCase):
    def backend(self, connection=None, readers=None):
        readers = readers if readers is not None else [FakeReader(connection)]
        return LinuxPcscBackend(reader_provider=lambda: readers)

    def test_full_session_and_simulated_mode(self):
        connection = FakeConnection()
        backend = self.backend(connection)
        self.assertEqual(backend.listReaders(), ["Rocketek CSCR3"])
        self.assertEqual(backend.mode, "SIMULATED")
        backend.connect()
        self.assertEqual(backend.getReader(), "Rocketek CSCR3")
        self.assertEqual(backend.getATR(), b"\x3b\x00")
        selected = backend.selectApplet()
        self.assertEqual(selected.sw1sw2, "9000")
        self.assertEqual(connection.commands[0], "00A4040007D4534C504D4401")
        response = backend.transmit("80CA000200")
        self.assertEqual(response.data, b"\x01")
        backend.disconnect()
        self.assertTrue(connection.disconnected)

    def test_reader_absent(self):
        backend = self.backend(readers=[])
        with self.assertRaises(CardError) as caught:
            backend.connect()
        self.assertEqual(caught.exception.code, "READER_NOT_FOUND")

    def test_card_absent(self):
        backend = self.backend(FakeConnection(connect_error=FakeError(0x8010000C)))
        with self.assertRaises(CardError) as caught:
            backend.waitForCard()
        self.assertEqual(caught.exception.code, "CARD_NOT_PRESENT")

    def test_card_removed(self):
        backend = self.backend(FakeConnection(transmit_error=FakeError(0x80100069)))
        backend.connect()
        with self.assertRaises(CardError) as caught:
            backend.transmit("00A4040000")
        self.assertEqual(caught.exception.code, "CARD_REMOVED")

    def test_transmit_timeout(self):
        backend = self.backend(FakeConnection(transmit_error=FakeError(0x8010000A)))
        backend.connect()
        with self.assertRaises(CardError) as caught:
            backend.transmit("00A4040000")
        self.assertEqual(caught.exception.code, "TRANSMIT_TIMEOUT")

    def test_transmit_failure(self):
        backend = self.backend(FakeConnection(transmit_error=RuntimeError("boom")))
        backend.connect()
        with self.assertRaises(CardError) as caught:
            backend.transmit("00A4040000")
        self.assertEqual(caught.exception.code, "TRANSMIT_FAILED")

    def test_invalid_response_and_invalid_apdu(self):
        backend = self.backend(FakeConnection(invalid_sw=True))
        backend.connect()
        with self.assertRaises(CardError) as caught:
            backend.transmit("00A4040000")
        self.assertEqual(caught.exception.code, "INVALID_RESPONSE")
        with self.assertRaises(CardError) as caught:
            backend.transmit("A4")
        self.assertEqual(caught.exception.code, "INVALID_APDU")

    def test_session_is_required(self):
        backend = self.backend()
        with self.assertRaises(CardError) as caught:
            backend.getATR()
        self.assertEqual(caught.exception.code, "CARD_CONNECTION_FAILED")


if __name__ == "__main__":
    unittest.main()



def test_physical_connect_does_not_force_t0_protocol():
    class NegotiatingConnection:
        def __init__(self):
            self.connect_calls = []

        def connect(self, *args, **kwargs):
            self.connect_calls.append(
                {
                    "args": args,
                    "kwargs": kwargs,
                }
            )

        def getProtocol(self):
            return 2

        def getATR(self):
            return [0x3B, 0x00]

        def disconnect(self):
            pass

    connection = NegotiatingConnection()
    backend = LinuxPcscBackend(
        reader_provider=lambda: [
            FakeReader(connection)
        ]
    )

    backend.mode = "PHYSICAL"

    backend.connect()

    assert connection.connect_calls == [
        {
            "args": (),
            "kwargs": {},
        }
    ]

    assert backend.getProtocol() == "T=1"

    backend.disconnect()
