import base64
import hashlib
import hmac
import json
import os
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from card_studio.ledger import (
    REAL_HEALTH_PATH,
    LedgerError,
    RealLedgerClient,
    canonical_payment_request,
    decode_secret,
)
from card_studio.model import Operation

LIVE_URL = os.environ.get("CARD_STUDIO_LIVE_API_URL", "")
LIVE_KEY = os.environ.get("SWIFT_LEDGER_TERMINAL_KEY", "")


@unittest.skipUnless(LIVE_URL and LIVE_KEY, "Live real API credentials not provided")
class RealApiLiveTests(unittest.TestCase):
    def client(self, key=LIVE_KEY):
        return RealLedgerClient(
            LIVE_URL,
            os.environ.get("SWIFT_LEDGER_TERMINAL", "VENUS-MAC-001"),
            key,
            os.environ.get("SWIFT_LEDGER_BACKEND_ID", "venus-swift-ledger-real"),
        )

    def test_health_identifies_real_persistent_backend(self):
        health = self.client().health()
        self.assertEqual(health["service"], "swift-ledger-payment-engine")
        self.assertEqual(health["environment"], "VENUS_PRIVATE")
        self.assertFalse(health["simulation"])
        self.assertEqual(health["storage"], "postgresql")

    def test_invalid_terminal_key_is_rejected(self):
        wrong_key = base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode().rstrip("=")
        with self.assertRaisesRegex(LedgerError, "INVALID_AUTHENTICATION"):
            self.client(wrong_key).health()

    def test_real_operation_is_persistent_and_idempotent(self):
        operation = Operation.create("VIRTUAL", "RELOAD", "SL-VENUS-INTEGRATION", 100, 0, 100)
        first = self.client().record(operation)
        replay = self.client().record(operation)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertFalse(first["simulation"])
        self.assertEqual(first["operationId"], operation.operation_id)

    def test_exact_authentication_replay_is_rejected(self):
        terminal = os.environ.get("SWIFT_LEDGER_TERMINAL", "VENUS-MAC-001")
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        nonce = str(uuid.uuid4())
        canonical = canonical_payment_request(terminal, timestamp, nonce, REAL_HEALTH_PATH, None, "GET")
        signature = hmac.new(decode_secret(LIVE_KEY), canonical.encode(), hashlib.sha256).hexdigest()
        request = urllib.request.Request(LIVE_URL + REAL_HEALTH_PATH, headers={
            "Accept": "application/json",
            "x-terminal-code": terminal,
            "x-payment-timestamp": timestamp,
            "x-payment-nonce": nonce,
            "x-payment-hmac": signature,
        })
        with urllib.request.urlopen(request, timeout=6) as response:
            self.assertEqual(response.status, 200)
            json.loads(response.read())
        with self.assertRaises(urllib.error.HTTPError) as replay:
            urllib.request.urlopen(request, timeout=6)
        self.assertEqual(replay.exception.code, 401)
        self.assertEqual(json.loads(replay.exception.read())["error"], "REPLAY_DETECTED")
        replay.exception.close()


if __name__ == "__main__":
    unittest.main()
