from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path

from card_studio.application import CardStudioApplication
from card_studio.integration.backend import (
    BackendIntegrationError,
    CardLedgerBridge,
    CardOperation,
    CardOperationStore,
)
from card_studio.integration.chain import run_integrated_transaction
from card_studio.integration.sunmi import SunmiIntegration
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.l3.internal import internal_profile
from card_studio.money import Money, MoneyValidationError
from card_studio.simulator.icc import SyntheticIccReader
from card_studio.transactions.model import TransactionRequest
from card_studio.transactions.view import card_flow_view


class FakeLedger:
    def __init__(self, failures=0):
        self.failures = failures
        self.operations = []

    def record(self, operation):
        if self.failures:
            self.failures -= 1
            from card_studio.ledger import LedgerError
            raise LedgerError("sandbox unavailable")
        self.operations.append(operation.to_api())
        return {"status": "RECORDED", "simulation": True}


class P2IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.kernel = InternalEmvKernel()
        self.profile = internal_profile(self.kernel, "p2-integration", "CONTACT", "online_required")
        self.profile_ref = f"{self.profile.profile_id}:{self.profile.profile_version}:{self.profile.profile_digest}"
        self.reader = SyntheticIccReader()
        self.app = CardStudioApplication(
            data_dir=self.root / "transactions",
            profiles=[self.profile],
            kernels=self.kernel,
            reader=self.reader,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, operation_id="p2-online", transaction_type="purchase", original=None):
        return TransactionRequest.create(
            money=Money(1000, "MXN", 2),
            profile_ref=self.profile_ref,
            interface="CONTACT",
            kernel_ref=self.kernel.kernel_ref,
            transaction_type=transaction_type,
            scenario="online_required" if transaction_type == "purchase" else "approved",
            original_operation_id=original["operation_id"] if original else None,
            original_amount=Money(1000, "MXN", 2) if original else None,
            operation_id=operation_id,
            idempotency_key=f"key-{operation_id}",
            correlation_id=f"correlation-{operation_id}",
        )

    def test_full_chain_binds_profile_reader_kernel_persistence_and_evidence(self):
        request = self.request()
        self.reader.configure(self.profile, request)
        path = self.root / "evidence" / "chain.json"
        report = run_integrated_transaction(self.app, request, path)
        self.assertEqual(set(report["chain"].values()), {"PASS"})
        self.assertEqual(report["profile"]["profile_digest"], self.profile.profile_digest)
        self.assertEqual(report["kernel"]["kernel_adapter"], "InternalEmvKernel")
        self.assertEqual(report["reader"]["adapter_name"], "stateful-synthetic-icc")
        self.assertEqual(report["reader"]["protocol"], "T=0")
        self.assertEqual(report["correlation_id"], request.correlation_id)
        self.assertEqual(report["transaction"]["host_operation"], "authorize")
        self.assertTrue(report["transaction"]["host_reference"].startswith("hr-"))
        self.assertIsNotNone(report["transaction"]["host_response_timestamp"])
        self.assertEqual(report["transaction"]["execution_state"], "SUCCEEDED")
        self.assertEqual(report["physical_validation"], "NOT_EXECUTED")
        stored = json.loads(path.read_text())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stored["run_id"], report["run_id"])
        result_path = self.root / "transactions" / "evidence" / "transactions" / report["transaction"]["result_reference"]
        self.assertEqual(report["persistence"]["evidence_sha256"], hashlib.sha256(result_path.read_bytes()).hexdigest())
        serialized = json.dumps(report)
        for forbidden in ("raw_apdu", "response_template\"", "issuer_authentication_data", "pan\"", "track2"):
            self.assertNotIn(forbidden, serialized)

    def test_money_and_original_reference_boundaries(self):
        self.assertEqual(Money(0, "JPY", 0).amount_minor, 0)
        with self.assertRaises(MoneyValidationError):
            Money(-1, "MXN", 2)
        with self.assertRaises(MoneyValidationError):
            Money(1, "mxn", 2)
        with self.assertRaises(MoneyValidationError):
            Money(1, "MXN", 4)
        with self.assertRaises(ValueError):
            TransactionRequest.create(
                money=Money(0, "MXN", 2),
                profile_ref=self.profile_ref,
                interface="CONTACT",
                kernel_ref=self.kernel.kernel_ref,
            )
        with self.assertRaises(ValueError):
            TransactionRequest.create(
                money=Money(100, "USD", 2),
                profile_ref=self.profile_ref,
                interface="CONTACT",
                kernel_ref=self.kernel.kernel_ref,
                transaction_type="refund",
                original_operation_id="original",
                original_amount=Money(100, "MXN", 2),
            )

    def test_card_backend_pending_reconcile_and_idempotency(self):
        request = self.request()
        self.reader.configure(self.profile, request)
        integration = SunmiIntegration(self.app, CardOperationStore(self.root / "backend"), FakeLedger(failures=1))
        result = integration.execute(request, self.root / "evidence" / "sunmi.json")
        self.assertEqual(result["state"], "APPROVED")
        self.assertEqual(result["flow"], [
            "CARD_DETECTED", "APPLICATION_SELECTED", "PROCESSING", "ONLINE_REQUIRED", "APPROVED",
        ])
        self.assertEqual(result["operation"]["mode"], "CARD")
        self.assertNotIn("balanceBeforeMinor", result["operation"])
        self.assertEqual(result["operation"]["apiStatus"], "PENDING")
        integration.store.add(CardOperation.from_dict(result["operation"]))
        recorded, error = integration.bridge.sync_pending(FakeLedger())
        self.assertEqual((recorded, error), (1, ""))
        reconciled = integration.bridge.reconcile(request.operation_id, {
            "operationId": request.operation_id,
            "idempotencyKey": request.idempotency_key,
            "status": "RECORDED",
            "simulation": True,
        })
        self.assertEqual(reconciled["status"], "RECONCILED")
        with self.assertRaises(BackendIntegrationError):
            integration.bridge.reconcile(request.operation_id, {
                "operationId": "other",
                "idempotencyKey": request.idempotency_key,
                "status": "RECORDED",
                "simulation": True,
            })
        self.assertEqual(result["receipt"]["title"], "CARD / LAB_ONLY")

    def test_reversal_uses_card_reversed_state_not_cash(self):
        original_request = self.request("p2-original")
        self.reader.configure(self.profile, original_request)
        original = self.app.create_transaction(original_request)
        self.assertEqual(original["execution_state"], "SUCCEEDED")
        store = CardOperationStore(self.root / "backend")
        integration = SunmiIntegration(self.app, store)
        reversal = self.request("p2-reversal", "reversal", original)
        self.reader.reset()
        self.reader.configure(self.profile, reversal)
        result = integration.execute(reversal, self.root / "evidence" / "reversal.json")
        self.assertEqual(result["state"], "REVERSED")
        self.assertEqual(result["operation"]["hostOperation"], "reverse")
        self.assertEqual(result["operation"]["originalHostReference"], original["details"]["host_reference"])
        view = card_flow_view(result["operation"])
        self.assertEqual(view["channel"], "CARD")
        self.assertIsNone(view["cash_state"])
        self.assertEqual(view["actions"], [])


if __name__ == "__main__":
    unittest.main()
