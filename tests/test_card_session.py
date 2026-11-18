from card_studio.card_session import CardSession, CardSessionError


class FakeBackend:
    def __init__(self):
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.transmit_calls = []
        self.reader_name = "Fake Reader"
        self.protocol = "T=0"
        self.atr = "3B6A00FF0031C173C84000009000"

    def connect(self):
        self.connect_calls += 1
        return self

    def transmit(self, command):
        self.transmit_calls.append(command)
        return b"\x90\x00"

    def disconnect(self):
        self.disconnect_calls += 1


def test_single_open_does_not_reconnect():
    backend = FakeBackend()
    session = CardSession(backend)

    first = session.open()
    second = session.open()

    assert backend.connect_calls == 1
    assert first == second
    assert session.connected is True


def test_multiple_apdus_use_same_session():
    backend = FakeBackend()
    session = CardSession(backend)

    session.open()
    r1 = session.transmit("00A4040000")
    r2 = session.transmit("00CA000000")

    assert backend.connect_calls == 1
    assert backend.transmit_calls == [
        "00A4040000",
        "00CA000000",
    ]
    assert r1 == b"\x90\x00"
    assert r2 == b"\x90\x00"


def test_transmit_without_open_fails():
    backend = FakeBackend()
    session = CardSession(backend)

    try:
        session.transmit("00A4040000")
        assert False, "expected CardSessionError"
    except CardSessionError:
        pass


def test_close_disconnects_once():
    backend = FakeBackend()
    session = CardSession(backend)

    session.open()
    session.close()
    session.close()

    assert backend.disconnect_calls == 1
    assert session.connected is False



def test_open_reads_method_based_pcsc_metadata():
    class MethodBackend:
        def __init__(self):
            self.connect_calls = 0
            self.disconnect_calls = 0
            self.reader_name = "Alcor Test Reader"

        def connect(self):
            self.connect_calls += 1
            return self

        def getATR(self):
            return bytes.fromhex(
                "3B6A00FF0031C173C84000009000"
            )

        def getProtocol(self):
            return "T=0"

        def transmit(self, command):
            raise AssertionError(
                "no APDU expected"
            )

        def disconnect(self):
            self.disconnect_calls += 1

    backend = MethodBackend()

    session = CardSession(
        backend
    )

    info = session.open()

    assert info.reader == "Alcor Test Reader"
    assert info.protocol == "T=0"

    assert (
        info.atr
        == "3B6A00FF0031C173C84000009000"
    )

    assert backend.connect_calls == 1

    session.close()

    assert backend.disconnect_calls == 1


def test_open_keeps_attribute_based_metadata():
    backend = FakeBackend()

    session = CardSession(
        backend
    )

    info = session.open()

    assert info.reader == "Fake Reader"
    assert info.protocol == "T=0"

    assert (
        info.atr
        == "3B6A00FF0031C173C84000009000"
    )

    session.close()
