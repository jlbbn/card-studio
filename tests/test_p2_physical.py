from __future__ import annotations

import unittest

from card_studio.applet import ApduResponse, build_select_apdu
from card_studio.integration.physical import (
    PersonalizationVerifier,
    PhysicalGate,
    PhysicalGateMachine,
    PhysicalPreparationError,
    prepare_physical_session,
    resolve_aid,
)
from card_studio.pcsc import LinuxPcscBackend
from card_studio.readers.base import Capability, CapabilityState, ReaderError
from card_studio.readers.pcsc import PcscReaderAdapter


class FakePcscError(Exception):
    def __init__(self, hresult):
        super().__init__("LAB PCSC error")
        self.hresult = hresult


class FakeConnection:
    def __init__(self, statuses=None, protocol=1):
        self.statuses = statuses or {}
        self.protocol = protocol
        self.commands = []
        self.disconnected = False

    def connect(self):
        return None

    def getATR(self):
        return [0x3B, 0x00]

    def getProtocol(self):
        return self.protocol

    def transmit(self, command):
        raw = bytes(command)
        self.commands.append(raw)
        status = self.statuses.get(raw.hex().upper(), "9000")
        if status == "TRANSMIT_TIMEOUT":
            raise FakePcscError(0x8010000A)
        if status == "CARD_REMOVED":
            raise FakePcscError(0x80100069)
        value = int(status, 16)
        return [], value >> 8, value & 0xFF

    def disconnect(self):
        self.disconnected = True


class FakeReader:
    def __init__(self, connection):
        self.connection = connection

    def __str__(self):
        return "Rocketek CSCR3 00 00"

    def createConnection(self):
        return self.connection


def adapter(connection):
    reader = FakeReader(connection)
    backend = LinuxPcscBackend(reader_provider=lambda: [reader])
    return PcscReaderAdapter(
        backend,
        interface_capabilities={"CONTACT": Capability(CapabilityState.SUPPORTED, source="LAB_TEST")},
    )


class AidResolutionTests(unittest.TestCase):
    def test_all_aid_outcomes_and_unknown_status(self):
        first = "D4534C504D4401"
        second = "D4534C504D44"
        cases = (
            ({first: "9000", second: "6A82"}, "PHYSICALLY_CONFIRMED", first),
            ({first: "6A82", second: "9000"}, "PHYSICALLY_CONFIRMED", second),
            ({first: "9000", second: "9000"}, "MULTIPLE_AID_MATCH", None),
            ({first: "6A82", second: "6A82"}, "APPLET_NOT_FOUND", None),
            ({first: "6985", second: "9000"}, "INCONCLUSIVE", None),
        )
        for statuses, expected, selected in cases:
            with self.subTest(expected=expected):
                def select(aid):
                    status = statuses[aid]
                    return ApduResponse(b"", int(status[:2], 16), int(status[2:], 16))
                report = resolve_aid(select, "PHYSICAL")
                self.assertEqual(report["result"], expected)
                self.assertEqual(report["selected_aid"], selected)
                self.assertEqual(report["physical_validation"], "INCONCLUSIVE")

    def test_simulated_selection_never_selects_physical_aid(self):
        report = resolve_aid(lambda aid: "9000" if aid.endswith("01") else "6A82")
        self.assertEqual(report["result"], "SIMULATED_MATCH")
        self.assertIsNone(report["selected_aid"])
        self.assertEqual(report["simulated_selected_aid"], "D4534C504D4401")
        self.assertEqual(report["physical_validation"], "NOT_EXECUTED")


