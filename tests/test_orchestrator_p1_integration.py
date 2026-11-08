from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from card_studio.application import CardStudioApplication
from card_studio.host.mock import MockAuthorizationHost
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.l3.internal import internal_profile
from card_studio.money import Money
from card_studio.profiles import CardProfile
from card_studio.simulator.icc import SyntheticIccReader
from card_studio.transactions.model import TransactionRequest, TransactionType


SEMANTICS = {
    "schema_version": 1,
    "purchase": {"card_kernel_mode": "REQUIRED", "transaction_code": "00", "original_link": "NONE"},
    "refund": {"card_kernel_mode": "REQUIRED", "transaction_code": "20", "original_link": "REQUEST_ORIGINAL"},
    "reversal": {"card_kernel_mode": "HOST_ONLY", "transaction_code": None, "original_link": "HOST_ORIGINAL"},
}


class P1IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.kernel = InternalEmvKernel()
        profile = internal_profile(self.kernel, "orchestrator-host-only", "CONTACT", "online_required")
        raw = profile.to_dict()
        raw["scheme_parameters"] = {
            **raw["scheme_parameters"],
            "kernel_transaction_semantics": SEMANTICS,
        }
        raw["profile_digest"] = ""
        self.profile = CardProfile.from_dict(raw)
        self.profile_ref = f"{self.profile.profile_id}:{self.profile.profile_version}:{self.profile.profile_digest}"
        self.reader = SyntheticIccReader()
        self.host = MockAuthorizationHost(self.root / "host.sqlite3")
        self.app = self.application(self.kernel, self.reader, self.host)

    def tearDown(self):
        self.temporary.cleanup()

    def application(self, kernel, reader, host):
        return CardStudioApplication(
            data_dir=self.root,
            profiles=[self.profile],
            kernels=kernel,
            reader=reader,
            host=host,
        )

    def request(self, operation_id, transaction_type="purchase", original=None):
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
        )

    def purchase(self):
        request = self.request("orchestrator-original")
        self.reader.configure(self.profile, request)
        result = self.app.create_transaction(request)
        self.assertEqual(result["execution_state"], "SUCCEEDED")
        self.assertEqual(result["functional_result"], "APPROVED")
        return result

    def test_host_only_reversal_bypasses_card_kernel_and_apdu(self):
        original = self.purchase()
        transmitted = list(self.reader.commands)
        reversal = self.request(
            "orchestrator-reversal",
            TransactionType.REVERSAL.value,
            original,
        )
        result = self.app.create_transaction(reversal)
        self.assertEqual(result["execution_state"], "SUCCEEDED")
        self.assertEqual(result["functional_result"], "APPROVED")
        self.assertEqual(result["details"]["original_host_reference"], original["details"]["host_reference"])
        self.assertEqual(self.host.operation_count("authorize"), 1)
        self.assertEqual(self.host.operation_count("reverse"), 1)
        self.assertEqual(self.kernel.execution_count(reversal.operation_id), 0)
        self.assertEqual(self.reader.commands, transmitted)
        record = self.app.service.get(reversal.operation_id)
        self.assertEqual(record.result.details["transaction_semantics"]["card_kernel_mode"], "HOST_ONLY")
        self.assertFalse(record.result.details["transaction_semantics"]["second_approval"])
        events = [entry["event_type"] for entry in self.app.audit(reversal.operation_id)]
        self.assertIn("REVERSAL_STARTED", events)
        self.assertIn("REVERSAL_COMPLETED", events)
        self.assertNotIn("KERNEL_COMPLETED", events)

    def test_host_only_reversal_late_result_has_no_card_or_second_host_attempt(self):
        original = self.purchase()
        transmitted = list(self.reader.commands)
        request = self.request(
            "orchestrator-reversal-late",
            TransactionType.REVERSAL.value,
            original,
        )
        timeout_host = MockAuthorizationHost(
            self.root / "host.sqlite3",
            timeout_operations={request.operation_id},
        )
        app = self.application(self.kernel, self.reader, timeout_host)
        uncertain = app.create_transaction(request)
        self.assertEqual(uncertain["execution_state"], "UNCERTAIN")
        self.assertEqual(self.reader.commands, transmitted)
        initial = timeout_host.query(request.operation_id)
        timeout_host.publish_late_result(request.operation_id, "APPROVED")

        restarted_kernel = InternalEmvKernel()
        restarted_reader = SyntheticIccReader()
        restarted_host = MockAuthorizationHost(self.root / "host.sqlite3")
        restarted = self.application(restarted_kernel, restarted_reader, restarted_host)
        result = restarted.query(request.operation_id)
        self.assertEqual(result["execution_state"], "SUCCEEDED")
        self.assertEqual(result["details"]["host_reference"], initial["host_reference"])
        self.assertEqual(result["details"]["host_response_timestamp"], initial["timestamp"])
        self.assertEqual(restarted_kernel.execution_count(request.operation_id), 0)
        self.assertEqual(restarted_reader.commands, [])
        self.assertEqual(restarted_host.operation_count("reverse"), 1)
        events = [entry["event_type"] for entry in restarted.audit(request.operation_id)]
        self.assertEqual(events.count("REVERSAL_STARTED"), 1)


if __name__ == "__main__":
    unittest.main()
