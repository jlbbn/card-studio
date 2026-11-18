from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from card_studio.p4.load import run_progressive_load
from card_studio.p4.security import RotatingServiceAuthenticator
from card_studio.production.security import AuthenticationError, ServiceAuthenticator, ServiceCredentials
from card_studio.production.store import LedgerStore

from test_p3_persistence import contract


class P4LoadSecurityTests(unittest.TestCase):
    def test_progressive_load_steps_and_limits(self):
        result = run_progressive_load(lambda _terminal, _operation: 200, steps=(1, 2, 3), max_workers=2)
        self.assertEqual([step["terminals"] for step in result["steps"]], [1, 2, 3])
        self.assertEqual(result["total_requests"], 6)
        self.assertEqual(result["total_errors"], 0)
        self.assertIn("p95_ms", result["steps"][-1])
        self.assertIn("FIXED_SINGLE_TASK", result["autoscaling_behavior"])

    def test_rotation_accepts_previous_and_current_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LedgerStore(Path(directory) / "rotation.db")
            old = {"terminal-p4": ServiceCredentials(b"a" * 32)}
            new = {"terminal-p4": ServiceCredentials(b"b" * 32)}
            rotating = RotatingServiceAuthenticator(new, old, store)
            body = b'{"synthetic":true}'
            old_headers = ServiceAuthenticator(old, store).headers(
                "terminal-p4", "POST", "/v1/card/operations", body,
            )
            self.assertEqual(rotating.authenticate(
                "POST", "/v1/card/operations", old_headers, body, operation_type="purchase",
            ), "terminal-p4")
            with self.assertRaises(AuthenticationError):
                rotating.authenticate("POST", "/v1/card/operations", old_headers, body, operation_type="purchase")
            new_headers = ServiceAuthenticator(new, store).headers(
                "terminal-p4", "POST", "/v1/card/operations", body,
            )
            self.assertEqual(rotating.authenticate(
                "POST", "/v1/card/operations", new_headers, body, operation_type="purchase",
            ), "terminal-p4")

    def test_duplicate_storm_has_one_logical_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LedgerStore(Path(directory) / "storm.db")
            requests = [contract("p4-retry-storm") for _ in range(50)]
            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(store.record, requests))
            self.assertEqual(sum(created for _, created in results), 1)
            self.assertEqual(store.operation_count(), 1)
            self.assertEqual(store.effect_count(), 1)


if __name__ == "__main__":
    unittest.main()
