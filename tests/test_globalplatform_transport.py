import pytest

from card_studio.card_session import CardSession
from card_studio.globalplatform_transport import GlobalPlatformTransport
from card_studio.model import CardError


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
        return {
            "command": command,
            "sw": "9000",
        }

    def disconnect(self):
        self.disconnect_calls += 1


def test_gp_transport_requires_existing_session():
    backend = FakeBackend()
    session = CardSession(backend)
    gp = GlobalPlatformTransport(session)

    with pytest.raises(CardError) as exc:
        gp.transmit_hex("00A4040000")

    assert exc.value.code == "GP_SESSION_NOT_OPEN"
    assert backend.connect_calls == 0


def test_gp_transport_never_connects_itself():
    backend = FakeBackend()
    session = CardSession(backend)
    session.open()

    gp = GlobalPlatformTransport(session)

    gp.transmit_hex("00A4040000")
    gp.transmit_hex("80CA000000")

    assert backend.connect_calls == 1
    assert backend.transmit_calls == [
        "00A4040000",
        "80CA000000",
    ]


def test_gp_transport_never_disconnects():
    backend = FakeBackend()
    session = CardSession(backend)
    session.open()

    gp = GlobalPlatformTransport(session)
    gp.transmit_hex("00A4040000")

    assert backend.disconnect_calls == 0
    assert session.connected is True


def test_invalid_gp_apdu_is_rejected():
    backend = FakeBackend()
    session = CardSession(backend)
    session.open()

    gp = GlobalPlatformTransport(session)

    with pytest.raises(CardError) as exc:
        gp.transmit_hex("NOT-HEX")

    assert exc.value.code == "INVALID_GP_APDU"
