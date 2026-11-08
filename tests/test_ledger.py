import base64
import hashlib
import hmac
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from card_studio.ledger import LedgerClient, canonical_payment_request
from card_studio.model import Operation


class SandboxHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(("GET", self.path, {}, None))
        self.respond({"ok": True, "simulation": True, "serverTime": "2026-11-04T00:00:00Z"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.server.requests.append(("POST", self.path, headers, body))
        self.respond({"status": "RECORDED", "simulation": True, "operationId": body["operationId"]})

    def respond(self, value):
        payload = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SandboxHandler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.secret = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_health_and_signed_sandbox_operation(self):
        client = LedgerClient(self.url, "SANDBOX-TEST", self.secret)
        self.assertTrue(client.health()["simulation"])
        operation = Operation.create("VIRTUAL", "RELOAD", "SL-CAJA-001", 5000, 0, 5000)
        response = client.record(operation)
        self.assertEqual(response["status"], "RECORDED")
        method, path, headers, body = self.server.requests[-1]
        self.assertEqual((method, path), ("POST", "/api/pos/card-studio/operations"))
        canonical = canonical_payment_request(
            "SANDBOX-TEST",
            headers["x-payment-timestamp"],
            headers["x-payment-nonce"],
            path,
            body,
        )
        key = base64.urlsafe_b64decode(self.secret + "=" * (-len(self.secret) % 4))
        expected = hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-payment-hmac"], expected)
        self.assertNotIn(self.secret, json.dumps(body))


if __name__ == "__main__":
    unittest.main()
