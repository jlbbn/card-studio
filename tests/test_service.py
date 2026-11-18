import tempfile
import unittest
from pathlib import Path

from card_studio.backends import VirtualCardBackend
from card_studio.ledger import LedgerError
from card_studio.service import CardStudioService
from card_studio.store import JsonStore


class FakeLedger:
    def __init__(self, failures=0):
        self.failures = failures
        self.operations = []

    def record(self, operation):
        if self.failures:
            self.failures -= 1
            raise LedgerError("API sandbox no disponible")
        self.operations.append(operation.operation_id)
        return {"status": "RECORDED", "simulation": True}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = JsonStore(Path(self.temporary.name))
        self.service = CardStudioService(self.store)
        self.backend = VirtualCardBackend(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def test_virtual_operations_and_audit_survive_restart(self):
        ledger = FakeLedger()
        self.service.perform(self.backend, "RELOAD", 5000, ledger=ledger)
        self.service.perform(self.backend, "CHARGE", 1725, ledger=ledger)
        restarted = JsonStore(Path(self.temporary.name))
        self.assertEqual(restarted.load_card().balance_minor, 3275)
        self.assertEqual([item.kind for item in restarted.history()], ["CHARGE", "RELOAD"])
        self.assertTrue(all(item.api_status == "RECORDED" for item in restarted.history()))

    def test_pending_operation_retries_with_same_idempotency_key(self):
        ledger = FakeLedger(failures=1)
        _state, operation = self.service.perform(self.backend, "RELOAD", 1000, ledger=ledger)
        self.assertEqual(operation.api_status, "PENDING")
        original_key = operation.idempotency_key
        recorded, error = self.service.sync_pending(ledger)
        self.assertEqual((recorded, error), (1, ""))
        self.assertEqual(self.store.history()[0].idempotency_key, original_key)
        recorded, error = self.service.sync_pending(ledger)
        self.assertEqual((recorded, error), (0, ""))
        self.assertEqual(ledger.operations.count(operation.operation_id), 1)

    def test_failed_sync_preserves_pending_audit(self):
        ledger = FakeLedger(failures=2)
        self.service.perform(self.backend, "RELOAD", 1000, ledger=ledger)
        recorded, error = self.service.sync_pending(ledger)
        self.assertEqual(recorded, 0)
        self.assertIn("no disponible", error)
        self.assertEqual(self.store.history()[0].api_status, "PENDING")


if __name__ == "__main__":
    unittest.main()
