from __future__ import annotations

import base64
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from card_studio.application import CardStudioApplication
from card_studio.evidence.redaction import redact_sensitive
from card_studio.host.mock import MockAuthorizationHost
from card_studio.integration.backend import CardOperationStore
from card_studio.integration.sunmi import SunmiIntegration
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.l3.internal import internal_profile
from card_studio.money import Money
from card_studio.production.http import LedgerApplication, LedgerServiceClient, make_server
from card_studio.production.pos import SunmiPosService
from card_studio.production.postgres import PostgreSQLLedgerStore
from card_studio.production.security import ServiceAuthenticator, ServiceCredentials
from card_studio.production.treasury import TreasuryService
from card_studio.simulator.icc import SyntheticIccReader
from card_studio.transactions.model import TransactionRequest

from .migrations import PostgreSQLMigrationRunner


ROOT = Path(__file__).resolve().parents[2]


def _secret() -> bytes:
    encoded = os.environ.get("P4_SERVICE_SECRET", "")
    try:
        value = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        return ServiceCredentials(value).secret
    except ValueError as error:
        raise RuntimeError("INVALID_STAGING_CREDENTIAL") from error


def _credentials(secret: bytes) -> dict[str, ServiceCredentials]:
    return {
        terminal: ServiceCredentials(secret)
        for terminal in ["terminal-p4", *(f"terminal-p4-{index:03d}" for index in range(1, 101))]
    }


def _dsn() -> str:
    value = os.environ.get("P4_DB_DSN", "")
    if not value.startswith("postgresql://"):
        raise RuntimeError("INVALID_STAGING_DB_CONFIG")
    return value


def _store() -> PostgreSQLLedgerStore:
    migration = PostgreSQLMigrationRunner(_dsn(), ROOT / "migrations" / "001_p3_postgresql.sql")
    migration.apply()
    return PostgreSQLLedgerStore(_dsn(), pool_size=int(os.environ.get("P4_DB_POOL_SIZE", "8")))


