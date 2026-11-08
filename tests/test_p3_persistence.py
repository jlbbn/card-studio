from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from card_studio.production.contract import CardServiceContract, utc_now
from card_studio.production.postgres import PostgreSQLLedgerStore
from card_studio.production.store import LedgerStore, PersistenceError
from card_studio.production.treasury import TreasuryService


def contract(transaction_id="tx-1", **changes):
    now = utc_now()
    values = {
        "terminal_id": "terminal-persistence",
        "correlation_id": f"correlation-{transaction_id}",
        "transaction_id": transaction_id,
        "idempotency_key": f"key-{transaction_id}",
        "operation_type": "purchase",
        "amount_minor": 1000,
        "currency": "MXN",
        "transaction_state": "APPROVED",
        "created_at": now,
        "updated_at": now,
        "result": {
            "execution_state": "SUCCEEDED", "functional_result": "APPROVED", "error_code": None,
            "host_reference": f"host-{transaction_id}", "host_operation": "authorize", "result_reference": "result.json",
        },
        "recovery": {
            "recovery_action": "NON_RETRYABLE", "command_sent": True, "attempt": 1,
            "original_transaction_id": None,
        },
    }
    values.update(changes)
    return CardServiceContract.from_dict(values)


def linked(transaction_id, kind, original, amount):
    return contract(
        transaction_id, operation_type=kind, amount_minor=amount,
        transaction_state="REVERSED" if kind == "reversal" else "APPROVED",
        result={
            **contract(transaction_id).result,
            "host_operation": "reverse" if kind == "reversal" else "refund",
        },
        recovery={**contract(transaction_id).recovery, "original_transaction_id": original},
    )


class StoreContract:
    def make_store(self):
        raise NotImplementedError

    def test_restart_persistence_and_unique_idempotency(self):
        original = contract()
        created, new = self.store.record(original)
        self.assertTrue(new)
        duplicate, new = self.store.record(original)
        self.assertFalse(new)
        self.assertEqual(created.to_dict(), duplicate.to_dict())
        restarted = self.make_store()
        self.assertEqual(restarted.get(original.terminal_id, original.transaction_id).to_dict(), original.to_dict())
        self.assertEqual(restarted.operation_count(), 1)
        with self.assertRaises(PersistenceError):
            restarted.record(contract(amount_minor=9999))

    def test_concurrent_duplicate_one_effect(self):
        requests = [contract() for _ in range(12)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(self.store.record, requests))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(self.store.operation_count(), 1)
        self.assertEqual(self.store.effect_count(), 1)

    def test_concurrent_refund_limit(self):
        self.store.record(contract("original-refunds"))
        requests = [linked(f"refund-{index}", "refund", "original-refunds", 200) for index in range(8)]
        def record(item):
            try:
                self.store.record(item)
                return "APPROVED"
            except PersistenceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(record, requests))
        self.assertEqual(results.count("APPROVED"), 5)
        self.assertEqual(results.count("REFUND_LIMIT_EXCEEDED"), 3)
        self.assertEqual(sum(item["amount_minor"] for item in self.store.effects()), 0)

    def test_concurrent_and_duplicate_reversal(self):
        self.store.record(contract("original-reversal"))
        requests = [linked(f"reversal-{index}", "reversal", "original-reversal", 1000) for index in range(6)]
        def record(item):
            try:
                self.store.record(item)
                return "REVERSED"
            except PersistenceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(record, requests))
        self.assertEqual(results.count("REVERSED"), 1)
        self.assertEqual(results.count("DUPLICATE_REVERSAL"), 5)
        self.assertEqual(self.store.effect_count(), 2)

    def test_treasury_cursor_restart_and_idempotent_consumption(self):
        for index in range(3):
            self.store.record(contract(f"cursor-{index}"))
        treasury = TreasuryService(self.store, "treasury-restart")
        first = treasury.consume(1)
        second = treasury.consume(1)
        third = treasury.consume(1)
        self.assertEqual(len({item["effect_id"] for item in [*first, *second, *third]}), 3)
        self.assertEqual(treasury.consume(), [])
        restarted = TreasuryService(self.make_store(), "treasury-restart")
        self.assertEqual(restarted.consume(), [])
        self.assertEqual(len(restarted.reconcile(TreasuryService.expectations([*first, *second, *third]))["items"]), 3)

    def test_reconciliation_discrepancies_never_silent(self):
        for index in range(4):
            self.store.record(contract(f"recon-{index}"))
        treasury = TreasuryService(self.store, "treasury-recon")
        consumed = treasury.consume()
        expected = TreasuryService.expectations(consumed[:2])
        expected.append(expected[0])
        expected.append({**TreasuryService.expectations(consumed[2:3])[0], "amount_minor": 9999})
        expected.append({"terminal_id": "terminal-persistence", "transaction_id": "absent", "amount_minor": 1, "currency": "MXN"})
        report = treasury.reconcile(expected)
        statuses = {item["status"] for item in report["items"]}
        self.assertEqual(statuses, {"MATCHED", "DUPLICATE", "MISMATCH", "MISSING", "LATE"})
        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertTrue(report["review_required"])

    def test_partial_failure_rolls_back_operation_and_effect(self):
        original_effect = self.store._effect
        def fail(*_args):
            raise RuntimeError("synthetic partial failure")
        self.store._effect = fail
        with self.assertRaises(Exception):
            self.store.record(contract("partial-failure"))
        self.store._effect = original_effect
        self.assertEqual(self.store.operation_count(), 0)
        self.assertEqual(self.store.effect_count(), 0)


class SQLiteP3PersistenceTests(StoreContract, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "ledger.sqlite3"
        self.store = LedgerStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def make_store(self):
        return LedgerStore(self.path)

    def test_nonce_persistence(self):
        self.assertTrue(self.store.reserve("terminal", "persistent-nonce", utc_now()))
        self.assertFalse(self.make_store().reserve("terminal", "persistent-nonce", utc_now()))


@unittest.skipUnless(os.environ.get("P3_TEST_POSTGRES_DSN"), "PostgreSQL P3 aislado no configurado")
class PostgreSQLP3PersistenceTests(StoreContract, unittest.TestCase):
    def setUp(self):
        self._stores = []
        self.store = self.make_store()
        with self.store.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("""
                TRUNCATE p3_operations, p3_nonces, p3_effects, p3_audit,
                p3_treasury_cursor, p3_treasury_consumed, p3_reconciliation RESTART IDENTITY CASCADE
            """)

    def tearDown(self):
        for store in self._stores:
            store.pool.close()

    def make_store(self):
        store = PostgreSQLLedgerStore(os.environ["P3_TEST_POSTGRES_DSN"], pool_size=16)
        self._stores.append(store)
        return store

    def test_pool_discards_broken_connection_and_reconnects(self):
        self.assertTrue(self.store.ping())
        with self.store.pool.connection() as connection:
            connection.close()
        self.assertTrue(self.store.ping())
        self.store.record(contract("after-reconnect"))
        self.assertEqual(self.store.operation_count(), 1)


if __name__ == "__main__":
    unittest.main()
