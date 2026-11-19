import base64
import hashlib
import hmac
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from card_studio.ledger import (
    LedgerError,
    REAL_HEALTH_PATH,
    REAL_OPERATION_PATH,
    RealLedgerClient,
    canonical_payment_request,
)
from card_studio.model import Operation


class RealApiHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(("GET", self.path, {key.lower(): value for key, value in self.headers.items()}, None))
        self.respond(self.server.health_response)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.server.requests.append(("POST", self.path, {key.lower(): value for key, value in self.headers.items()}, body))
        self.respond({
            "ok": True,
            "status": "RECORDED",
            "operationId": body["operationId"],
            "simulation": False,
            "service": "swift-ledger-payment-engine",
            "backendId": "venus-swift-ledger-real",
            "environment": "VENUS_PRIVATE",
            "contract": "card-studio/v1",
            "replayed": False,
        }, 201)

    def respond(self, value, status=200):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class RealLedgerTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RealApiHandler)
        self.server.requests = []
        self.server.health_response = {
            "ok": True,
            "simulation": False,
            "service": "swift-ledger-payment-engine",
            "backendId": "venus-swift-ledger-real",
            "environment": "VENUS_PRIVATE",
            "contract": "card-studio/v1",
            "storage": "postgresql",
            "serverTime": "2026-11-18T00:00:00.000Z",
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.secret = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def client(self):
        return RealLedgerClient(
            self.url, "VENUS-MAC-001", self.secret, "venus-swift-ledger-real",
        )

    def assert_valid_signature(self, method, path, headers, body):
        canonical = canonical_payment_request(
            "VENUS-MAC-001",
            headers["x-payment-timestamp"],
            headers["x-payment-nonce"],
            path,
            body,
            method,
        )
        key = base64.urlsafe_b64decode(self.secret + "=" * (-len(self.secret) % 4))
        expected = hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-payment-hmac"], expected)

    def test_real_health_is_signed_and_identifies_persistent_backend(self):
        health = self.client().health()
        self.assertEqual(health["backendId"], "venus-swift-ledger-real")
        self.assertFalse(health["simulation"])
        method, path, headers, body = self.server.requests[-1]
        self.assertEqual((method, path, body), ("GET", REAL_HEALTH_PATH, None))
        self.assert_valid_signature(method, path, headers, None)

    def test_real_operation_is_signed_and_not_marked_simulation(self):
        operation = Operation.create("VIRTUAL", "RELOAD", "SL-CAJA-001", 5000, 0, 5000)
        response = self.client().record(operation)
        self.assertEqual(response["status"], "RECORDED")
        self.assertFalse(response["simulation"])
        method, path, headers, body = self.server.requests[-1]
        self.assertEqual((method, path), ("POST", REAL_OPERATION_PATH))
        self.assert_valid_signature(method, path, headers, body)

    def test_sandbox_or_unpinned_backend_is_rejected(self):
        valid = dict(self.server.health_response)
        for response in [
            {"ok": True, "simulation": True},
            {**valid, "backendId": "other-backend"},
            {**valid, "service": "sunmi-v3-cobros"},
        ]:
            self.server.health_response = response
            with self.subTest(response=response), self.assertRaises(LedgerError):
                self.client().health()

    def test_real_api_requires_terminal_credentials(self):
        with self.assertRaises(LedgerError):
            RealLedgerClient(self.url).health()
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