def _json(value: Any) -> bytes:
    return json.dumps(redact_sensitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class StagingLedgerApplication(LedgerApplication):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._malformed_seen: set[str] = set()

    def post(self, path: str, headers: Mapping[str, str], raw: bytes) -> dict[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8"))
            operation_id = value.get("transaction_id", "") if isinstance(value, dict) else ""
        except (ValueError, UnicodeDecodeError):
            operation_id = ""
        if operation_id and operation_id == os.environ.get("P4_DELAY_OPERATION_ID"):
            time.sleep(float(os.environ.get("P4_DELAY_SECONDS", "3")))
        if operation_id.startswith("malformed") and operation_id not in self._malformed_seen:
            self._malformed_seen.add(operation_id)
            return {"ok": True, "simulation": True}
        return super().post(path, headers, raw)


class PosRuntime:
    def __init__(self, store: PostgreSQLLedgerStore, secret: bytes):
        self.store = store
        self.terminal_id = "terminal-p4"
        self.kernel = InternalEmvKernel()
        self.profile = internal_profile(self.kernel, "p4-staging", "CONTACT", "online_required")
        self.profile_ref = f"{self.profile.profile_id}:{self.profile.profile_version}:{self.profile.profile_digest}"
        host = MockAuthorizationHost(Path("/data/pos/host.sqlite3"))
        self.app = CardStudioApplication(
            data_dir=Path("/data/pos/transactions"), profiles=[self.profile], kernels=self.kernel,
            reader=SyntheticIccReader(), host=host,
        )
        integration = SunmiIntegration(self.app, CardOperationStore(Path("/data/pos/operations")))
        authenticator = ServiceAuthenticator(_credentials(secret), store)
        client = LedgerServiceClient(
            os.environ.get("P4_LEDGER_URL", "http://swiftledger:5000"), self.terminal_id,
            authenticator, timeout=float(os.environ.get("P4_LEDGER_TIMEOUT", "2")),
        )
        self.pos = SunmiPosService(
            self.terminal_id, integration, client, TreasuryService(store), Path("/data/evidence"),
        )

    def execute(self, value: Mapping[str, Any]) -> dict[str, Any]:
        operation_id = str(value["operation_id"])
        kind = str(value.get("operation_type", "purchase"))
        amount = int(value.get("amount_minor", 1000))
        original_id = value.get("original_operation_id")
        original_amount = int(value.get("original_amount_minor", 1000))
        if "host-timeout" in operation_id:
            self.app.host.timeout_operations.add(operation_id)
        request = TransactionRequest.create(
            money=Money(amount, "MXN", 2), profile_ref=self.profile_ref, interface="CONTACT",
            kernel_ref=self.kernel.kernel_ref, transaction_type=kind,
            scenario="approved" if kind != "purchase" else "online_required",
            original_operation_id=str(original_id) if original_id else None,
            original_amount=Money(original_amount, "MXN", 2) if original_id else None,
            operation_id=operation_id, idempotency_key=str(value.get("idempotency_key", f"key-{operation_id}")),
            correlation_id=str(value.get("correlation_id", f"correlation-{operation_id}")),
        )
        self.app.reader.reset()
        self.app.reader.configure(self.profile, request)
        return self.pos.execute(request, Path("/data/evidence") / f"chain-{operation_id}.json", settle=False)

    def query(self, value: Mapping[str, Any]) -> dict[str, Any]:
        operation_id = str(value["operation_id"])
        if value.get("publish_late") is True:
            self.app.host.publish_late_result(operation_id, "APPROVED")
        return self.pos.query(operation_id, settle=False)


class TreasuryRuntime:
    def __init__(self, store: PostgreSQLLedgerStore):
        self.store = store
        self.treasury = TreasuryService(store)

    def settle(self) -> dict[str, Any]:
        effects = []
        for _ in range(100):
            batch = self.treasury.consume()
            if not batch:
                break
            effects.extend(batch)
        delay = float(os.environ.get("P4_SETTLE_DELAY_SECONDS", "0"))
        if delay:
            time.sleep(delay)
        consumed = self.store.consumed(self.treasury.consumer_id)
        reconciliation = self.treasury.reconcile(TreasuryService.expectations(consumed))
        backlog = self.store.effect_count() - len(consumed)
        if backlog:
            reconciliation.update({"status": "REVIEW_REQUIRED", "review_required": True, "backlog": backlog})
        return {"effects": effects, "consumed_total": len(consumed), "backlog": backlog, "reconciliation": reconciliation}


def _role_handler(role: str, runtime: Any, store: PostgreSQLLedgerStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CardP4Staging"
        sys_version = ""

        def _send(self, status: int, value: Mapping[str, Any]) -> None:
            payload = _json(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/livez":
                self._send(200, {"ok": True})
            elif self.path == "/readyz":
                ready = store.ping()
                self._send(200 if ready else 503, {"ok": ready})
            elif self.path == "/metrics":
                value = {
                    "service": role,
                    "release": os.environ.get("P4_RELEASE_VERSION", "unknown"),
                    "operations": store.operation_count(),
                    "effects": store.effect_count(),
                    "reconciliation_lag": store.effect_count() - len(store.consumed("tesoreria-p3")),
                    "simulation": True,
                }
                self._send(200, value)
            else:
                self._send(404, {"ok": False, "error": "NOT_FOUND"})

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32 * 1024:
                    raise ValueError("INVALID_PAYLOAD_SIZE")
                value = json.loads(self.rfile.read(length).decode("utf-8"))
                if role == "sunmi-pos" and self.path == "/execute":
                    result = runtime.execute(value)
                elif role == "sunmi-pos" and self.path == "/query":
                    result = runtime.query(value)
                elif role == "tesoreria" and self.path == "/settle":
                    result = runtime.settle()
                else:
                    self._send(404, {"ok": False, "error": "NOT_FOUND"})
                    return
                self._send(200, {"ok": True, "result": result, "simulation": True})
            except Exception as error:
                self._send(400, {"ok": False, "error": getattr(error, "code", type(error).__name__)})

        def log_message(self, *_args: Any) -> None:
            return

    return Handler


def main() -> int:
    if os.environ.get("P4_ENVIRONMENT") != "STAGING_ISOLATED":
        return 2
    role = os.environ.get("P4_ROLE", "")
    if role not in {"sunmi-pos", "tesoreria", "swiftledger"}:
        return 2
    try:
        secret = _secret()
        store = _store()
        if role == "swiftledger":
            authenticator = ServiceAuthenticator(_credentials(secret), store)
            application = StagingLedgerApplication(store, authenticator)
            server = make_server(("0.0.0.0", 5000), application)
            base_handler = server.RequestHandlerClass
            class LedgerMetricsHandler(base_handler):
                def do_GET(self) -> None:
                    if self.path == "/metrics":
                        self._send(200, {
                            "service": "swiftledger",
                            "release": os.environ.get("P4_RELEASE_VERSION", "unknown"),
                            "operations": store.operation_count(),
                            "effects": store.effect_count(),
                            "reconciliation_lag": store.effect_count() - len(store.consumed("tesoreria-p3")),
                            "simulation": True,
                        })
                    else:
                        super().do_GET()
            server.RequestHandlerClass = LedgerMetricsHandler
        else:
            runtime = PosRuntime(store, secret) if role == "sunmi-pos" else TreasuryRuntime(store)
            port = 5300 if role == "sunmi-pos" else 5301
            server = ThreadingHTTPServer(("0.0.0.0", port), _role_handler(role, runtime, store))
        server.serve_forever()
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
