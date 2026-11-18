from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from card_studio.evidence.redaction import redact_sensitive
from card_studio.ledger import normalize_base_url

from .contract import CardServiceContract, ContractError, canonical_json
from .security import AuthenticationError, ServiceAuthenticator
from .store import LedgerStore, PersistenceError


OPERATIONS_PATH = "/v1/card/operations"
RESOLVE_PATH = "/v1/card/operations/resolve"
QUERY_PATH = "/v1/card/query"


class BackendError(RuntimeError):
    def __init__(self, code: str, *, sent: bool = True):
        super().__init__(code)
        self.code = code
        self.sent = sent


class LedgerApplication:
    def __init__(self, store: LedgerStore, authenticator: ServiceAuthenticator):
        self.store = store
        self.authenticator = authenticator

    def _authenticated(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        raw: bytes,
        operation_type: str,
    ) -> str:
        return self.authenticator.authenticate(
            method, path, headers, raw, operation_type=operation_type,
        )

    def post(self, path: str, headers: Mapping[str, str], raw: bytes) -> dict[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ContractError("MALFORMED_JSON") from error
        if not isinstance(value, dict):
            raise ContractError("INVALID_PAYLOAD")
        if path == QUERY_PATH:
            self._authenticated("POST", path, headers, raw, "query")
            terminal_id = value.get("terminal_id")
            transaction_id = value.get("transaction_id")
            if not isinstance(terminal_id, str) or not isinstance(transaction_id, str):
                raise ContractError("INVALID_QUERY")
            operation = self.store.get(terminal_id, transaction_id)
            if operation.correlation_id != headers.get("X-Correlation-Id"):
                raise ContractError("CORRELATION_MISMATCH")
            return {"ok": True, "status": "RECORDED", "operation": operation.public_dict(), "simulation": True}
        contract = CardServiceContract.from_dict(value)
        terminal_id = self._authenticated("POST", path, headers, raw, contract.operation_type)
        if terminal_id != contract.terminal_id:
            raise ContractError("TERMINAL_MISMATCH")
        if headers.get("X-Correlation-Id") != contract.correlation_id:
            raise ContractError("CORRELATION_MISMATCH")
        if path == OPERATIONS_PATH:
            operation, created = self.store.record(contract)
            return {
                "ok": True, "status": "RECORDED", "created": created,
                "operation": operation.public_dict(), "simulation": True,
            }
        if path == RESOLVE_PATH:
            operation = self.store.resolve(contract)
            return {"ok": True, "status": "RECONCILED", "operation": operation.public_dict(), "simulation": True}
        raise ContractError("NOT_FOUND")

    def livez(self) -> dict[str, bool]:
        return {"ok": True}

    def readyz(self) -> dict[str, bool]:
        return {"ok": self.store.ping()}


def make_server(address: tuple[str, int], application: LedgerApplication) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CardLedgerP3"
        sys_version = ""

        def _send(self, status: int, value: Mapping[str, Any]) -> None:
            payload = canonical_json(redact_sensitive(dict(value)))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/livez":
                self._send(200, application.livez())
            elif self.path == "/readyz":
                ready = application.readyz()
                self._send(200 if ready["ok"] else 503, ready)
            else:
                self._send(404, {"ok": False, "error": "NOT_FOUND"})

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64 * 1024:
                    raise ContractError("INVALID_PAYLOAD_SIZE")
                raw = self.rfile.read(length)
                value = application.post(self.path, self.headers, raw)
                self._send(200, value)
            except AuthenticationError as error:
                self._send(401, {"ok": False, "error": error.code})
            except (ContractError, PersistenceError) as error:
                status = 503 if error.code == "DATABASE_UNAVAILABLE" else 409 if "CONFLICT" in error.code or "DUPLICATE" in error.code else 400
                self._send(status, {"ok": False, "error": error.code})
            except Exception:
                self._send(500, {"ok": False, "error": "INTERNAL_ERROR"})

        def log_message(self, *_args: Any) -> None:
            return

    return ThreadingHTTPServer(address, Handler)


class LedgerServiceClient:
    def __init__(
        self,
        base_url: str,
        terminal_id: str,
        authenticator: ServiceAuthenticator,
        timeout: float = 2.0,
    ):
        self.base_url = normalize_base_url(base_url)
        self.terminal_id = terminal_id
        self.authenticator = authenticator
        self.timeout = timeout

    def _request(self, path: str, body: Mapping[str, Any], correlation_id: str) -> dict[str, Any]:
        raw = canonical_json(body)
        headers = self.authenticator.headers(self.terminal_id, "POST", path, raw)
        headers["X-Correlation-Id"] = correlation_id
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=raw, method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(64 * 1024 + 1)
        except urllib.error.HTTPError as error:
            try:
                with error:
                    value = json.loads(error.read(64 * 1024).decode("utf-8"))
                code = value.get("error", "BACKEND_REJECTED") if isinstance(value, dict) else "BACKEND_REJECTED"
            except (ValueError, UnicodeDecodeError):
                code = "MALFORMED_BACKEND_RESPONSE"
            raise BackendError(code) from error
        except (socket.timeout, TimeoutError) as error:
            raise BackendError("BACKEND_TIMEOUT_AFTER_SEND") from error
        except (urllib.error.URLError, OSError) as error:
            raise BackendError("BACKEND_UNAVAILABLE_UNCERTAIN") from error
        if len(payload) > 64 * 1024:
            raise BackendError("MALFORMED_BACKEND_RESPONSE")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise BackendError("MALFORMED_BACKEND_RESPONSE") from error
        if (
            not isinstance(value, dict) or value.get("ok") is not True
            or value.get("simulation") is not True or not isinstance(value.get("operation"), dict)
        ):
            raise BackendError("MALFORMED_BACKEND_RESPONSE")
        operation = value["operation"]
        if operation.get("correlation_id") != correlation_id:
            raise BackendError("CORRELATION_MISMATCH")
        return value

    def submit(self, contract: CardServiceContract) -> dict[str, Any]:
        return self._request(OPERATIONS_PATH, contract.to_dict(), contract.correlation_id)

    def resolve(self, contract: CardServiceContract) -> dict[str, Any]:
        return self._request(RESOLVE_PATH, contract.to_dict(), contract.correlation_id)

    def query(self, contract: CardServiceContract) -> dict[str, Any]:
        body = {
            "terminal_id": contract.terminal_id,
            "transaction_id": contract.transaction_id,
        }
        return self._request(QUERY_PATH, body, contract.correlation_id)

    def health(self, path: str) -> dict[str, Any]:
        if path not in {"/livez", "/readyz"}:
            raise ValueError("Ruta health no soportada.")
        try:
            with urllib.request.urlopen(f"{self.base_url}{path}", timeout=self.timeout) as response:
                value = json.loads(response.read(4096).decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                error.read(4096)
            if error.code == 503 and path == "/readyz":
                return {"ok": False}
            raise BackendError("HEALTH_UNAVAILABLE", sent=False) from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise BackendError("HEALTH_UNAVAILABLE", sent=False) from error
        if not isinstance(value, dict) or set(value) != {"ok"} or not isinstance(value["ok"], bool):
            raise BackendError("MALFORMED_HEALTH_RESPONSE", sent=False)
        return value
