from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from card_studio.integration.sunmi import CardFlowState, SunmiCardFlow
from card_studio.production.contract import CardServiceContract, ContractError, canonical_json, utc_now
from card_studio.production.http import LedgerApplication, LedgerServiceClient, make_server
from card_studio.production.security import ServiceAuthenticator, ServiceCredentials
from card_studio.production.store import LedgerStore, PersistenceError


class P3ContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = LedgerStore(self.root / "ledger.sqlite3")
        self.credentials = {
            "terminal-a": ServiceCredentials(bytes(range(32))),
            "terminal-b": ServiceCredentials(bytes(reversed(range(32)))),
            "limited": ServiceCredentials(bytes(range(32, 64)), frozenset({"purchase", "query"})),
        }
        self.auth = ServiceAuthenticator(self.credentials, self.store)
        self.server = make_server(("127.0.0.1", 0), LedgerApplication(self.store, self.auth))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = LedgerServiceClient(self.url, "terminal-a", self.auth)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def contract(self, transaction_id="tx-1", **changes):
        now = utc_now()
        values = {
            "terminal_id": "terminal-a",
            "correlation_id": f"correlation-{transaction_id}",
            "transaction_id": transaction_id,
            "idempotency_key": f"key-{transaction_id}",
            "operation_type": "purchase",
            "amount_minor": 1250,
            "currency": "MXN",
            "transaction_state": "APPROVED",
            "created_at": now,
            "updated_at": now,
            "result": {
                "execution_state": "SUCCEEDED", "functional_result": "APPROVED",
                "error_code": None, "host_reference": "host-1", "host_operation": "authorize",
                "result_reference": "result.json",
            },
            "recovery": {
                "recovery_action": "NON_RETRYABLE", "command_sent": True, "attempt": 1,
                "original_transaction_id": None,
            },
        }
        values.update(changes)
        return CardServiceContract.from_dict(values)

    def raw_post(self, path, body, headers):
        request = urllib.request.Request(
            self.url + path, data=canonical_json(body), method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_real_service_to_service_contract_and_health(self):
        response = self.client.submit(self.contract())
        self.assertTrue(response["created"])
        self.assertEqual(response["operation"]["terminal_id"], "terminal-a")
        self.assertEqual(response["operation"]["amount_minor"], 1250)
        self.assertEqual(self.store.operation_count(), 1)
        self.assertEqual(self.store.effect_count(), 1)
        self.assertEqual(self.client.health("/livez"), {"ok": True})
        self.assertEqual(self.client.health("/readyz"), {"ok": True})

    def test_invalid_authentication_and_signature(self):
        wrong_auth = ServiceAuthenticator(
            {"terminal-a": ServiceCredentials(b"x" * 32)}, self.store,
        )
        client = LedgerServiceClient(self.url, "terminal-a", wrong_auth)
        with self.assertRaisesRegex(RuntimeError, "INVALID_SIGNATURE"):
            client.submit(self.contract())
        self.assertEqual(self.store.operation_count(), 0)

    def test_replay_and_expired_timestamp(self):
        contract = self.contract()
        body = contract.to_dict()
        path = "/v1/card/operations"
        headers = self.auth.headers("terminal-a", "POST", path, body, nonce="nonce-replay-1")
        headers["X-Correlation-Id"] = contract.correlation_id
        self.assertEqual(self.raw_post(path, body, headers)[0], 200)
        status, response = self.raw_post(path, body, headers)
        self.assertEqual(status, 401)
        self.assertEqual(response["error"], "DUPLICATE_NONCE")
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        headers = self.auth.headers("terminal-a", "POST", path, body, timestamp=stale)
        headers["X-Correlation-Id"] = contract.correlation_id
        self.assertEqual(self.raw_post(path, body, headers)[1]["error"], "TIMESTAMP_EXPIRED")

    def test_tampered_payload_and_correlation_mismatch(self):
        contract = self.contract()
        body = contract.to_dict()
        path = "/v1/card/operations"
        headers = self.auth.headers("terminal-a", "POST", path, body)
        headers["X-Correlation-Id"] = contract.correlation_id
        body["amount_minor"] = 9999
        self.assertEqual(self.raw_post(path, body, headers)[1]["error"], "INVALID_SIGNATURE")
        body = contract.to_dict()
        headers = self.auth.headers("terminal-a", "POST", path, body)
        headers["X-Correlation-Id"] = "wrong-correlation"
        self.assertEqual(self.raw_post(path, body, headers)[1]["error"], "CORRELATION_MISMATCH")

    def test_wrong_terminal_and_cross_terminal_isolation(self):
        contract = self.contract(terminal_id="terminal-b")
        body = contract.to_dict()
        path = "/v1/card/operations"
        headers = self.auth.headers("terminal-a", "POST", path, body)
        headers["X-Correlation-Id"] = contract.correlation_id
        self.assertEqual(self.raw_post(path, body, headers)[1]["error"], "TERMINAL_MISMATCH")
        self.client.submit(self.contract())
        other = LedgerServiceClient(self.url, "terminal-b", self.auth)
        with self.assertRaisesRegex(RuntimeError, "TRANSACTION_NOT_FOUND"):
            other.query(self.contract(terminal_id="terminal-b"))

    def test_unauthorized_refund_fails_closed(self):
        limited = LedgerServiceClient(self.url, "limited", self.auth)
        contract = self.contract(
            terminal_id="limited", operation_type="refund",
            recovery={**self.contract().recovery, "original_transaction_id": "original-tx"},
        )
        with self.assertRaisesRegex(RuntimeError, "OPERATION_NOT_AUTHORIZED"):
            limited.submit(contract)
        self.assertEqual(self.store.operation_count(), 0)

    def test_unauthorized_reversal_fails_closed(self):
        limited = LedgerServiceClient(self.url, "limited", self.auth)
        contract = self.contract(
            terminal_id="limited", operation_type="reversal", transaction_state="REVERSED",
            result={**self.contract().result, "host_operation": "reverse"},
            recovery={**self.contract().recovery, "original_transaction_id": "original-tx"},
        )
        with self.assertRaisesRegex(RuntimeError, "OPERATION_NOT_AUTHORIZED"):
            limited.submit(contract)
        self.assertEqual(self.store.operation_count(), 0)

    def test_malformed_amount_currency_and_transition(self):
        with self.assertRaises(ContractError):
            self.contract(amount_minor=0)
        with self.assertRaises(ContractError):
            self.contract(currency="ZZZ")
        with self.assertRaises(ContractError):
            self.contract(transaction_state="DECLINED")
        with self.assertRaises(ContractError):
            self.contract(transaction_state="APPROVED", transaction_type="invalid")

    def test_uncertain_is_not_declined_and_failed_is_distinct(self):
        uncertain = self.contract(
            transaction_state="UNCERTAIN",
            result={**self.contract().result, "execution_state": "UNCERTAIN", "functional_result": None},
            recovery={**self.contract().recovery, "recovery_action": "QUERY_REQUIRED"},
        )
        self.assertEqual(uncertain.transaction_state, "UNCERTAIN")
        failed = self.contract(
            transaction_state="FAILED",
            result={**self.contract().result, "execution_state": "FAILED", "functional_result": None},
        )
        self.assertNotEqual(failed.transaction_state, "DECLINED")
        flow = SunmiCardFlow()
        flow.apply({"transaction_type": "purchase", "execution_state": "FAILED", "functional_result": None}, [])
        self.assertEqual(flow.state, CardFlowState.FAILED)

    def test_readiness_failure_is_sanitized(self):
        original = self.store.ping
        self.store.ping = lambda: False
        try:
            self.assertEqual(self.client.health("/readyz"), {"ok": False})
        finally:
            self.store.ping = original

    def test_duplicate_idempotent_response_and_conflict(self):
        contract = self.contract()
        first = self.client.submit(contract)
        second = self.client.submit(contract)
        self.assertFalse(second["created"])
        self.assertEqual(first["operation"], second["operation"])
        changed = self.contract(amount_minor=1300)
        with self.assertRaises(PersistenceError):
            self.store.record(changed)
        self.assertEqual(self.store.operation_count(), 1)


if __name__ == "__main__":
    unittest.main()
