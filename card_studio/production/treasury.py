from __future__ import annotations

from typing import Any, Mapping

from .store import LedgerStore


class TreasuryService:
    def __init__(self, store: LedgerStore, consumer_id: str = "tesoreria-p3"):
        self.store = store
        self.consumer_id = consumer_id

    def consume(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.consume(self.consumer_id, limit)

    def reconcile(self, expected: list[Mapping[str, Any]], run_id: str | None = None) -> dict[str, Any]:
        return self.store.reconcile(self.consumer_id, expected, run_id)

    @staticmethod
    def expectations(effects: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "terminal_id": effect["terminal_id"],
                "transaction_id": effect["transaction_id"],
                "amount_minor": effect["amount_minor"],
                "currency": effect["currency"],
            }
            for effect in effects
        ]

    def settle(self, limit: int = 100) -> dict[str, Any]:
        effects = self.consume(limit)
        expected = self.expectations(effects)
        reconciliation = self.reconcile(expected) if effects else {
            "run_id": None, "status": "MATCHED", "items": [], "review_required": False,
        }
        return {"effects": effects, "reconciliation": reconciliation}
