from __future__ import annotations

import json
import queue
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .contract import CardServiceContract, canonical_json, utc_now
from .store import PersistenceError

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


class PostgreSQLConnectionPool:
    def __init__(self, dsn: str, max_size: int = 4, connect_timeout: float = 3.0):
        if psycopg is None:
            raise PersistenceError("POSTGRES_DRIVER_UNAVAILABLE")
        if not 1 <= max_size <= 32:
            raise ValueError("max_size debe estar entre 1 y 32.")
        self.dsn = dsn
        self.max_size = max_size
        self.connect_timeout = connect_timeout
        self._available: queue.LifoQueue[Any] = queue.LifoQueue()
        self._lock = threading.Lock()
        self._created = 0
        self._closed = False

    def _new_connection(self) -> Any:
        with self._lock:
            if self._closed:
                raise PersistenceError("CONNECTION_POOL_CLOSED")
            self._created += 1
        try:
            return psycopg.connect(
                self.dsn, autocommit=True, connect_timeout=max(1, int(self.connect_timeout)),
                row_factory=dict_row,
            )
        except Exception:
            with self._lock:
                self._created -= 1
            raise

    def _discard(self, connection: Any) -> None:
        try:
            connection.close()
        finally:
            with self._lock:
                self._created -= 1

    @contextmanager
    def connection(self) -> Iterator[Any]:
        try:
            connection = self._available.get_nowait()
        except queue.Empty:
            with self._lock:
                can_create = self._created < self.max_size and not self._closed
            connection = self._new_connection() if can_create else self._available.get(timeout=self.connect_timeout)
        reusable = False
        try:
            if connection.closed:
                raise PersistenceError("DATABASE_UNAVAILABLE")
            yield connection
            reusable = not connection.closed
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            if reusable:
                self._available.put(connection)
            else:
                self._discard(connection)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        while True:
            try:
                self._discard(self._available.get_nowait())
            except queue.Empty:
                return


