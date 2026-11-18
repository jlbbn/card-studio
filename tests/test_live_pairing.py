from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from card_studio.cli import main
from card_studio.pairing import (
    AtomicEnvelopeStore,
    PairingClient,
    PairingError,
    PairingService,
    build_metadata_event,
    decode_pairing_key,
    generate_pairing_key,
    make_server,
    metadata_event_from_evidence,
)
from card_studio.pairing.crypto import SessionKeys, open_envelope, seal_event, sign_value
from card_studio.pairing.schema import (
    EVENTS_PATH,
    PAIRING_SCHEMA,
    encode_base64,
    format_timestamp,
    utc_now,
)


class LivePairingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.key = bytes(range(32))
        self.store = AtomicEnvelopeStore(self.root / "server")
        self.service = PairingService(self.key, "paco-reader", "server-main", self.store)
        self.server = make_server(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = PairingClient(self.url, "paco-reader", self.key)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def event(self, **changes):
        metadata = {
            "atr": "3B0000",
            "selected_aid": "D4534C504D4401",
            "reader_name": "Lector sintético",
            "interface": "CONTACT",
            "protocol": "T=1",
            "standard": "ISO/IEC 7816-4",
        }
        values = {
            "reader_id": "paco-reader",
            "metadata": metadata,
            "evidence_class": "SYNTHETIC",
            "source_result": "SIMULATED_MATCH",
            "captured_at": "2026-11-08T00:00:00.000000Z",
            "event_id": "evt-synthetic-0001",
        }
        values.update(changes)
        return build_metadata_event(**values)

    def files(self):
        return list((self.root / "server" / "events").glob("*.json"))

    def test_real_http_pairing_encrypted_atomic_storage_and_health(self):
        paired = self.client.pair()
        self.assertEqual(paired["server_id"], "server-main")
        acknowledgement = self.client.publish(self.event())
        self.assertEqual(acknowledgement["status"], "STORED")
        self.assertEqual(acknowledgement["event_id"], "evt-synthetic-0001")
        self.assertEqual(len(self.files()), 1)
        path = self.files()[0]
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("ciphertext", stored["envelope"])
        self.assertNotIn("metadata", stored)
        self.assertNotIn("selected_aid", stored)
        self.assertNotIn("D4534C504D4401", path.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with urllib.request.urlopen(self.url + "/livez", timeout=2) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})
        with urllib.request.urlopen(self.url + "/readyz", timeout=2) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})

    def test_aes_gcm_detects_ciphertext_and_associated_header_tampering(self):
        self.client.pair()
        session = self.service.sessions[self.client.session.session_id]
        envelope = seal_event(
            session.keys, self.event(), session_id=self.client.session.session_id,
            sender_id="paco-reader", sequence=1,
        )
        tampered = dict(envelope)
        ciphertext = tampered["ciphertext"]
        tampered["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
        with self.assertRaisesRegex(ValueError, "ENVELOPE_AUTHENTICATION_FAILED"):
            open_envelope(session.keys, tampered)
        tampered_header = dict(envelope, sender_id="other-reader")
        with self.assertRaisesRegex(ValueError, "SENDER_MISMATCH"):
            self.service.event(tampered_header)
        self.assertEqual(self.files(), [])

    def test_wrong_pairing_key_cannot_create_session(self):
        wrong = PairingClient(self.url, "paco-reader", bytes(reversed(range(32))))
        with self.assertRaisesRegex(ValueError, "INVALID_SIGNATURE"):
            wrong.pair()
        self.assertEqual(self.service.sessions, {})

    def test_hello_nonce_replay_is_rejected(self):
        request = {
            "schema": PAIRING_SCHEMA,
            "reader_id": "paco-reader",
            "reader_nonce": encode_base64(b"r" * 32),
            "requested_at": utc_now(),
        }
        request["signature"] = sign_value(self.key, "CARD-STUDIO-HELLO-V1", request)
        self.service.hello(request)
        with self.assertRaisesRegex(ValueError, "DUPLICATE_HELLO_NONCE"):
            self.service.hello(request)

    def test_sequence_and_reader_binding_fail_closed(self):
        self.client.pair()
        session = self.service.sessions[self.client.session.session_id]
        envelope = seal_event(
            session.keys, self.event(), session_id=self.client.session.session_id,
            sender_id="paco-reader", sequence=2,
        )
        with self.assertRaisesRegex(ValueError, "SEQUENCE_MISMATCH"):
            self.service.event(envelope)
        other = self.event(reader_id="other-reader")
        envelope = seal_event(
            session.keys, other, session_id=self.client.session.session_id,
            sender_id="paco-reader", sequence=1,
        )
        with self.assertRaisesRegex(ValueError, "EVENT_READER_MISMATCH"):
            self.service.event(envelope)
        self.assertEqual(self.files(), [])

    def test_exact_envelope_replay_is_idempotent(self):
        self.client.pair()
        session = self.service.sessions[self.client.session.session_id]
        envelope = seal_event(
            session.keys, self.event(), session_id=self.client.session.session_id,
            sender_id="paco-reader", sequence=1,
        )
        self.assertEqual(self.service.event(envelope)["status"], "STORED")
        self.assertEqual(self.service.event(envelope)["status"], "DUPLICATE")
        changed = dict(envelope, ciphertext=("A" if envelope["ciphertext"][0] != "A" else "B") + envelope["ciphertext"][1:])
        with self.assertRaisesRegex(ValueError, "MESSAGE_ID_CONFLICT"):
            self.service.event(changed)
        self.assertEqual(len(self.files()), 1)

    def test_same_event_is_idempotent_across_sessions(self):
        self.client.pair()
        self.assertEqual(self.client.publish(self.event())["status"], "STORED")
        second = PairingClient(self.url, "paco-reader", self.key)
        second.pair()
        self.assertEqual(second.publish(self.event())["status"], "DUPLICATE")
        self.assertEqual(len(self.files()), 1)

    def test_event_id_conflict_cannot_replace_original(self):
        self.client.pair()
        self.client.publish(self.event())
        second = PairingClient(self.url, "paco-reader", self.key)
        second.pair()
        changed = self.event(metadata={"atr": "3B0001"})
        with self.assertRaisesRegex(ValueError, "EVENT_ID_CONFLICT"):
            second.publish(changed)
        self.assertEqual(len(self.files()), 1)

    def test_uncertain_send_requires_explicit_retry_and_preserves_envelope(self):
        self.client.pair()
        original = self.client._request
        failed = False

        def lose_ack(path, value):
            nonlocal failed
            response = original(path, value)
            if path == EVENTS_PATH and not failed:
                failed = True
                raise PairingError("SERVER_TIMEOUT_AFTER_SEND")
            return response

        self.client._request = lose_ack
        with self.assertRaisesRegex(ValueError, "SERVER_TIMEOUT_AFTER_SEND"):
            self.client.publish(self.event())
        pending = dict(self.client.session.pending)
        with self.assertRaisesRegex(ValueError, "UNCONFIRMED_EVENT_REQUIRES_RETRY"):
            self.client.publish(self.event(event_id="evt-other"))
        self.assertEqual(self.client.session.pending, pending)
        self.assertEqual(self.client.retry()["status"], "DUPLICATE")
        self.assertIsNone(self.client.session.pending)
        self.assertEqual(len(self.files()), 1)

    def test_ack_event_binding_and_signature_are_verified(self):
        self.client.pair()
        original = self.client._request
        tamper = True

        def change_ack(path, value):
            response = dict(original(path, value))
            if path == EVENTS_PATH and tamper:
                response["event_id"] = "evt-wrong"
            return response

        self.client._request = change_ack
        with self.assertRaisesRegex(ValueError, "ACK_MISMATCH"):
            self.client.publish(self.event())
        tamper = False
        self.assertEqual(self.client.retry()["status"], "DUPLICATE")

    def test_expired_session_is_not_accepted(self):
        now = [datetime.now(timezone.utc)]
        service = PairingService(
            self.key, "paco-reader", "server-main", self.store,
            clock=lambda: now[0],
        )
        server = make_server(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = PairingClient(f"http://127.0.0.1:{server.server_port}", "paco-reader", self.key)
            client.pair()
            now[0] += timedelta(seconds=301)
            with self.assertRaisesRegex(ValueError, "PAIRING_EXPIRED"):
                client.publish(self.event())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_sensitive_and_unknown_fields_are_rejected(self):
        forbidden = [
            {"track2": "1234567890123456=25121010000000000000"},
            {"pan": "4111111111111111"},
            {"pin": "1234"},
            {"cvv": "123"},
            {"raw_apdu": "00A40400"},
            {"application_cryptogram": "7ABC1F9E40229011"},
        ]
        for extra in forbidden:
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, "INVALID_METADATA_FIELDS"):
                    self.event(metadata={"atr": "3B0000", **extra})

    def test_metadata_validation_rejects_malformed_or_unsafe_values(self):
        cases = [
            {"atr": "8F8001"},
            {"atr": "3B01ZZ"},
            {"selected_aid": "D4"},
            {"application_label": "4111111111111111"},
            {"protocol": "T=2"},
            {"interface": "MAGNETIC"},
        ]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                with self.assertRaises(PairingError):
                    self.event(metadata=metadata)

    def test_evidence_mapping_preserves_simulation_and_excludes_apdu(self):
        report = {
            "mode": "SIMULATED",
            "timestamp": "2026-11-08T00:00:00Z",
            "reader_name": "Rocketek SIMULATED",
            "atr": "3B0000",
            "simulated_selected_aid": "D4534C504D4401",
            "result": "SIMULATED_MATCH",
            "select_attempts": [{"select_apdu": "00A4040007D4534C504D4401"}],
        }
        event = metadata_event_from_evidence(report, "paco-reader", protocol="T=1")
        self.assertEqual(event["evidence_class"], "SYNTHETIC")
        self.assertNotIn("select_attempts", json.dumps(event))
        self.assertNotIn("00A404", json.dumps(event))

    def test_physical_evidence_is_only_live_read_only(self):
        report = {
            "mode": "PHYSICAL",
            "timestamp": "2026-11-08T00:00:00Z",
            "reader_name": "Rocketek CSCR3",
            "atr": "3B0000",
            "selected_aid": "D4534C504D4401",
            "result": "PHYSICALLY_CONFIRMED",
        }
        event = metadata_event_from_evidence(report, "paco-reader")
        self.assertEqual(event["evidence_class"], "LIVE_READ_ONLY")
        self.assertNotIn("physical_validation", event)

    def test_cli_publishes_without_exposing_key_or_plaintext_on_server(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"CARD_STUDIO_PAIRING_KEY": generate_pairing_key()}, clear=False):
            key = os.environ["CARD_STUDIO_PAIRING_KEY"]
            self.service.pairing_key = decode_pairing_key(key)
            self.client = PairingClient(self.url, "paco-reader", decode_pairing_key(key))
            with contextlib.redirect_stdout(output):
                result = main([
                    "--config-dir", str(self.root / "config"),
                    "pairing", "publish", "--server", self.url,
                    "--reader-id", "paco-reader", "--atr", "3B0000",
                    "--aid", "D4534C504D4401", "--event-id", "evt-cli-0001",
                ])
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "STORED")
        self.assertNotIn(key, output.getvalue())
        self.assertNotIn("D4534C504D4401", self.files()[0].read_text(encoding="utf-8"))

    def test_cli_generate_key_and_missing_key_behavior(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["--config-dir", str(self.root / "config"), "pairing", "generate-key"])
        self.assertEqual(result, 0)
        generated = json.loads(output.getvalue())
        self.assertEqual(len(decode_pairing_key(generated["pairing_key"])), 32)
        self.assertFalse((self.root / "config" / "settings.json").exists())
        output = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            result = main([
                "pairing", "publish", "--server", self.url,
                "--reader-id", "paco-reader", "--atr", "3B0000",
            ])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["error"], "PAIRING_KEY_REQUIRED")

    def test_no_ack_before_durable_write(self):
        self.client.pair()
        original = self.client._request
        with mock.patch("card_studio.pairing.store.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "INTERNAL_ERROR"):
                self.client.publish(self.event())
        self.assertIsNotNone(self.client.session.pending)
        self.client._request = original
        self.assertEqual(self.client.retry()["status"], "STORED")
        self.assertEqual(len(self.files()), 1)

    def test_dependency_is_declared(self):
        requirements = Path(__file__).parents[1].joinpath("requirements.txt").read_text(encoding="utf-8")
        self.assertIn("cryptography>=46,<47", requirements)


if __name__ == "__main__":
    unittest.main()
