from __future__ import annotations

import logging

from .backends import CardBackend
from .ledger import LedgerClient, LedgerError
from .model import CardState, Operation
from .store import JsonStore

logger = logging.getLogger(__name__)


class CardStudioService:
    def __init__(self, store: JsonStore):
        self.store = store

    def perform(
        self,
        backend: CardBackend,
        kind: str,
        amount_minor: int = 0,
        identifier: str | None = None,
        ledger: LedgerClient | None = None,
    ) -> tuple[CardState, Operation]:
        before = backend.read()
        if kind == "WRITE_ID":
            after = backend.write_id(identifier or "")
        elif kind == "RELOAD":
            after = backend.reload(amount_minor)
        elif kind == "CHARGE":
            after = backend.charge(amount_minor)
        else:
            raise ValueError("Operación no reconocida.")
        operation = Operation.create(
            backend.mode,
            kind,
            after.identifier,
            amount_minor,
            before.balance_minor,
            after.balance_minor,
        )
        self.store.add_operation(operation)
        if ledger is not None:
            self._record(operation, ledger)
        return after, operation

    def _record(self, operation: Operation, ledger: LedgerClient) -> None:
        try:
            ledger.record(operation)
        except LedgerError as error:
            operation.api_message = str(error)
            logger.warning("Operación %s pendiente de API: %s", operation.operation_id, error)
        else:
            operation.api_status = "RECORDED"
            operation.api_message = "Registrado en Swift Ledger sandbox"
        self.store.update_operation(operation)

    def sync_pending(self, ledger: LedgerClient) -> tuple[int, str]:
        recorded = 0
        last_error = ""
        for operation in self.store.history():
            if operation.api_status == "RECORDED":
                continue
            try:
                ledger.record(operation)
            except LedgerError as error:
                operation.api_message = str(error)
                self.store.update_operation(operation)
                last_error = str(error)
                logger.warning("Sincronización pendiente detenida: %s", error)
                break
            operation.api_status = "RECORDED"
            operation.api_message = "Registrado en Swift Ledger sandbox"
            self.store.update_operation(operation)
            recorded += 1
        return recorded, last_error