class PostgreSQLLedgerStore:
    def __init__(self, dsn: str, *, pool_size: int = 4, migration_path: Path | None = None):
        self.pool = PostgreSQLConnectionPool(dsn, pool_size)
        self._lock = threading.RLock()
        migration = migration_path or Path(__file__).resolve().parents[2] / "migrations" / "001_p3_postgresql.sql"
        try:
            with self.pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute(migration.read_text(encoding="utf-8"))
        except Exception as error:
            self.pool.close()
            raise PersistenceError("DATABASE_UNAVAILABLE") from error

    @staticmethod
    def _contract(row: Mapping[str, Any]) -> CardServiceContract:
        payload = row["payload_json"]
        return CardServiceContract.from_dict(payload if isinstance(payload, dict) else json.loads(payload))

    @staticmethod
    def _audit(cursor: Any, contract: CardServiceContract, event: str) -> None:
        cursor.execute("""
            INSERT INTO p3_audit(terminal_id, transaction_id, event_type, state, correlation_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            contract.terminal_id, contract.transaction_id, event, contract.transaction_state,
            contract.correlation_id, utc_now(),
        ))

    @staticmethod
    def _effect(cursor: Any, contract: CardServiceContract, amount: int | None = None) -> None:
        if amount is None:
            amount = contract.amount_minor if contract.operation_type == "purchase" else -contract.amount_minor
        cursor.execute("""
            INSERT INTO p3_effects(
                effect_id, terminal_id, transaction_id, correlation_id,
                effect_type, amount_minor, currency, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(terminal_id, transaction_id) DO NOTHING
        """, (
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"card-p3:{contract.terminal_id}:{contract.transaction_id}")),
            contract.terminal_id, contract.transaction_id, contract.correlation_id,
            contract.operation_type, amount, contract.currency, utc_now(),
        ))

    @staticmethod
    def _locks(cursor: Any, contract: CardServiceContract) -> None:
        keys = [f"idem:{contract.terminal_id}:{contract.idempotency_key}"]
        original = contract.recovery.get("original_transaction_id")
        if original:
            keys.append(f"original:{contract.terminal_id}:{original}")
        for key in sorted(keys):
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))

    def _linked_error(self, cursor: Any, contract: CardServiceContract) -> str | None:
        if contract.operation_type == "purchase":
            return None
        original_id = contract.recovery["original_transaction_id"]
        cursor.execute(
            "SELECT * FROM p3_operations WHERE terminal_id=%s AND transaction_id=%s",
            (contract.terminal_id, original_id),
        )
        original = cursor.fetchone()
        if original is None or original["operation_type"] != "purchase":
            return "ORIGINAL_TRANSACTION_REQUIRED"
        if contract.currency != original["currency"]:
            return "ORIGINAL_CURRENCY_MISMATCH"
        if contract.transaction_state in {"DECLINED", "FAILED"}:
            return None
        cursor.execute(
            "SELECT * FROM p3_operations WHERE terminal_id=%s AND original_transaction_id=%s",
            (contract.terminal_id, original_id),
        )
        linked = [self._contract(row) for row in cursor.fetchall()]
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
        import time

        last_error = None
        for attempt in range(10):
            try:
                return self._record_once(contract)
            except PersistenceError as error:
                if psycopg is None or not isinstance(error.__cause__, psycopg.errors.SerializationFailure):
                    raise
                last_error = error
                time.sleep(0.005 * (attempt + 1))
        raise PersistenceError("DATABASE_UNAVAILABLE") from last_error

    def _record_once(self, contract: CardServiceContract) -> tuple[CardServiceContract, bool]:
        try:
            with self.pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
                self._locks(cursor, contract)
                cursor.execute(
                    "SELECT * FROM p3_operations WHERE terminal_id=%s AND (transaction_id=%s OR idempotency_key=%s)",
                    (contract.terminal_id, contract.transaction_id, contract.idempotency_key),
                )
                rows = cursor.fetchall()
                if rows:
                    if len(rows) != 1 or rows[0]["request_digest"] != contract.request_digest():
                        raise PersistenceError("IDEMPOTENCY_CONFLICT")
                    current = self._contract(rows[0])
                    current_value = current.to_dict()
                    submitted_value = contract.to_dict()
                    for timestamp_field in ("created_at", "updated_at"):
                        current_value.pop(timestamp_field)
                        submitted_value.pop(timestamp_field)
                    if current_value != submitted_value:
                        raise PersistenceError("RESULT_MUTATION_REQUIRES_RECONCILIATION")
                    connection.commit()
                    return current, False
                linked_error = self._linked_error(cursor, contract)
                if linked_error is not None:
                    raise PersistenceError(linked_error)
                cursor.execute("""
                    INSERT INTO p3_operations(
                        terminal_id, transaction_id, idempotency_key, correlation_id,
                        operation_type, amount_minor, currency, transaction_state,
                        original_transaction_id, request_digest, payload_json, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    contract.terminal_id, contract.transaction_id, contract.idempotency_key,
                    contract.correlation_id, contract.operation_type, contract.amount_minor,
                    contract.currency, contract.transaction_state,
                    contract.recovery.get("original_transaction_id"), contract.request_digest(),
                    json.dumps(contract.to_dict()), contract.created_at, contract.updated_at,
                ))
                self._audit(cursor, contract, "OPERATION_RECORDED")
                if contract.transaction_state in {"APPROVED", "REVERSED"}:
                    amount = None
                    if contract.transaction_state == "REVERSED":
                        cursor.execute(
                            "SELECT transaction_state FROM p3_operations WHERE terminal_id=%s AND transaction_id=%s",
                            (contract.terminal_id, contract.recovery["original_transaction_id"]),
                        )
                        if cursor.fetchone()["transaction_state"] == "UNCERTAIN":
                            amount = 0
                    self._effect(cursor, contract, amount)
                connection.commit()
                return contract, True
        except PersistenceError:
            raise
        except Exception as error:
            raise PersistenceError("DATABASE_UNAVAILABLE") from error

    def get(self, terminal_id: str, transaction_id: str) -> CardServiceContract:
        try:
            with self.pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM p3_operations WHERE terminal_id=%s AND transaction_id=%s",
                    (terminal_id, transaction_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise PersistenceError("TRANSACTION_NOT_FOUND")
                return self._contract(row)
        except PersistenceError:
            raise
        except Exception as error:
            raise PersistenceError("DATABASE_UNAVAILABLE") from error

    def resolve(self, contract: CardServiceContract) -> CardServiceContract:
        if contract.transaction_state not in {"APPROVED", "DECLINED"}:
            raise PersistenceError("LATE_RESULT_NOT_FINAL")
        try:
            with self.pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
                self._locks(cursor, contract)
                cursor.execute(
                    "SELECT * FROM p3_operations WHERE terminal_id=%s AND transaction_id=%s FOR UPDATE",
                    (contract.terminal_id, contract.transaction_id),
                )
                row = cursor.fetchone()
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
                        connection.commit()
                        return current
                    raise PersistenceError("LATE_RESULT_NOT_APPLICABLE")
                if contract.result.get("host_reference") != current.result.get("host_reference"):
                    raise PersistenceError("HOST_REFERENCE_MISMATCH")
                cursor.execute("""
                    UPDATE p3_operations SET transaction_state=%s, payload_json=%s, updated_at=%s
                    WHERE terminal_id=%s AND transaction_id=%s AND transaction_state='UNCERTAIN'
                """, (
                    contract.transaction_state, json.dumps(contract.to_dict()), contract.updated_at,
                    contract.terminal_id, contract.transaction_id,
                ))
                self._audit(cursor, contract, "LATE_RESULT_RECONCILED")
                if contract.transaction_state == "APPROVED":
                    cursor.execute("""
                        SELECT 1 AS found FROM p3_operations WHERE terminal_id=%s
                        AND operation_type='reversal' AND transaction_state IN ('REVERSED','UNCERTAIN')
                        AND original_transaction_id=%s
                    """, (contract.terminal_id, contract.transaction_id))
                    if cursor.fetchone() is None:
                        self._effect(cursor, contract)
                connection.commit()
                return contract
        except PersistenceError:
            raise
        except Exception as error:
            raise PersistenceError("DATABASE_UNAVAILABLE") from error

    def reserve(self, terminal_id: str, nonce: str, timestamp: str) -> bool:
        try:
            with self.pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO p3_nonces(terminal_id, nonce, seen_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (terminal_id, nonce, timestamp),
                )
                return cursor.rowcount == 1
        except Exception as error:
            raise PersistenceError("DATABASE_UNAVAILABLE") from error

    def operation_count(self) -> int:
        return self._count("p3_operations")

    def effect_count(self) -> int:
        return self._count("p3_effects")

    def _count(self, table: str) -> int:
        if table not in {"p3_operations", "p3_effects"}:
            raise ValueError("Tabla no permitida.")
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
            return cursor.fetchone()["count"]

    def effects(self) -> list[dict[str, Any]]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM p3_effects ORDER BY effect_sequence")
            return [dict(row) for row in cursor.fetchall()]

    def consume(self, consumer_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if not consumer_id or not 1 <= limit <= 1000:
            raise PersistenceError("INVALID_TREASURY_CURSOR")
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"consumer:{consumer_id}",))
            cursor.execute("SELECT effect_sequence FROM p3_treasury_cursor WHERE consumer_id=%s", (consumer_id,))
            row = cursor.fetchone()
            position = row["effect_sequence"] if row else 0
            cursor.execute(
                "SELECT * FROM p3_effects WHERE effect_sequence>%s ORDER BY effect_sequence LIMIT %s",
                (position, limit),
            )
            effects = cursor.fetchall()
            consumed = []
            for effect in effects:
                cursor.execute("""
                    INSERT INTO p3_treasury_consumed(
                        consumer_id, effect_id, effect_sequence, terminal_id, transaction_id,
                        correlation_id, amount_minor, currency, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    consumer_id, effect["effect_id"], effect["effect_sequence"], effect["terminal_id"],
                    effect["transaction_id"], effect["correlation_id"], effect["amount_minor"],
                    effect["currency"], utc_now(),
                ))
                position = effect["effect_sequence"]
                consumed.append(dict(effect))
            cursor.execute("""
                INSERT INTO p3_treasury_cursor(consumer_id, effect_sequence, updated_at) VALUES (%s, %s, %s)
                ON CONFLICT(consumer_id) DO UPDATE SET effect_sequence=excluded.effect_sequence, updated_at=excluded.updated_at
            """, (consumer_id, position, utc_now()))
            connection.commit()
            return consumed

    def consumed(self, consumer_id: str) -> list[dict[str, Any]]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM p3_treasury_consumed WHERE consumer_id=%s ORDER BY effect_sequence", (consumer_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def reconcile(self, consumer_id: str, expected: list[Mapping[str, Any]], run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or f"recon-{uuid.uuid4()}"
        actual = self.consumed(consumer_id)
        by_transaction = {(item["terminal_id"], item["transaction_id"]): item for item in actual}
        items = []
        seen = set()
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
                items.append({"item_key": f"{key[0]}:{key[1]}", "status": "LATE", "detail": {"effect_sequence": found["effect_sequence"]}})
        anomalies = [item for item in items if item["status"] != "MATCHED"]
        timestamp = utc_now()
        try:
            with self.pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute("BEGIN")
                for item in items:
                    cursor.execute("""
                        INSERT INTO p3_reconciliation(run_id, item_key, status, detail_json, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (run_id, item["item_key"], item["status"], json.dumps(item["detail"]), timestamp))
                connection.commit()
        except Exception as error:
            raise PersistenceError("RECONCILIATION_RUN_ALREADY_EXISTS") from error
        return {
            "run_id": run_id, "status": "REVIEW_REQUIRED" if anomalies else "MATCHED",
            "items": items, "review_required": bool(anomalies),
        }

    def audit(self, terminal_id: str, transaction_id: str) -> list[dict[str, Any]]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT * FROM p3_audit WHERE terminal_id=%s AND transaction_id=%s ORDER BY sequence
            """, (terminal_id, transaction_id))
            return [dict(row) for row in cursor.fetchall()]

    def ping(self) -> bool:
        try:
            with self.pool.connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                return cursor.fetchone()["ok"] == 1
        except Exception:
            return False

    def close(self) -> None:
        self.pool.close()
