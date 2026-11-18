from dataclasses import dataclass

import pytest

from card_studio.globalplatform_registry import (
    GlobalPlatformRegistryReader,
    build_get_status_apdu,
    parse_registry_tlv,
)
from card_studio.model import CardError


@dataclass
class FakeResponse:
    data: bytes
    sw1sw2: str


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.commands = []

    def transmit_hex(self, command):
        self.commands.append(command)
        return self.responses.pop(0)


def test_build_get_status_first():
    assert (
        build_get_status_apdu(0x40)
        == "80F24002024F0000"
    )


def test_build_get_status_next():
    assert (
        build_get_status_apdu(
            0x40,
            next_occurrence=True,
        )
        == "80F24003024F0000"
    )


def test_parse_application_e3():
    raw = bytes.fromhex(
        "E312"
        "4F07D4534C504D4701"
        "9F700107"
        "C503000000"
    )

    entries = parse_registry_tlv(
        raw,
        entry_type="APPLET",
    )

    assert entries == [{
        "type": "APPLET",
        "aid": "D4534C504D4701",
        "life_cycle": "07",
        "privileges": "000000",
    }]


def test_parse_package_e3():
    raw = bytes.fromhex(
        "E30C"
        "4F06D4534C504D47"
        "9F700101"
    )

    entries = parse_registry_tlv(
        raw,
        entry_type="PACKAGE",
    )

    assert entries == [{
        "type": "PACKAGE",
        "aid": "D4534C504D47",
        "life_cycle": "01",
    }]


def test_registry_reader_handles_6310():
    first = bytes.fromhex(
        "E312"
        "4F07D4534C504D4701"
        "9F700107"
        "C503000000"
    )

    second = bytes.fromhex(
        "E312"
        "4F07D4534C504D4501"
        "9F700107"
        "C503000000"
    )

    transport = FakeTransport([
        FakeResponse(first, "6310"),
        FakeResponse(second, "9000"),
    ])

    reader = GlobalPlatformRegistryReader(
        transport
    )

    entries = reader.read_entries(
        0x40,
        entry_type="APPLET",
    )

    assert len(entries) == 2

    assert transport.commands == [
        "80F24002024F0000",
        "80F24003024F0000",
    ]


def test_registry_reader_treats_6a88_as_empty():
    transport = FakeTransport([
        FakeResponse(b"", "6A88"),
    ])

    reader = GlobalPlatformRegistryReader(
        transport
    )

    assert reader.read_entries(
        0x20,
        entry_type="PACKAGE",
    ) == []


def test_registry_reader_rejects_error_sw():
    transport = FakeTransport([
        FakeResponse(b"", "6985"),
    ])

    reader = GlobalPlatformRegistryReader(
        transport
    )

    with pytest.raises(CardError) as exc:
        reader.read_entries(
            0x40,
            entry_type="APPLET",
        )

    assert exc.value.code == "GP_GET_STATUS_FAILED"
