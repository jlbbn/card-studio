from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from card_studio.evidence.redaction import redact_sensitive
from card_studio.transactions.model import TransactionRequest, TransactionType
from card_studio.transactions.store import canonical_request_digest, utc_now

from .base import AuthorizationHostAdapter, HostError
from .evidence import HostEvidenceStore
from .response import build_response_template, validate_response_template


SCHEMA_VERSION = 2


class MockAuthorizationHost(AuthorizationHostAdapter):
    host_ref = "mock-authorization-host-v1"
    host_version = "2"
    mode = "SIMULATED"

    def __init__(
        self,
        path: Path,
        unavailable_operations: set[str] | None = None,
        timeout_operations: set[str] | None = None,
        decline_operations: set[str] | None = None,
        authorization_policy: str = "approved",
        fallback_policy: str = "declined",
        issuer_authentication_data: str | None = None,
        issuer_script_commands: Mapping[str, Sequence[str]] | None = None,
        issuer_script_identifiers: Mapping[str, str] | None = None,
        evidence_dir: Path | None = None,
    ):
        if authorization_policy not in {"approved", "declined", "unable"}:
            raise HostError("INVALID_AUTHORIZATION_POLICY", "Política debe ser approved, declined o unable.")
        if fallback_policy not in {"declined", "approved", "query"}:
            raise HostError("INVALID_FALLBACK_POLICY", "Fallback debe ser declined, approved o query.")
        self.path = path
        self.unavailable_operations = set(unavailable_operations or ())
        self.timeout_operations = set(timeout_operations or ())
        self.decline_operations = set(decline_operations or ())
        self.authorization_policy = authorization_policy
        self.fallback_policy = fallback_policy
        self.issuer_authentication_data = issuer_authentication_data
        self.issuer_script_commands = dict(issuer_script_commands or {})
        self.issuer_script_identifiers = dict(issuer_script_identifiers or {})
        build_response_template(
            "00",
            self.issuer_authentication_data,
            self.issuer_script_commands,
            self.issuer_script_identifiers,
        )
        self.evidence = HostEvidenceStore(evidence_dir) if evidence_dir is not None else None
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
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _host_reference(operation_id: str, request_digest: str) -> str:
        value = f"host-reference\0{operation_id}\0{request_digest}".encode("utf-8")
        return f"hr-{hashlib.sha256(value).hexdigest()[:32]}"

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS host_requests (
                    operation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    command_sent INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    host_reference TEXT UNIQUE,
                    host_operation TEXT NOT NULL DEFAULT 'authorize',
                    original_operation_id TEXT,
                    original_host_reference TEXT,
                    response_template TEXT,
                    response_template_sha256 TEXT,
                    response_template_length INTEGER,
                    authorization_response_code TEXT,
                    policy TEXT NOT NULL DEFAULT 'approved',
                    fallback_policy TEXT NOT NULL DEFAULT 'declined',
                    response_origin TEXT NOT NULL DEFAULT 'HOST_SIMULATED'
                );
                CREATE TABLE IF NOT EXISTS host_late_results (
                    operation_id TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES host_requests(operation_id)
                );
                CREATE TRIGGER IF NOT EXISTS host_late_results_no_update
                BEFORE UPDATE ON host_late_results
                BEGIN SELECT RAISE(ABORT, 'HOST_LATE_RESULT_APPEND_ONLY'); END;
                CREATE TRIGGER IF NOT EXISTS host_late_results_no_delete
                BEFORE DELETE ON host_late_results
                BEGIN SELECT RAISE(ABORT, 'HOST_LATE_RESULT_APPEND_ONLY'); END;
                CREATE TABLE IF NOT EXISTS host_linked_operations (
                    operation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    command_sent INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    host_reference TEXT UNIQUE,
                    host_operation TEXT NOT NULL,
                    original_operation_id TEXT,
                    original_host_reference TEXT,
                    response_template TEXT,
                    response_template_sha256 TEXT,
                    response_template_length INTEGER,
                    authorization_response_code TEXT,
                    policy TEXT NOT NULL DEFAULT 'approved',
                    fallback_policy TEXT NOT NULL DEFAULT 'declined',
                    response_origin TEXT NOT NULL DEFAULT 'HOST_SIMULATED',
                    UNIQUE(host_operation, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS host_linked_late_results (
                    operation_id TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES host_linked_operations(operation_id)
                );
                CREATE TRIGGER IF NOT EXISTS host_linked_late_results_no_update
                BEFORE UPDATE ON host_linked_late_results
                BEGIN SELECT RAISE(ABORT, 'HOST_LATE_RESULT_APPEND_ONLY'); END;
                CREATE TRIGGER IF NOT EXISTS host_linked_late_results_no_delete
                BEFORE DELETE ON host_linked_late_results
                BEGIN SELECT RAISE(ABORT, 'HOST_LATE_RESULT_APPEND_ONLY'); END;
                DROP TRIGGER IF EXISTS host_requests_no_update_response;
                DROP TRIGGER IF EXISTS host_linked_operations_no_update_response;
            """)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2, SCHEMA_VERSION}:
                raise HostError("HOST_SCHEMA_UNSUPPORTED", "Esquema de host no soportado.")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(host_requests)").fetchall()
            }
            additions = {
                "host_reference": "TEXT",
                "host_operation": "TEXT NOT NULL DEFAULT 'authorize'",
                "original_operation_id": "TEXT",
                "original_host_reference": "TEXT",
                "response_template": "TEXT",
                "response_template_sha256": "TEXT",
                "response_template_length": "INTEGER",
                "authorization_response_code": "TEXT",
                "policy": "TEXT NOT NULL DEFAULT 'approved'",
                "fallback_policy": "TEXT NOT NULL DEFAULT 'declined'",
                "response_origin": "TEXT NOT NULL DEFAULT 'HOST_SIMULATED'",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE host_requests ADD COLUMN {name} {declaration}")
            rows = connection.execute("SELECT * FROM host_requests").fetchall()
            for row in rows:
                response = json.loads(row["response_json"])
                if response.get("host_version") == self.host_version and response.get("host_reference"):
                    continue
                request = json.loads(row["request_json"])
                reference = self._host_reference(row["operation_id"], row["request_digest"])
                operation = response.get("host_operation") or (
                    "reverse" if request.get("transaction_type") == TransactionType.REVERSAL.value else "authorize"
                )
                arc = response.get("authorization_response_code")
                if arc is None and response.get("command_sent") and response.get("functional_result") in {"APPROVED", "DECLINED"}:
                    arc = "00" if response["functional_result"] == "APPROVED" else "05"
                response.update({
                    "host_reference": reference,
                    "host_operation": operation,
                    "original_operation_id": request.get("original_operation_id"),
                    "original_host_reference": None,
                    "host_version": self.host_version,
                    "host_ref": self.host_ref,
                    "simulation": True,
                    "synthetic": True,
                    "mode": "SIMULATED",
                    "policy": response.get("policy", "approved"),
                    "fallback_policy": response.get("fallback_policy", "declined"),
                    "response_origin": response.get("response_origin", "HOST_SIMULATED"),
                })
                if arc is not None:
                    template = response.get("response_template") or build_response_template(arc)
                    metadata = validate_response_template(template)
                    response.update(metadata)
                    response["response_template"] = template
                    response["response_tags"] = metadata["response_tags"]
                else:
                    response.update({
                        "response_template": None,
                        "response_template_sha256": None,
                        "response_template_length": None,
                        "authorization_response_code": None,
                    })
                connection.execute("""
                    UPDATE host_requests SET host_reference=?, host_operation=?,
                        original_operation_id=?, original_host_reference=?,
                        response_json=?, response_template=?, response_template_sha256=?,
                        response_template_length=?, authorization_response_code=?,
                        policy=?, fallback_policy=?, response_origin=?
                    WHERE operation_id=?
                """, (
                    reference,
                    operation,
                    request.get("original_operation_id"),
                    None,
                    json.dumps(redact_sensitive(response), sort_keys=True, separators=(",", ":")),
                    response.get("response_template"),
                    response.get("response_template_sha256"),
                    response.get("response_template_length"),
                    response.get("authorization_response_code"),
                    response["policy"],
                    response["fallback_policy"],
                    response["response_origin"],
                    row["operation_id"],
                ))
            for row in connection.execute(
                "SELECT operation_id, original_operation_id FROM host_requests WHERE original_operation_id IS NOT NULL"
            ).fetchall():
                original = connection.execute(
                    "SELECT host_reference FROM host_requests WHERE operation_id=?",
                    (row["original_operation_id"],),
                ).fetchone()
                if original is not None:
                    response = json.loads(connection.execute(
                        "SELECT response_json FROM host_requests WHERE operation_id=?", (row["operation_id"],),
                    ).fetchone()[0])
                    response["original_host_reference"] = original["host_reference"]
                    connection.execute(
                        "UPDATE host_requests SET original_host_reference=?, response_json=? WHERE operation_id=?",
                        (
                            original["host_reference"],
                            json.dumps(redact_sensitive(response), sort_keys=True, separators=(",", ":")),
                            row["operation_id"],
                        ),
                    )
            connection.execute("""
                INSERT OR IGNORE INTO host_linked_operations(
                    operation_id, idempotency_key, request_digest, request_json,
                    response_json, command_sent, created_at, updated_at,
                    host_reference, host_operation, original_operation_id,
                    original_host_reference, response_template, response_template_sha256,
                    response_template_length, authorization_response_code, policy,
                    fallback_policy, response_origin
                )
                SELECT operation_id, idempotency_key, request_digest, request_json,
                    response_json, command_sent, created_at, updated_at,
                    host_reference, host_operation, original_operation_id,
                    original_host_reference, response_template, response_template_sha256,
                    response_template_length, authorization_response_code, policy,
                    fallback_policy, response_origin
                FROM host_requests WHERE host_operation != 'authorize'
            """)
            connection.execute("""
                CREATE TRIGGER IF NOT EXISTS host_requests_no_update_response
                BEFORE UPDATE OF response_json ON host_requests
                WHEN OLD.command_sent = 1
                BEGIN SELECT RAISE(ABORT, 'HOST_RESPONSE_APPEND_ONLY'); END;
            """)
            connection.execute("""
                CREATE TRIGGER IF NOT EXISTS host_linked_operations_no_update_response
                BEFORE UPDATE OF response_json ON host_linked_operations
                WHEN OLD.command_sent = 1
                BEGIN SELECT RAISE(ABORT, 'HOST_RESPONSE_APPEND_ONLY'); END;
            """)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _base_response(
        self,
        request: TransactionRequest,
        operation: str,
        original_host_reference: str | None,
    ) -> dict[str, Any]:
        digest = canonical_request_digest(request)
        return {
            "operation_id": request.operation_id,
            "correlation_id": request.correlation_id,
            "host_reference": self._host_reference(request.operation_id, digest),
            "original_operation_id": request.original_operation_id,
            "original_host_reference": original_host_reference,
            "host_operation": operation,
            "host_ref": self.host_ref,
            "host_version": self.host_version,
            "execution_state": "UNCERTAIN",
            "functional_result": None,
            "decision": None,
            "outcome": None,
            "simulation": True,
            "synthetic": True,
            "recovery_action": "QUERY_REQUIRED",
            "error_code": None,
            "command_sent": False,
            "mode": "SIMULATED",
            "evidence_level": "SIMULATED",
            "physical_validation": "NOT_EXECUTED",
            "response_template": None,
            "response_template_sha256": None,
            "response_template_length": None,
            "response_tag_count": 0,
            "script_count": 0,
            "script_identifier_count": 0,
            "response_tags": [],
            "authorization_response_code": None,
            "policy": self.authorization_policy,
            "fallback_policy": self.fallback_policy,
            "response_origin": "TRANSPORT",
            "timestamp": utc_now(),
        }

    def _apply_template(self, response: dict[str, Any], arc: str) -> None:
        template = build_response_template(
            arc,
            self.issuer_authentication_data,
            self.issuer_script_commands,
            self.issuer_script_identifiers,
        )
        metadata = validate_response_template(template)
        response.update(metadata)
        response["response_template"] = template
        response["authorization_response_code"] = arc

    def _process(
        self,
        request: TransactionRequest,
        operation: str,
        original_host_reference: str | None,
    ) -> dict[str, Any]:
        response = self._base_response(request, operation, original_host_reference)
        if request.operation_id in self.unavailable_operations:
            response.update({
                "execution_state": "FAILED",
                "recovery_action": "SAFE_TO_RETRY",
                "error_code": "HOST_UNAVAILABLE_BEFORE_SEND",
                "policy": "unavailable",
            })
            return response
        if request.operation_id in self.timeout_operations or request.scenario == "timeout":
            response.update({
                "execution_state": "UNCERTAIN",
                "recovery_action": "REVERSAL_REQUIRED" if operation == "reverse" else "QUERY_REQUIRED",
                "error_code": "HOST_TIMEOUT_AFTER_SEND",
                "command_sent": True,
                "policy": "timeout",
            })
            return response
        policy = "declined" if request.operation_id in self.decline_operations or request.scenario == "declined" else self.authorization_policy
        response["policy"] = policy
        response["command_sent"] = True
        response["response_origin"] = "HOST_SIMULATED"
        if policy == "approved":
            response.update({
                "execution_state": "SUCCEEDED",
                "functional_result": "APPROVED",
                "decision": "APPROVED",
                "outcome": "APPROVED",
                "recovery_action": "NON_RETRYABLE",
            })
            self._apply_template(response, "00")
            return response
        if policy == "declined":
            response.update({
                "execution_state": "FAILED",
                "functional_result": "DECLINED",
                "decision": "DECLINED",
                "outcome": "DECLINED",
                "recovery_action": "NON_RETRYABLE",
                "error_code": "HOST_SIMULATED_DECLINE",
            })
            self._apply_template(response, "05")
            return response
        response["response_origin"] = "LOCAL_FALLBACK"
        response["error_code"] = "HOST_UNABLE_TO_GO_ONLINE"
        if self.fallback_policy == "approved":
            response.update({
                "execution_state": "SUCCEEDED",
                "functional_result": "APPROVED",
                "decision": "APPROVED",
                "outcome": "APPROVED",
                "recovery_action": "NON_RETRYABLE",
                "command_sent": False,
            })
            self._apply_template(response, "Y3")
            return response
        if self.fallback_policy == "declined":
            response.update({
                "execution_state": "FAILED",
                "functional_result": "DECLINED",
                "decision": "DECLINED",
                "outcome": "DECLINED",
                "recovery_action": "NON_RETRYABLE",
                "command_sent": False,
            })
            self._apply_template(response, "Z3")
            return response
        response.update({
            "execution_state": "UNCERTAIN",
            "functional_result": None,
            "decision": None,
            "outcome": None,
            "recovery_action": "QUERY_REQUIRED",
            "command_sent": True,
        })
        self._apply_template(response, "Z3")
        return response

    def _write_evidence(self, response: Mapping[str, Any]) -> None:
        if self.evidence is not None:
            self.evidence.write(response)

    def _submit(self, request: TransactionRequest, operation: str) -> Mapping[str, Any]:
        if request.simulation is not True:
            raise HostError("SIMULATION_REQUIRED", "El host mock solo procesa solicitudes simuladas.")
        expected_type = {"reverse": TransactionType.REVERSAL.value, "refund": TransactionType.REFUND.value}
        if operation in expected_type and request.transaction_type != expected_type[operation]:
            raise HostError("INVALID_HOST_OPERATION", f"{operation} requiere una solicitud {expected_type[operation]}.")
        if operation not in {"authorize", "reverse", "refund"}:
            raise HostError("INVALID_HOST_OPERATION", "Operación de host desconocida.")
        digest = canonical_request_digest(request)
        table = "host_requests" if operation == "authorize" else "host_linked_operations"
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE operation_id=? OR idempotency_key=?",
                    (request.operation_id, request.idempotency_key),
                ).fetchone()
                if row is not None:
                    if row["request_digest"] != digest:
                        raise HostError("IDEMPOTENCY_CONFLICT", "La solicitud de host ya existe con otro contenido.")
                    response = json.loads(row["response_json"])
                    connection.execute("COMMIT")
                    self._write_evidence(response)
                    return response
                if operation == "reverse" and request.original_operation_id:
                    duplicate = connection.execute(
                        "SELECT operation_id FROM host_linked_operations WHERE original_operation_id=? AND host_operation='reverse'",
                        (request.original_operation_id,),
                    ).fetchone()
                    if duplicate is not None:
                        raise HostError("DUPLICATE_REVERSAL", "Ya existe un reversal para la operación original.")
                original = None
                original_response = None
                if request.original_operation_id:
                    original = connection.execute(
                        "SELECT host_reference, request_json, response_json FROM host_requests WHERE operation_id=?",
                        (request.original_operation_id,),
                    ).fetchone()
                    if original is not None:
                        original_response = json.loads(original["response_json"])
                        late_original = connection.execute(
                            "SELECT response_json FROM host_late_results WHERE operation_id=?",
                            (request.original_operation_id,),
                        ).fetchone()
                        if late_original is not None:
                            original_response = json.loads(late_original["response_json"])
                if operation == "refund" and original is None:
                    raise HostError(
                        "HOST_ORIGINAL_OPERATION_NOT_FOUND",
                        "Un refund de host requiere la autorización original y su referencia.",
                    )
                if operation == "refund" and (
                    original_response.get("execution_state") != "SUCCEEDED"
                    or original_response.get("functional_result") != "APPROVED"
                ):
                    raise HostError(
                        "HOST_ORIGINAL_NOT_APPROVED",
                        "El refund requiere una autorización original final y aprobada.",
                    )
                original_reference = original["host_reference"] if original is not None else None
                if operation == "refund":
                    original_request = TransactionRequest.from_dict(json.loads(original["request_json"]))
                    refunded = 0
                    for related in connection.execute(
                        "SELECT request_json, response_json FROM host_linked_operations WHERE original_operation_id=? AND host_operation='refund'",
                        (request.original_operation_id,),
                    ).fetchall():
                        related_response = json.loads(related["response_json"])
                        if related_response.get("command_sent") and related_response.get("execution_state") not in {"FAILED", "CANCELLED"}:
                            refunded += TransactionRequest.from_dict(json.loads(related["request_json"])).money.amount_minor
                    if refunded + request.money.amount_minor > original_request.money.amount_minor:
                        raise HostError("EXCESSIVE_REFUND", "El refund acumulado supera el importe original.")
                response = self._process(request, operation, original_reference)
                connection.execute(f"""
                    INSERT INTO {table}(
                        operation_id, idempotency_key, request_digest, request_json,
                        response_json, command_sent, created_at, updated_at,
                        host_reference, host_operation, original_operation_id,
                        original_host_reference, response_template, response_template_sha256,
                        response_template_length, authorization_response_code, policy,
                        fallback_policy, response_origin
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    request.operation_id,
                    request.idempotency_key,
                    digest,
                    json.dumps(redact_sensitive(request.to_dict()), sort_keys=True, separators=(",", ":")),
                    json.dumps(redact_sensitive(response), sort_keys=True, separators=(",", ":")),
                    1 if response["command_sent"] else 0,
                    response["timestamp"],
                    response["timestamp"],
                    response["host_reference"],
                    operation,
                    request.original_operation_id,
                    original_reference,
                    response.get("response_template"),
                    response.get("response_template_sha256"),
                    response.get("response_template_length"),
                    response.get("authorization_response_code"),
                    response["policy"],
                    response["fallback_policy"],
                    response["response_origin"],
                ))
                connection.execute("COMMIT")
                self._write_evidence(response)
                return response
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def authorize(self, request: TransactionRequest, deadline: float | None = None) -> Mapping[str, Any]:
        return self._submit(request, "authorize")

    def reverse(self, request: TransactionRequest, deadline: float | None = None) -> Mapping[str, Any]:
        return self._submit(request, "reverse")

    def refund(self, request: TransactionRequest, deadline: float | None = None) -> Mapping[str, Any]:
        return self._submit(request, "refund")

    def publish_late_result(self, operation_id: str, functional_result: str) -> Mapping[str, Any]:
        decision = str(functional_result).upper()
        if decision not in {"APPROVED", "DECLINED"}:
            raise HostError("INVALID_LATE_RESULT", "El resultado tardío debe ser APPROVED o DECLINED.")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT response_json FROM host_linked_operations WHERE operation_id=?", (operation_id,),
                ).fetchone()
                late_table = "host_linked_late_results"
                if row is None:
                    row = connection.execute(
                        "SELECT response_json FROM host_requests WHERE operation_id=?", (operation_id,),
                    ).fetchone()
                    late_table = "host_late_results"
                if row is None:
                    raise HostError("HOST_OPERATION_NOT_FOUND", "No existe la operación original de host.")
                original = json.loads(row["response_json"])
                if original.get("execution_state") != "UNCERTAIN" or not original.get("command_sent"):
                    raise HostError("LATE_RESULT_NOT_APPLICABLE", "Sólo una respuesta post-send UNCERTAIN admite resultado tardío.")
                response = dict(original)
                response.update({
                    "execution_state": "SUCCEEDED" if decision == "APPROVED" else "FAILED",
                    "functional_result": decision,
                    "decision": decision,
                    "outcome": decision,
                    "recovery_action": "NON_RETRYABLE",
                    "error_code": None if decision == "APPROVED" else "HOST_LATE_DECLINE",
                    "response_origin": "HOST_LATE_RESULT",
                    "late_response_timestamp": utc_now(),
                })
                self._apply_template(response, "00" if decision == "APPROVED" else "05")
                connection.execute(
                    f"INSERT INTO {late_table}(operation_id, response_json, created_at) VALUES (?, ?, ?)",
                    (
                        operation_id,
                        json.dumps(redact_sensitive(response), sort_keys=True, separators=(",", ":")),
                        response["late_response_timestamp"],
                    ),
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as error:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise HostError("LATE_RESULT_ALREADY_EXISTS", "La operación ya tiene un resultado tardío.") from error
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        self._write_evidence(response)
        return response

    def operation_count(self, host_operation: str = "authorize") -> int:
        with self._lock, self._connect() as connection:
            return connection.execute(
                "SELECT (SELECT COUNT(*) FROM host_requests WHERE host_operation=?) + "
                "(SELECT COUNT(*) FROM host_linked_operations WHERE host_operation=?)",
                (host_operation, host_operation),
            ).fetchone()[0]

    def query(self, operation_id: str) -> Mapping[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM host_linked_operations WHERE operation_id=?", (operation_id,),
            ).fetchone()
            late_table = "host_linked_late_results"
            if row is None:
                row = connection.execute(
                    "SELECT * FROM host_requests WHERE operation_id=?", (operation_id,),
                ).fetchone()
                late_table = "host_late_results"
        if row is not None:
            with self._lock, self._connect() as connection:
                late = connection.execute(
                    f"SELECT response_json FROM {late_table} WHERE operation_id=?", (operation_id,),
                ).fetchone()
            response = json.loads(late["response_json"] if late is not None else row["response_json"])
            self._write_evidence(response)
            return response
        return {
            "operation_id": operation_id,
            "host_reference": None,
            "original_host_reference": None,
            "host_operation": "query",
            "host_ref": self.host_ref,
            "host_version": self.host_version,
            "execution_state": "UNCERTAIN",
            "functional_result": None,
            "decision": None,
            "outcome": None,
            "recovery_action": "QUERY_REQUIRED",
            "error_code": "HOST_OPERATION_NOT_FOUND",
            "command_sent": True,
            "simulation": True,
            "synthetic": True,
            "mode": "SIMULATED",
            "evidence_level": "SIMULATED",
            "physical_validation": "NOT_EXECUTED",
            "response_template": None,
            "response_template_sha256": None,
            "response_template_length": None,
            "authorization_response_code": None,
            "timestamp": utc_now(),
        }

    def cancel(self, operation_id: str) -> Mapping[str, Any]:
        response = dict(self.query(operation_id))
        if response.get("execution_state") == "UNCERTAIN":
            response["error_code"] = "HOST_CANCELLATION_UNCERTAIN"
            response["recovery_action"] = "QUERY_REQUIRED"
            response["cancellation_status"] = "NOT_CONFIRMED"
            return response
        if response.get("command_sent"):
            response["cancellation_status"] = "NOT_CANCELLED_ALREADY_SENT"
            return response
        response.update({
            "execution_state": "CANCELLED",
            "functional_result": None,
            "decision": None,
            "outcome": None,
            "recovery_action": "NON_RETRYABLE",
            "error_code": "HOST_CANCELLED",
            "cancellation_status": "CANCELLED",
            "response_template": None,
            "response_template_sha256": None,
            "response_template_length": None,
            "response_tag_count": 0,
            "script_count": 0,
            "script_identifier_count": 0,
            "response_tags": [],
            "authorization_response_code": None,
            "response_origin": "CANCELLED",
            "timestamp": utc_now(),
        })
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for table in ("host_requests", "host_linked_operations"):
                connection.execute(f"""
                    UPDATE {table} SET response_json=?, updated_at=?,
                        response_template=NULL, response_template_sha256=NULL,
                        response_template_length=NULL, authorization_response_code=NULL,
                        response_origin='CANCELLED'
                    WHERE operation_id=? AND command_sent=0
                """, (
                    json.dumps(redact_sensitive(response), sort_keys=True, separators=(",", ":")),
                    response["timestamp"],
                    operation_id,
                ))
            connection.execute("COMMIT")
        self._write_evidence(response)
        return response

    def request_digest(self, request: TransactionRequest) -> str:
        return canonical_request_digest(request)
