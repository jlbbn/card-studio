from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from card_studio.evidence.redaction import redact_sensitive

from .model import ExecutionState, TransactionError, TransactionRequest, TransactionResult
from .recovery import RecoveryClass


SCHEMA_VERSION = 1


class PersistentState(str, Enum):
    CREATED = "CREATED"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset({
    PersistentState.SUCCEEDED,
    PersistentState.FAILED,
    PersistentState.UNCERTAIN,
    PersistentState.CANCELLED,
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_request_digest(request: TransactionRequest) -> str:
    value = request.to_dict()
    for field in ("operation_id", "idempotency_key", "correlation_id"):
        value.pop(field, None)
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def profile_parts(profile_ref: str) -> tuple[str, int, str]:
    match = re.fullmatch(r"(.+):([1-9][0-9]*):([0-9a-f]{64})", profile_ref)
    if match is None:
        raise TransactionError(
            "INVALID_PROFILE_REF",
            "profile_ref debe incluir profile_id, profile_version y profile_digest.",
        )
    return match.group(1), int(match.group(2)), match.group(3)


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    operation_id: str
    event_type: str
    previous_state: str | None
    new_state: str
    timestamp: str
    reason: str
    simulation: bool
    kernel_ref: str
    profile_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "operation_id": self.operation_id,
            "event_type": self.event_type,
            "previous_state": self.previous_state,
            "new_state": self.new_state,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "simulation": self.simulation,
            "kernel_ref": self.kernel_ref,
            "profile_ref": self.profile_ref,
        }


@dataclass(frozen=True)
class TransactionRecord:
    request: TransactionRequest
    execution_state: PersistentState
    functional_result: str | None
    recovery_action: RecoveryClass | None
    kernel_adapter: str
    kernel_version: str
    kernel_mode: str
    request_digest: str
    created_at: str
    updated_at: str
    result_reference: str | None
    result: TransactionResult | None

    @property
    def operation_id(self) -> str:
        return self.request.operation_id

    @property
    def profile_ref(self) -> str:
        return self.request.profile_ref

    @property
    def kernel_ref(self) -> str:
        return self.request.kernel_ref

    @property
    def simulation(self) -> bool:
        return self.request.simulation

    def to_dict(self) -> dict[str, Any]:
        profile_id, profile_version, profile_digest = profile_parts(self.profile_ref)
        value = {
            "operation_id": self.operation_id,
            "idempotency_key": self.request.idempotency_key,
            "correlation_id": self.request.correlation_id,
            "operation_type": self.request.transaction_type,
            "transaction_type": self.request.transaction_type,
            "profile_ref": self.profile_ref,
            "profile_id": profile_id,
            "profile_version": profile_version,
            "profile_digest": profile_digest,
            "interface": self.request.interface,
            "kernel_ref": self.kernel_ref,
            "kernel_adapter": self.kernel_adapter,
            "kernel_version": self.kernel_version,
            "kernel_mode": self.kernel_mode,
            "amount": self.request.money.amount_minor,
            "amount_minor": self.request.money.amount_minor,
            "currency": self.request.money.currency,
            "exponent": self.request.money.exponent,
            "simulation": True,
            "execution_state": self.execution_state.value,
            "functional_result": self.functional_result,
            "recovery_action": self.recovery_action.value if self.recovery_action else None,
            "original_operation_id": self.request.original_operation_id,
            "original_amount": self.request.original_amount.to_dict() if self.request.original_amount else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result_reference": self.result_reference,
            "request_digest": self.request_digest,
            "scenario": self.request.scenario,
        }
        if self.result is not None:
            value["error_code"] = self.result.error_code
        return value


class TransactionStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    self.close()

        connection = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None, factory=ClosingConnection,
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
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    operation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    correlation_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    transaction_type TEXT NOT NULL,
                    profile_ref TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    profile_version INTEGER NOT NULL,
                    profile_digest TEXT NOT NULL,
                    interface TEXT NOT NULL,
                    kernel_ref TEXT NOT NULL,
                    kernel_adapter TEXT NOT NULL,
                    kernel_version TEXT NOT NULL,
                    kernel_mode TEXT NOT NULL CHECK(kernel_mode IN ('SIMULATED', 'LAB / SYNTHETIC')),
                    amount INTEGER NOT NULL CHECK(amount > 0),
                    currency TEXT NOT NULL,
                    exponent INTEGER NOT NULL,
                    simulation INTEGER NOT NULL CHECK(simulation = 1),
                    execution_state TEXT NOT NULL,
                    functional_result TEXT,
                    recovery_action TEXT,
                    original_operation_id TEXT,
                    original_amount INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_reference TEXT
                );
                CREATE INDEX IF NOT EXISTS transactions_pending
                    ON transactions(execution_state, created_at, operation_id);
                CREATE INDEX IF NOT EXISTS transactions_original
                    ON transactions(original_operation_id, transaction_type, execution_state);
                CREATE TABLE IF NOT EXISTS audit_trail (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    previous_state TEXT,
                    new_state TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    simulation INTEGER NOT NULL CHECK(simulation = 1),
                    kernel_ref TEXT NOT NULL,
                    profile_ref TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES transactions(operation_id)
                );
                CREATE TRIGGER IF NOT EXISTS audit_trail_no_update
                BEFORE UPDATE ON audit_trail
                BEGIN SELECT RAISE(ABORT, 'AUDIT_TRAIL_APPEND_ONLY'); END;
                CREATE TRIGGER IF NOT EXISTS audit_trail_no_delete
                BEFORE DELETE ON audit_trail
                BEGIN SELECT RAISE(ABORT, 'AUDIT_TRAIL_APPEND_ONLY'); END;
            """)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError("La versión del almacén transaccional no es compatible.")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.execute(
                "INSERT OR IGNORE INTO schema_metadata(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _audit(row: sqlite3.Row) -> AuditEntry:
        return AuditEntry(
            sequence=row["sequence"],
            operation_id=row["operation_id"],
            event_type=row["event_type"],
            previous_state=row["previous_state"],
            new_state=row["new_state"],
            timestamp=row["timestamp"],
            reason=row["reason"],
            simulation=bool(row["simulation"]),
            kernel_ref=row["kernel_ref"],
            profile_ref=row["profile_ref"],
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> TransactionRecord:
        request = TransactionRequest.from_dict(json.loads(row["request_json"]))
        result = TransactionResult.from_dict(json.loads(row["result_json"])) if row["result_json"] else None
        return TransactionRecord(
            request=request,
            execution_state=PersistentState(row["execution_state"]),
            functional_result=row["functional_result"],
            recovery_action=RecoveryClass(row["recovery_action"]) if row["recovery_action"] else None,
            kernel_adapter=row["kernel_adapter"],
            kernel_version=row["kernel_version"],
            kernel_mode=row["kernel_mode"],
            request_digest=row["request_digest"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result_reference=row["result_reference"],
            result=result,
        )

    @staticmethod
    def _get(connection: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM transactions WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise TransactionError("OPERATION_NOT_FOUND", "No existe la operación solicitada.")
        return row

    @staticmethod
    def _get_by_key(connection: sqlite3.Connection, idempotency_key: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM transactions WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        event_type: str,
        previous_state: str | None,
        new_state: str,
        timestamp: str,
        reason: str,
    ) -> AuditEntry:
        safe_reason = redact_sensitive({"value": reason})["value"]
        cursor = connection.execute("""
            INSERT INTO audit_trail(
                operation_id, event_type, previous_state, new_state, timestamp,
                reason, simulation, kernel_ref, profile_ref
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        """, (
            row["operation_id"], event_type, previous_state, new_state, timestamp,
            safe_reason, row["kernel_ref"], row["profile_ref"],
        ))
        return AuditEntry(
            sequence=cursor.lastrowid,
            operation_id=row["operation_id"],
            event_type=event_type,
            previous_state=previous_state,
            new_state=new_state,
            timestamp=timestamp,
            reason=safe_reason,
            simulation=True,
            kernel_ref=row["kernel_ref"],
            profile_ref=row["profile_ref"],
        )

    @staticmethod
    def _linked_failure(
        connection: sqlite3.Connection,
        request: TransactionRequest,
    ) -> TransactionResult | None:
        if request.transaction_type == "purchase":
            return None

        def failure(code: str, recovery: RecoveryClass, refunded: int | None = None) -> TransactionResult:
            details = {} if refunded is None else {"refunded_amount_minor": refunded}
            return TransactionResult(
                request, ExecutionState.FAILED, "DECLINED", recovery, code, details,
            )

        original = connection.execute(
            "SELECT * FROM transactions WHERE operation_id=?", (request.original_operation_id,),
        ).fetchone()
        if original is None:
            return failure("ORIGINAL_OPERATION_NOT_FOUND", RecoveryClass.OPERATOR_REVIEW)
        original_approved = (
            original["execution_state"] == PersistentState.SUCCEEDED.value
            and original["functional_result"] == "APPROVED"
        )
        reversible_uncertain = (
            request.transaction_type == "reversal"
            and original["execution_state"] == PersistentState.UNCERTAIN.value
        )
        if original["transaction_type"] != "purchase" or not (original_approved or reversible_uncertain):
            return failure("INVALID_ORIGINAL_OPERATION", RecoveryClass.NON_RETRYABLE)
        original_request = TransactionRequest.from_dict(json.loads(original["request_json"]))
        if request.original_amount != original_request.money:
            return failure("ORIGINAL_AMOUNT_MISMATCH", RecoveryClass.OPERATOR_REVIEW)
        active = connection.execute("""
            SELECT transaction_type, amount FROM transactions
            WHERE original_operation_id=?
              AND execution_state IN ('CREATED', 'IN_PROGRESS', 'SUCCEEDED', 'UNCERTAIN')
        """, (request.original_operation_id,)).fetchall()
        reversals = [row for row in active if row["transaction_type"] == "reversal"]
        refunds = [row for row in active if row["transaction_type"] == "refund"]
        if request.transaction_type == "reversal":
            if reversals:
                return failure("DUPLICATE_REVERSAL", RecoveryClass.NON_RETRYABLE)
            if refunds:
                return failure("REFUND_ALREADY_EXISTS", RecoveryClass.OPERATOR_REVIEW)
            if request.money != original_request.money:
                return failure("REVERSAL_AMOUNT_MISMATCH", RecoveryClass.NON_RETRYABLE)
            return None
        refunded = sum(row["amount"] for row in refunds)
        if refunded + request.money.amount_minor > original_request.money.amount_minor:
            return failure("EXCESSIVE_REFUND", RecoveryClass.NON_RETRYABLE, refunded)
        return None

    def create(
        self,
        request: TransactionRequest,
        kernel_adapter: str,
        kernel_version: str,
        kernel_mode: str,
    ) -> tuple[TransactionRecord, bool, AuditEntry | None]:
        if request.simulation is not True:
            raise TransactionError("SIMULATION_REQUIRED", "La operación debe ser simulada.")
        profile_id, profile_version, profile_digest = profile_parts(request.profile_ref)
        digest = canonical_request_digest(request)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                by_operation = connection.execute(
                    "SELECT * FROM transactions WHERE operation_id=?", (request.operation_id,)
                ).fetchone()
                if by_operation is not None:
                    existing = self._record(by_operation)
                    if existing.request.fingerprint() != request.fingerprint():
                        raise TransactionError("DUPLICATE_OPERATION", "operation_id ya existe con otro request.")
                    return existing, False, None
                by_key = self._get_by_key(connection, request.idempotency_key)
                if by_key is not None:
                    if by_key["request_digest"] != digest:
                        raise TransactionError(
                            "IDEMPOTENCY_CONFLICT",
                            "idempotency_key ya existe con otro request canónico.",
                        )
                    return self._record(by_key), False, None
                linked_failure = self._linked_failure(connection, request)
                timestamp = utc_now()
                request_json = json.dumps(
                    redact_sensitive(request.to_dict()), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                connection.execute("""
                    INSERT INTO transactions(
                        operation_id, idempotency_key, correlation_id, request_digest,
                        request_json, transaction_type, profile_ref, profile_id,
                        profile_version, profile_digest, interface, kernel_ref,
                        kernel_adapter, kernel_version, kernel_mode, amount, currency,
                        exponent, simulation, execution_state, original_operation_id,
                        original_amount, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                              'CREATED', ?, ?, ?, ?)
                """, (
                    request.operation_id, request.idempotency_key, request.correlation_id,
                    digest, request_json, request.transaction_type, request.profile_ref,
                    profile_id, profile_version, profile_digest, request.interface,
                    request.kernel_ref, kernel_adapter, kernel_version, kernel_mode,
                    request.money.amount_minor, request.money.currency, request.money.exponent,
                    request.original_operation_id,
                    request.original_amount.amount_minor if request.original_amount else None,
                    timestamp, timestamp,
                ))
                row = self._get(connection, request.operation_id)
                audit = self._insert_audit(
                    connection, row, "TRANSACTION_CREATED", None, "CREATED", timestamp, "REQUEST_VALIDATED"
                )
                if linked_failure is not None:
                    result_json = json.dumps(
                        redact_sensitive(linked_failure.to_dict()),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    connection.execute("""
                        UPDATE transactions SET result_json=?, execution_state='FAILED',
                            functional_result='DECLINED', recovery_action=?, updated_at=?
                        WHERE operation_id=?
                    """, (
                        result_json, linked_failure.recovery.value, timestamp, request.operation_id,
                    ))
                    row = self._get(connection, request.operation_id)
                    self._insert_audit(
                        connection, row, "TRANSACTION_FAILED", "CREATED", "FAILED",
                        timestamp, linked_failure.error_code or "LINKED_OPERATION_REJECTED",
                    )
                connection.execute("COMMIT")
                return self._record(row), True, audit
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def get(self, operation_id: str) -> TransactionRecord:
        with self._lock, self._connect() as connection:
            return self._record(self._get(connection, operation_id))

    def list(self, limit: int = 100) -> list[TransactionRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit debe estar entre 1 y 1000.")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transactions ORDER BY created_at DESC, operation_id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._record(row) for row in rows]

    def list_pending(self) -> list[TransactionRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("""
                SELECT * FROM transactions
                WHERE execution_state IN ('IN_PROGRESS', 'UNCERTAIN')
                ORDER BY created_at, operation_id
            """).fetchall()
            return [self._record(row) for row in rows]

    def related(self, original_operation_id: str) -> list[TransactionRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transactions WHERE original_operation_id=? ORDER BY created_at, operation_id",
                (original_operation_id,),
            ).fetchall()
            return [self._record(row) for row in rows]

    def mark_in_progress(self, operation_id: str) -> tuple[TransactionRecord, bool, AuditEntry | None]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._get(connection, operation_id)
                if row["execution_state"] != PersistentState.CREATED.value:
                    connection.execute("COMMIT")
                    return self._record(row), False, None
                timestamp = utc_now()
                connection.execute("""
                    UPDATE transactions SET execution_state='IN_PROGRESS', updated_at=?
                    WHERE operation_id=? AND execution_state='CREATED'
                """, (timestamp, operation_id))
                updated = self._get(connection, operation_id)
                audit = self._insert_audit(
                    connection, updated, "KERNEL_STARTED", "CREATED", "IN_PROGRESS", timestamp, "EXECUTION_STARTED"
                )
                connection.execute("COMMIT")
                return self._record(updated), True, audit
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def mark_retry(self, operation_id: str) -> tuple[TransactionRecord, AuditEntry]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._get(connection, operation_id)
                if (
                    row["execution_state"] != PersistentState.FAILED.value
                    or row["recovery_action"] != RecoveryClass.SAFE_TO_RETRY.value
                ):
                    raise TransactionError(
                        "NON_RETRYABLE",
                        "Solo un FAILED con SAFE_TO_RETRY admite reintento explícito.",
                    )
                timestamp = utc_now()
                connection.execute("""
                    UPDATE transactions SET execution_state='IN_PROGRESS', result_json=NULL,
                        functional_result=NULL, recovery_action=NULL, updated_at=?
                    WHERE operation_id=? AND execution_state='FAILED'
                """, (timestamp, operation_id))
                updated = self._get(connection, operation_id)
                audit = self._insert_audit(
                    connection, updated, "KERNEL_STARTED", "FAILED", "IN_PROGRESS",
                    timestamp, "EXPLICIT_SAFE_RETRY",
                )
                connection.execute("COMMIT")
                return self._record(updated), audit
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def record_event(
        self,
        operation_id: str,
        event_type: str,
        reason: str,
        previous_state: str | None = None,
        new_state: str | None = None,
    ) -> AuditEntry:
        if not re.fullmatch(r"[A-Z][A-Z_]{2,63}", event_type):
            raise ValueError("event_type no tiene un formato válido.")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._get(connection, operation_id)
                timestamp = utc_now()
                audit = self._insert_audit(
                    connection,
                    row,
                    event_type,
                    row["execution_state"] if previous_state is None else previous_state,
                    row["execution_state"] if new_state is None else new_state,
                    timestamp,
                    reason,
                )
                connection.execute("COMMIT")
                return audit
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def complete(
        self,
        result: TransactionResult,
        reason: str | None = None,
    ) -> tuple[TransactionRecord, AuditEntry | None]:
        operation_id = result.operation_id
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._get(connection, operation_id)
                stored_request = TransactionRequest.from_dict(json.loads(row["request_json"]))
                if stored_request.fingerprint() != result.request.fingerprint():
                    raise TransactionError(
                        "RESULT_REQUEST_MISMATCH",
                        "El resultado no corresponde al request persistente.",
                    )
                current = PersistentState(row["execution_state"])
                if current not in {
                    PersistentState.CREATED,
                    PersistentState.IN_PROGRESS,
                    PersistentState.UNCERTAIN,
                }:
                    connection.execute("COMMIT")
                    return self._record(row), None
                new_state = PersistentState(result.execution_state.value)
                timestamp = utc_now()
                functional_result = result.details.get("functional_result", result.outcome)
                result_json = json.dumps(
                    redact_sensitive(result.to_dict()), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                connection.execute("""
                    UPDATE transactions SET
                        result_json=?, execution_state=?, functional_result=?, recovery_action=?, updated_at=?
                    WHERE operation_id=?
                """, (
                    result_json, new_state.value, functional_result, result.recovery.value,
                    timestamp, operation_id,
                ))
                updated = self._get(connection, operation_id)
                events = {
                    PersistentState.SUCCEEDED: "TRANSACTION_SUCCEEDED",
                    PersistentState.FAILED: "TRANSACTION_DECLINED" if functional_result == "DECLINED" else "TRANSACTION_FAILED",
                    PersistentState.UNCERTAIN: "TRANSACTION_UNCERTAIN",
                    PersistentState.CANCELLED: "TRANSACTION_CANCELLED",
                }
                audit = self._insert_audit(
                    connection,
                    updated,
                    events[new_state],
                    current.value,
                    new_state.value,
                    timestamp,
                    reason or result.error_code or functional_result or new_state.value,
                )
                connection.execute("COMMIT")
                return self._record(updated), audit
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def set_result_reference(self, operation_id: str, reference: str) -> TransactionRecord:
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.json", reference):
            raise ValueError("result_reference debe ser un nombre de evidencia JSON.")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._get(connection, operation_id)
                connection.execute("""
                    UPDATE transactions SET result_reference=?, updated_at=? WHERE operation_id=?
                """, (reference, utc_now(), operation_id))
                row = self._get(connection, operation_id)
                connection.execute("COMMIT")
                return self._record(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def audit(self, operation_id: str) -> list[AuditEntry]:
        with self._lock, self._connect() as connection:
            self._get(connection, operation_id)
            rows = connection.execute(
                "SELECT * FROM audit_trail WHERE operation_id=? ORDER BY sequence", (operation_id,)
            ).fetchall()
            return [self._audit(row) for row in rows]

    def ping(self) -> bool:
        with self._lock, self._connect() as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            triggers = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
            return {"transactions", "audit_trail", "schema_metadata"} <= tables and {
                "audit_trail_no_update", "audit_trail_no_delete"
            } <= triggers
