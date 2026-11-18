from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from card_studio.application import CardStudioApplication
from card_studio.host.mock import MockAuthorizationHost
from card_studio.integration.backend import CardOperationStore
from card_studio.integration.sunmi import SunmiIntegration
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.l3.internal import internal_profile
from card_studio.money import Money
from card_studio.production.http import LedgerApplication, LedgerServiceClient, make_server
from card_studio.production.observability import StructuredLogger
from card_studio.production.pos import SunmiPosService
from card_studio.production.security import ServiceAuthenticator, ServiceCredentials
from card_studio.production.store import LedgerStore, PersistenceError
from card_studio.production.treasury import TreasuryService
from card_studio.simulator.icc import SyntheticIccReader
from card_studio.transactions.model import TransactionRequest


class P3SyntheticE2ETests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.kernel = InternalEmvKernel()
        self.profile = internal_profile(self.kernel, "p3-e2e", "CONTACT", "online_required")
        self.profile_ref = f"{self.profile.profile_id}:{self.profile.profile_version}:{self.profile.profile_digest}"
        self.host = MockAuthorizationHost(self.root / "host.sqlite3")
        self.app = self.make_app()
        self.operation_store = CardOperationStore(self.root / "operations")
        self.integration = SunmiIntegration(self.app, self.operation_store)
        self.store = LedgerStore(self.root / "ledger.sqlite3")
        self.credentials = {"terminal-p3": ServiceCredentials(bytes(range(32)))}
        self.auth = ServiceAuthenticator(self.credentials, self.store)
        self.application = LedgerApplication(self.store, self.auth)
        self.start_server()
        self.logs = io.StringIO()
        self.pos = self.make_pos()

    def tearDown(self):
        self.stop_server()
        self.temporary.cleanup()

    def make_app(self):
        return CardStudioApplication(
            data_dir=self.root / "transactions", profiles=[self.profile], kernels=self.kernel,
            reader=SyntheticIccReader(), host=self.host,
        )

    def start_server(self):
        self.server = make_server(("127.0.0.1", 0), self.application)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()

    def make_pos(self):
        client = LedgerServiceClient(self.url, "terminal-p3", self.auth)
        return SunmiPosService(
            "terminal-p3", self.integration, client, TreasuryService(self.store),
            self.root / "evidence", StructuredLogger(self.logs, "sunmi-pos-p3"),
        )

    def request(self, operation_id, *, scenario="online_required", kind="purchase", amount=1000, original=None):
        return TransactionRequest.create(
            money=Money(amount, "MXN", 2), profile_ref=self.profile_ref, interface="CONTACT",
            kernel_ref=self.kernel.kernel_ref, transaction_type=kind, scenario=scenario,
            original_operation_id=original.operation_id if original else None,
            original_amount=original.money if original else None,
            operation_id=operation_id, idempotency_key=f"key-{operation_id}",
            correlation_id=f"correlation-{operation_id}",
        )

    def execute(self, request, *, settle=True):
        self.app.reader.reset()
        self.app.reader.configure(self.profile, request)
        return self.pos.execute(request, self.root / "chains" / f"{request.operation_id}.json", settle=settle)

    def purchase(self, operation_id="original"):
        request = self.request(operation_id)
        result = self.execute(request)
        self.assertEqual(result["state"], "APPROVED")
        return request, result

    def restart_application(self, request):
        self.kernel = InternalEmvKernel()
        self.host = MockAuthorizationHost(self.root / "host.sqlite3")
        self.app = self.make_app()
        self.app.reader.configure(self.profile, request)
        self.integration = SunmiIntegration(self.app, self.operation_store)
        self.pos = self.make_pos()

    def test_e2e_01_approved_and_trace(self):
        request, result = self.purchase("e2e-approved")
        self.assertEqual(result["delivery"]["status"], "RECORDED")
        self.assertEqual(result["reconciliation"]["status"], "MATCHED")
        self.assertEqual(result["chain"]["swift_ledger"], "PASS")
        trace = self.pos.trace(request.operation_id)
        self.assertEqual(trace["correlation_id"], request.correlation_id)
        self.assertEqual(trace["persistence"], "APPROVED")
        self.assertIsNotNone(trace["treasury"])
        self.assertTrue((self.root / "evidence" / result["evidence_reference"]).is_file())

    def test_e2e_02_declined(self):
        self.profile = internal_profile(self.kernel, "p3-e2e-declined", "CONTACT", "declined")
        self.profile_ref = f"{self.profile.profile_id}:{self.profile.profile_version}:{self.profile.profile_digest}"
        self.app = self.make_app()
        self.integration = SunmiIntegration(self.app, self.operation_store)
        self.pos = self.make_pos()
        request = self.request("e2e-declined", scenario="declined")
        result = self.execute(request)
        self.assertEqual(result["state"], "DECLINED")
        self.assertEqual(self.store.effect_count(), 0)

    def test_e2e_03_timeout_before_send(self):
        request = self.request("e2e-before-send")
        self.host.unavailable_operations.add(request.operation_id)
        result = self.execute(request)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["contract"]["recovery"]["recovery_action"], "SAFE_TO_RETRY")
        self.assertNotEqual(result["state"], "DECLINED")

    def test_e2e_04_timeout_after_send(self):
        request = self.request("e2e-after-send")
        self.host.timeout_operations.add(request.operation_id)
        result = self.execute(request)
        self.assertEqual(result["state"], "UNCERTAIN")
        self.assertEqual(result["contract"]["recovery"]["recovery_action"], "QUERY_REQUIRED")
        self.assertFalse(result["recovery"]["automatic_retry"])
        self.assertEqual(self.store.effect_count(), 0)

    def test_e2e_05_late_response(self):
        request = self.request("e2e-late")
        self.host.timeout_operations.add(request.operation_id)
        self.assertEqual(self.execute(request)["state"], "UNCERTAIN")
        self.host.publish_late_result(request.operation_id, "APPROVED")
        recovered = self.pos.query(request.operation_id)
        self.assertEqual(recovered["state"], "APPROVED")
        self.assertEqual(recovered["delivery"]["status"], "RECONCILED")
        self.assertEqual(self.store.effect_count(), 1)
        self.assertEqual(self.host.operation_count("authorize"), 1)

    def test_e2e_06_duplicate_transaction(self):
        request, _ = self.purchase("e2e-duplicate")
        duplicate = self.execute(request)
        self.assertFalse(duplicate["delivery"]["created"])
        self.assertEqual(self.store.operation_count(), 1)
        self.assertEqual(self.store.effect_count(), 1)
        self.assertEqual(self.host.operation_count("authorize"), 1)

    def test_e2e_07_concurrent_duplicate(self):
        request = self.request("e2e-concurrent")
        self.app.reader.configure(self.profile, request)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda index: self.pos.execute(
                    request, self.root / "chains" / f"concurrent-{index}.json", settle=False,
                ), range(2),
            ))
        self.assertEqual({item["state"] for item in results}, {"APPROVED"})
        self.assertEqual(self.store.operation_count(), 1)
        self.assertEqual(self.host.operation_count("authorize"), 1)
        self.assertEqual(len(self.treasury_effects()), 1)

    def treasury_effects(self):
        return self.store.consume("verification", 100)

    def test_e2e_08_application_restart(self):
        request, _ = self.purchase("e2e-app-restart")
        self.restart_application(request)
        duplicate = self.execute(request)
        self.assertFalse(duplicate["delivery"]["created"])
        self.assertEqual(self.host.operation_count("authorize"), 1)
        self.assertEqual(self.store.operation_count(), 1)

    def test_e2e_09_backend_restart(self):
        request, _ = self.purchase("e2e-backend-restart")
        self.stop_server()
        self.start_server()
        self.pos = self.make_pos()
        duplicate = self.execute(request)
        self.assertEqual(duplicate["delivery"]["status"], "RECORDED")
        self.assertFalse(duplicate["delivery"]["created"])
        self.assertEqual(self.store.operation_count(), 1)

    def test_e2e_10_host_unavailable(self):
        request = self.request("e2e-host-down")
        self.host.unavailable_operations.add(request.operation_id)
        result = self.execute(request)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["contract"]["recovery"]["command_sent"], False)

    def test_e2e_11_database_unavailable_and_recovery(self):
        request = self.request("e2e-db-down")
        original = self.store.record
        def unavailable(_contract):
            raise PersistenceError("DATABASE_UNAVAILABLE")
        self.store.record = unavailable
        result = self.execute(request)
        self.assertEqual(result["delivery"]["status"], "UNCERTAIN")
        self.assertEqual(result["state"], "APPROVED")
        self.store.record = original
        synchronized = self.pos.sync_pending()
        self.assertTrue(any(item["delivery"]["status"] == "RECORDED" for item in synchronized))
        self.assertEqual(self.store.operation_count(), 1)

    def test_e2e_12_malformed_backend_response(self):
        request = self.request("e2e-malformed-backend")
        original = self.application.post
        self.application.post = lambda *_args: {"ok": True, "simulation": True}
        result = self.execute(request)
        self.application.post = original
        self.assertEqual(result["delivery"]["error"], "MALFORMED_BACKEND_RESPONSE")
        self.assertEqual(result["state"], "APPROVED")
        self.assertNotEqual(result["state"], "DECLINED")

    def test_e2e_13_authentication_failure(self):
        request = self.request("e2e-auth-failure")
        wrong = ServiceAuthenticator(
            {"terminal-p3": ServiceCredentials(b"x" * 32)}, self.store,
        )
        self.pos.ledger = LedgerServiceClient(self.url, "terminal-p3", wrong)
        result = self.execute(request)
        self.assertEqual(result["delivery"]["error"], "INVALID_SIGNATURE")
        self.assertEqual(self.store.operation_count(), 0)
        self.assertNotEqual(result["state"], "DECLINED")

    def test_e2e_14_reconciliation_delayed(self):
        request, result = self.purchase("e2e-recon-delayed")
        self.assertEqual(result["reconciliation"]["status"], "MATCHED")
        delayed = self.pos.treasury.settle()
        self.assertEqual(delayed["reconciliation"]["status"], "MATCHED")
        self.assertEqual(delayed["effects"], [])

    def test_e2e_15_duplicate_reversal(self):
        original, _ = self.purchase("e2e-reversal-original")
        reversal = self.request("e2e-reversal", scenario="approved", kind="reversal", original=original)
        first = self.execute(reversal)
        second = self.execute(reversal)
        self.assertEqual(first["state"], "REVERSED")
        self.assertFalse(second["delivery"]["created"])
        self.assertEqual(self.host.operation_count("reverse"), 1)
        self.assertEqual(self.store.effect_count(), 2)

    def test_e2e_16_refund(self):
        original, _ = self.purchase("e2e-refund-original")
        refund = self.request("e2e-refund", scenario="approved", kind="refund", amount=400, original=original)
        result = self.execute(refund)
        self.assertEqual(result["state"], "APPROVED")
        self.assertEqual(sum(item["amount_minor"] for item in self.store.effects()), 600)

    def test_e2e_17_refund_over_limit(self):
        original, _ = self.purchase("e2e-limit-original")
        first = self.request("e2e-limit-first", scenario="approved", kind="refund", amount=800, original=original)
        self.execute(first)
        excessive = self.request("e2e-limit-excess", scenario="approved", kind="refund", amount=300, original=original)
        result = self.execute(excessive)
        self.assertEqual(result["state"], "DECLINED")
        self.assertEqual(result["contract"]["result"]["error_code"], "EXCESSIVE_REFUND")
        self.assertEqual(sum(item["amount_minor"] for item in self.store.effects()), 200)

    def test_e2e_18_duplicate_refund(self):
        original, _ = self.purchase("e2e-dup-refund-original")
        refund = self.request("e2e-dup-refund", scenario="approved", kind="refund", amount=400, original=original)
        self.execute(refund)
        duplicate = self.execute(refund)
        self.assertFalse(duplicate["delivery"]["created"])
        self.assertEqual(self.host.operation_count("refund"), 1)
        self.assertEqual(sum(item["amount_minor"] for item in self.store.effects()), 600)

    def test_e2e_19_reversal_after_uncertain_and_late_authorization(self):
        original = self.request("e2e-uncertain-original")
        self.host.timeout_operations.add(original.operation_id)
        self.assertEqual(self.execute(original)["state"], "UNCERTAIN")
        reversal = self.request("e2e-uncertain-reversal", scenario="approved", kind="reversal", original=original)
        result = self.execute(reversal)
        self.assertEqual(result["state"], "REVERSED")
        authorizations_before_late = self.host.operation_count("authorize")
        self.assertEqual(self.host.operation_count("reverse"), 1)
        self.host.publish_late_result(original.operation_id, "APPROVED")
        late = self.pos.query(original.operation_id)
        self.assertEqual(late["state"], "APPROVED")
        self.assertEqual(self.host.operation_count("authorize"), authorizations_before_late)
        amounts = {item["transaction_id"]: item["amount_minor"] for item in self.store.effects()}
        self.assertNotIn(original.operation_id, amounts)
        self.assertEqual(amounts[reversal.operation_id], 0)

    def test_e2e_20_recovery_after_restart_without_second_authorization(self):
        request = self.request("e2e-restart-recovery")
        self.host.timeout_operations.add(request.operation_id)
        self.execute(request)
        self.restart_application(request)
        self.host.publish_late_result(request.operation_id, "APPROVED")
        recovered = self.pos.recover(request.operation_id)
        self.assertEqual(recovered["state"], "APPROVED")
        self.assertEqual(self.host.operation_count("authorize"), 1)
        self.assertEqual(self.store.effect_count(), 1)

    def test_structured_logs_are_correlated_and_sanitized(self):
        self.purchase("e2e-logs")
        events = [json.loads(line) for line in self.logs.getvalue().splitlines()]
        event = next(item for item in events if item["operation"] == "ledger_submit")
        for field in ("service", "operation", "state", "correlation_id", "transaction_id", "elapsed_ms", "outcome"):
            self.assertIn(field, event)
        serialized = json.dumps(events)
        for forbidden in ("secret", "password", "track2", "cvv", "pin", "raw_apdu"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
