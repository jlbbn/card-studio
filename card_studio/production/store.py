from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from .contract import CardServiceContract, canonical_json, utc_now


class PersistenceError(RuntimeError):
    def __init__(self, code: str, message: str = "Persistencia CARD no disponible."):
        super().__init__(message)
        self.code = code
        self.message = message


class LedgerStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._initialize()
        except sqlite3.Error as error:
            raise PersistenceError("DATABASE_UNAVAILABLE") from error

    def _connect(self) -> sqlite3.Connection:
        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, *args: Any) -> None:
                try:
                    super().__exit__(*args)
                finally:
                    self.close()

        connection = sqlite3.connect(
            self.path, timeout=5, isolation_level=None, factory=ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS p3_operations (
                    terminal_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    currency TEXT NOT NULL,
                    transaction_state TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(terminal_id, transaction_id),
                    UNIQUE(terminal_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS p3_original
                    ON p3_operations(terminal_id, json_extract(payload_json, '$.recovery.original_transaction_id'));
                CREATE TABLE IF NOT EXISTS p3_nonces (
                    terminal_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY(terminal_id, nonce)
                );
                CREATE TABLE IF NOT EXISTS p3_effects (
                    effect_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    effect_id TEXT NOT NULL UNIQUE,
                    terminal_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    effect_type TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(terminal_id, transaction_id)
                );
                CREATE TABLE IF NOT EXISTS p3_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    terminal_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS p3_treasury_cursor (
                    consumer_id TEXT PRIMARY KEY,
                    effect_sequence INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS p3_treasury_consumed (
                    consumer_id TEXT NOT NULL,
                    effect_id TEXT NOT NULL,
                    effect_sequence INTEGER NOT NULL,
                    terminal_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(consumer_id, effect_id),
                    UNIQUE(consumer_id, terminal_id, transaction_id)
                );
                CREATE TABLE IF NOT EXISTS p3_reconciliation (
                    run_id TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, item_key, status)
                );
                PRAGMA user_version=1;
            """)

    @staticmethod
    def _contract(row: sqlite3.Row) -> CardServiceContract:
        return CardServiceContract.from_dict(json.loads(row["payload_json"]))

    @staticmethod
    def _audit(connection: sqlite3.Connection, contract: CardServiceContract, event: str) -> None:
        connection.execute("""
            INSERT INTO p3_audit(terminal_id, transaction_id, event_type, state, correlation_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            contract.terminal_id, contract.transaction_id, event, contract.transaction_state,
            contract.correlation_id, utc_now(),
        ))

    @staticmethod
    def _effect(connection: sqlite3.Connection, contract: CardServiceContract, amount: int | None = None) -> None:
        if amount is None:
            amount = contract.amount_minor if contract.operation_type == "purchase" else -contract.amount_minor
        connection.execute("""
            INSERT OR IGNORE INTO p3_effects(
                effect_id, terminal_id, transaction_id, correlation_id,
                effect_type, amount_minor, currency, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"card-p3:{contract.terminal_id}:{contract.transaction_id}")),
            contract.terminal_id, contract.transaction_id, contract.correlation_id,
            contract.operation_type, amount, contract.currency, utc_now(),
        ))

    def _linked_error(self, connection: sqlite3.Connection, contract: CardServiceContract) -> str | None:
        if contract.operation_type == "purchase":
            return None
        original_id = contract.recovery["original_transaction_id"]
        original = connection.execute(
            "SELECT * FROM p3_operations WHERE terminal_id=? AND transaction_id=?",
            (contract.terminal_id, original_id),
        ).fetchone()
        if original is None or original["operation_type"] != "purchase":
            return "ORIGINAL_TRANSACTION_REQUIRED"
        if contract.currency != original["currency"]:
            return "ORIGINAL_CURRENCY_MISMATCH"
        if contract.transaction_state in {"DECLINED", "FAILED"}:
            return None
        linked = [self._contract(row) for row in connection.execute(
            "SELECT * FROM p3_operations WHERE terminal_id=? "
            "AND json_extract(payload_json, '$.recovery.original_transaction_id')=?",
            (contract.terminal_id, original_id),
        ).fetchall()]
        active = [item for item in linked if item.transaction_state in {"APPROVED", "REVERSED", "UNCERTAIN"}]
        if contract.operation_type == "reversal":
            if any(item.operation_type == "reversal" for item in active):
                return "DUPLICATE_REVERSAL"
            if any(item.operation_type == "refund" for item in active):
                return "REFUND_ALREADY_EXISTS"
            if original["transaction_state"] not in {"APPROVED", "UNCERTAIN"}:
                return "INVALID_ORIGINAL_STATE"
            if contract.amount_minor != original["amount_minor"]:
                return "REVERSAL_AMOUNT_MISMATCH"
            return None
        if original["transaction_state"] != "APPROVED":
            return "INVALID_ORIGINAL_STATE"
        if any(item.operation_type == "reversal" for item in active):
            return "REVERSAL_ALREADY_EXISTS"
        refunded = sum(item.amount_minor for item in active if item.operation_type == "refund")
        if refunded + contract.amount_minor > original["amount_minor"]:
            return "REFUND_LIMIT_EXCEEDED"
        return None

    def record(self, contract: CardServiceContract) -> tuple[CardServiceContract, bool]:
        if not isinstance(contract, CardServiceContract):
            raise PersistenceError("INVALID_CONTRACT")
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    by_id = connection.execute(
                        "SELECT * FROM p3_operations WHERE terminal_id=? AND transaction_id=?",
                        (contract.terminal_id, contract.transaction_id),
                    ).fetchone()
                    by_key = connection.execute(
                        "SELECT * FROM p3_operations WHERE terminal_id=? AND idempotency_key=?",
                        (contract.terminal_id, contract.idempotency_key),
                    ).fetchone()
                    existing = by_id or by_key
                    if existing is not None:
                        if existing["request_digest"] != contract.request_digest() or (
                            by_id is not None and by_key is not None and by_id["transaction_id"] != by_key["transaction_id"]
                        ):
                            raise PersistenceError("IDEMPOTENCY_CONFLICT")
                        current = self._contract(existing)
                        current_value = current.to_dict()
                        submitted_value = contract.to_dict()
                        for timestamp_field in ("created_at", "updated_at"):
                            current_value.pop(timestamp_field)
                            submitted_value.pop(timestamp_field)
                        if current_value != submitted_value:
                            raise PersistenceError("RESULT_MUTATION_REQUIRES_RECONCILIATION")
                        connection.execute("COMMIT")
                        return current, False
                    linked_error = self._linked_error(connection, contract)
                    if linked_error is not None:
                        raise PersistenceError(linked_error)
                    connection.execute("""
                        INSERT INTO p3_operations(
                            terminal_id, transaction_id, idempotency_key, correlation_id,
                            operation_type, amount_minor, currency, transaction_state,
                            request_digest, payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        contract.terminal_id, contract.transaction_id, contract.idempotency_key,
                        contract.correlation_id, contract.operation_type, contract.amount_minor,
                        contract.currency, contract.transaction_state, contract.request_digest(),
                        canonical_json(contract.to_dict()).decode("utf-8"), contract.created_at, contract.updated_at,
                    ))
                    self._audit(connection, contract, "OPERATION_RECORDED")
                    if contract.transaction_state in {"APPROVED", "REVERSED"}:
                        amount = None
                        if contract.transaction_state == "REVERSED":
                            original = connection.execute(
                                "SELECT transaction_state FROM p3_operations WHERE terminal_id=? AND transaction_id=?",
                                (contract.terminal_id, contract.recovery["original_transaction_id"]),
                            ).fetchone()
                            if original["transaction_state"] == "UNCERTAIN":
                                amount = 0
                        self._effect(connection, contract, amount)
                    connection.execute("COMMIT")
                    return contract, True
                except Exception:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
        except sqlite3.Error as error:
            raise PersistenceError("DATABASE_UNAVAILABLE") from error

    def get(self, terminal_id: str, transaction_id: str) -> CardServiceContract:
        try:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM p3_operations WHERE terminal_id=? AND transaction_id=?",
                    (terminal_id, transaction_id),
                ).fetchone()
                if row is None:
                    raise PersistenceError("TRANSACTION_NOT_FOUND")
                return self._contract(row)
        except sqlite3.Error as error:
            raise PersistenceError("DATABASE_UNAVAILABLE") from error

    def resolve(self, contract: CardServiceContract) -> CardServiceContract:
        if contract.transaction_state not in {"APPROVED", "DECLINED"}:
            raise PersistenceError("LATE_RESULT_NOT_FINAL")
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        "SELECT * FROM p3_operations WHERE terminal_id=? AND transaction_id=?",
                        (contract.terminal_id, contract.transaction_id),
                    ).fetchone()
                    if row is None:
                        raise PersistenceError("TRANSACTION_NOT_FOUND")
                    current = self._contract(row)
                    if (
                        current.request_digest() != contract.request_digest()
                        or current.correlation_id != contract.correlation_id
                        or current.idempotency_key != contract.idempotency_key
                    ):
                        raise PersistenceError("CORRELATION_MISMATCH")
                    if current.transaction_state != "UNCERTAIN":
                        if current.to_dict() == contract.to_dict():
                            connection.execute("COMMIT")
                            return current
                        raise PersistenceError("LATE_RESULT_NOT_APPLICABLE")
                    if contract.result.get("host_reference") != current.result.get("host_reference"):
                        raise PersistenceError("HOST_REFERENCE_MISMATCH")
                    connection.execute("""
                        UPDATE p3_operations SET transaction_state=?, payload_json=?, updated_at=?
                        WHERE terminal_id=? AND transaction_id=? AND transaction_state='UNCERTAIN'
                    """, (
                        contract.transaction_state, canonical_json(contract.to_dict()).decode("utf-8"),
                        contract.updated_at, contract.terminal_id, contract.transaction_id,
                    ))
                    self._audit(connection, contract, "LATE_RESULT_RECONCILED")
                    if contract.transaction_state == "APPROVED":
                        reversal = connection.execute(
                            "SELECT 1 FROM p3_operations WHERE terminal_id=? "
                            "AND operation_type='reversal' AND transaction_state IN ('REVERSED','UNCERTAIN') "
                            "AND json_extract(payload_json, '$.recovery.original_transaction_id')=?",
                            (contract.terminal_id, contract.transaction_id),
                        ).fetchone()
                        if reversal is None:
                            self._effect(connection, contract)
                    connection.execute("COMMIT")
                    return contract
                except Exception:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
        except sqlite3.Error as error:
            raise PersistenceError("DATABASE_UNAVAILABLE") from error

    def reserve(self, terminal_id: str, nonce: str, timestamp: str) -> bool:
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "INSERT INTO p3_nonces(terminal_id, nonce, seen_at) VALUES (?, ?, ?)",
                        (terminal_id, nonce, timestamp),
                    )
                    connection.execute("COMMIT")
                    return True
                except sqlite3.IntegrityError:
                    connection.execute("ROLLBACK")
                    return False
        except sqlite3.Error as error:
            raise PersistenceError("DATABASE_UNAVAILABLE") from error

    def operation_count(self) -> int:
        with self._lock, self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM p3_operations").fetchone()[0]

    def effect_count(self) -> int:
        with self._lock, self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM p3_effects").fetchone()[0]

    def effects(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM p3_effects ORDER BY effect_sequence").fetchall()
            return [dict(row) for row in rows]

    def consume(self, consumer_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if not consumer_id or not 1 <= limit <= 1000:
            raise PersistenceError("INVALID_TREASURY_CURSOR")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT effect_sequence FROM p3_treasury_cursor WHERE consumer_id=?", (consumer_id,),
                ).fetchone()
                cursor = row[0] if row is not None else 0
                effects = connection.execute(
                    "SELECT * FROM p3_effects WHERE effect_sequence>? ORDER BY effect_sequence LIMIT ?",
                    (cursor, limit),
                ).fetchall()
                consumed = []
                for effect in effects:
                    connection.execute("""
                        INSERT INTO p3_treasury_consumed(
                            consumer_id, effect_id, effect_sequence, terminal_id, transaction_id,
                            correlation_id, amount_minor, currency, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        consumer_id, effect["effect_id"], effect["effect_sequence"], effect["terminal_id"],
                        effect["transaction_id"], effect["correlation_id"], effect["amount_minor"],
                        effect["currency"], utc_now(),
                    ))
                    cursor = effect["effect_sequence"]
                    consumed.append(dict(effect))
                connection.execute("""
                    INSERT INTO p3_treasury_cursor(consumer_id, effect_sequence, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(consumer_id) DO UPDATE SET effect_sequence=excluded.effect_sequence, updated_at=excluded.updated_at
                """, (consumer_id, cursor, utc_now()))
                connection.execute("COMMIT")
                return consumed
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def consumed(self, consumer_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM p3_treasury_consumed WHERE consumer_id=? ORDER BY effect_sequence", (consumer_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def reconcile(
        self,
        consumer_id: str,
        expected: list[Mapping[str, Any]],
        run_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or f"recon-{uuid.uuid4()}"
        actual = self.consumed(consumer_id)
        by_transaction: dict[tuple[str, str], dict[str, Any]] = {
            (item["terminal_id"], item["transaction_id"]): item for item in actual
        }
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for expectation in expected:
            key = (str(expectation["terminal_id"]), str(expectation["transaction_id"]))
            if key in seen:
                items.append({"item_key": f"{key[0]}:{key[1]}", "status": "DUPLICATE", "detail": {}})
                continue
            seen.add(key)
            found = by_transaction.get(key)
            if found is None:
                status = "MISSING"
            elif found["amount_minor"] != expectation["amount_minor"] or found["currency"] != expectation["currency"]:
                status = "MISMATCH"
            else:
                status = "MATCHED"
            items.append({"item_key": f"{key[0]}:{key[1]}", "status": status, "detail": {}})
        for key, found in by_transaction.items():
            if key not in seen:
                items.append({
                    "item_key": f"{key[0]}:{key[1]}", "status": "LATE",
                    "detail": {"effect_sequence": found["effect_sequence"]},
                })
        anomalies = [item for item in items if item["status"] != "MATCHED"]
        summary = "REVIEW_REQUIRED" if anomalies else "MATCHED"
        timestamp = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for item in items:
                    connection.execute("""
                        INSERT INTO p3_reconciliation(run_id, item_key, status, detail_json, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (run_id, item["item_key"], item["status"], canonical_json(item["detail"]).decode(), timestamp))
                connection.execute("COMMIT")
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                raise PersistenceError("RECONCILIATION_RUN_ALREADY_EXISTS")
        return {"run_id": run_id, "status": summary, "items": items, "review_required": bool(anomalies)}

    def audit(self, terminal_id: str, transaction_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("""
                SELECT * FROM p3_audit WHERE terminal_id=? AND transaction_id=? ORDER BY sequence
            """, (terminal_id, transaction_id)).fetchall()
            return [dict(row) for row in rows]

    def ping(self) -> bool:
        try:
            with self._lock, self._connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except (sqlite3.Error, OSError):
            return False