class PhysicalPreparationTests(unittest.TestCase):
    def test_pcsc_atr_protocol_select_disconnect_and_reconnect(self):
        connection = FakeConnection({
            build_select_apdu("D4534C504D4401").hex().upper(): "9000",
            build_select_apdu("D4534C504D44").hex().upper(): "6A82",
        })
        report = prepare_physical_session(adapter(connection), "Rocketek CSCR3 00 00", "p2-physical")
        self.assertEqual(report["status"], "PREPARED")
        self.assertIsNone(report["selected_aid"])
        self.assertEqual(report["aid_resolution"]["simulated_selected_aid"], "D4534C504D4401")
        self.assertEqual(report["protocol"], "T=1")
        self.assertEqual(report["reader"]["atr"], "3B00")
        self.assertEqual(report["physical_validation"], "NOT_EXECUTED")
        self.assertNotEqual(report["physical_validation"], "PASS")
        self.assertEqual([event["stage"] for event in report["events"]], [
            "DETECTED", "CONNECTED", "SELECT", "DISCONNECTED", "RECONNECTED",
        ])
        self.assertTrue(connection.disconnected)

    def test_post_send_timeout_is_uncertain_without_write(self):
        connection = FakeConnection({
            build_select_apdu("D4534C504D4401").hex().upper(): "TRANSMIT_TIMEOUT",
        })
        report = prepare_physical_session(adapter(connection), "Rocketek CSCR3 00 00", "p2-timeout")
        self.assertEqual(report["status"], "TRANSMIT_TIMEOUT")
        self.assertEqual(report["recovery"], "QUERY_REQUIRED")
        self.assertTrue(any(event.get("error_code") == "TRANSMIT_TIMEOUT" for event in report["events"]))
        self.assertEqual(report["physical_validation"], "NOT_EXECUTED")

    def test_gate_and_personalization_require_authorization_and_exact_readback(self):
        gate = PhysicalGateMachine()
        for state in (PhysicalGate.DETECTED, PhysicalGate.VALIDATED, PhysicalGate.APPLET_READY):
            gate.advance(state, "LAB")
        verifier = PersonalizationVerifier(gate)
        writes = []
        with self.assertRaises(PhysicalPreparationError) as unauthorized:
            verifier.personalize(b"synthetic", writes.append, lambda: b"synthetic", lambda a, b: a == b, authorized=False)
        self.assertEqual(unauthorized.exception.code, "WRITE_NOT_AUTHORIZED")
        self.assertEqual(writes, [])
        result = verifier.personalize(b"synthetic", writes.append, lambda: b"synthetic", lambda a, b: a == b, authorized=True)
        self.assertEqual(result["verify"], "PASS")
        self.assertEqual(result["physical_validation"], "NOT_EXECUTED")
        verifier.register("synthetic-register")
        verifier.bind_token("synthetic-token")
        verifier.activate("synthetic-active")
        self.assertEqual(gate.state, PhysicalGate.ACTIVE)
        self.assertEqual([item["new_state"] for item in gate.history], [
            "DETECTED", "VALIDATED", "APPLET_READY", "PERSONALIZING",
            "PERSONALIZED", "VERIFIED", "REGISTERED", "TOKEN_BOUND", "ACTIVE",
        ])

    def test_failed_readback_does_not_verify_or_activate(self):
        gate = PhysicalGateMachine()
        for state in (PhysicalGate.DETECTED, PhysicalGate.VALIDATED, PhysicalGate.APPLET_READY):
            gate.advance(state, "LAB")
        verifier = PersonalizationVerifier(gate)
        with self.assertRaises(PhysicalPreparationError) as caught:
            verifier.personalize(b"expected", lambda _: None, lambda: b"different", lambda a, b: a == b, authorized=True)
        self.assertEqual(caught.exception.code, "PERSONALIZATION_VERIFY_FAILED")
        self.assertEqual(gate.state, PhysicalGate.PERSONALIZING)
        with self.assertRaises(PhysicalPreparationError):
            verifier.activate("not-allowed")

    def test_gate_rejects_skipped_states(self):
        gate = PhysicalGateMachine()
        with self.assertRaises(PhysicalPreparationError) as caught:
            gate.advance(PhysicalGate.VERIFIED, "skip")
        self.assertEqual(caught.exception.code, "INVALID_PHYSICAL_GATE_TRANSITION")


if __name__ == "__main__":
    unittest.main()
