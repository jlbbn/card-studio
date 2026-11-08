from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .model import Operation

OPERATION_PATH = "/api/pos/card-studio/operations"


class LedgerError(Exception):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_secret(secret: str) -> bytes:
    try:
        padded = secret.strip() + "=" * (-len(secret.strip()) % 4)
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise LedgerError("La llave de terminal no es base64url válida.") from exc
    if len(key) != 32:
        raise LedgerError("La llave de terminal debe decodificar a 32 bytes.")
    return key


def canonical_payment_request(
    terminal_code: str,
    timestamp: str,
    nonce: str,
    path: str,
    body: dict[str, Any],
) -> str:
    body_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return "\n".join(["v1", "PAYMENTS", terminal_code, timestamp, nonce, "POST", path, body_hash])


def normalize_base_url(base_url: str) -> str:
    value = base_url.strip()
    try:
        parsed = urlsplit(value)
        _port = parsed.port
    except ValueError as exc:
        raise LedgerError("La URL contiene un puerto no válido.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise LedgerError("La URL de Swift Ledger debe ser HTTP o HTTPS válida.")
    if parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise LedgerError("La URL no debe incluir credenciales, parámetros ni fragmentos.")
    return value.rstrip("/")


class LedgerClient:
    def __init__(self, base_url: str, terminal_code: str = "", secret_key: str = "", timeout: float = 6.0):
        self.base_url = normalize_base_url(base_url)
        self.terminal_code = terminal_code.strip()
        self.secret_key = secret_key.strip()
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        payload = None if body is None else canonical_json(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload,
            method=method,
            headers={
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if payload is not None else {}),
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read().decode("utf-8"))
                message = value.get("message") or value.get("error") or str(exc)
            except (ValueError, UnicodeDecodeError):
                message = f"Swift Ledger respondió HTTP {exc.code}."
            raise LedgerError(message) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LedgerError("No se pudo conectar con Swift Ledger.") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise LedgerError("Swift Ledger devolvió una respuesta JSON inválida.") from exc
        if not isinstance(value, dict):
            raise LedgerError("Swift Ledger devolvió una respuesta inesperada.")
        return value

    def health(self) -> dict[str, Any]:
        value = self.request("GET", "/api/pos/card-studio/health")
        if value.get("ok") is not True or value.get("simulation") is not True:
            raise LedgerError("El servicio no expone la API sandbox de Card Studio.")
        return value

    def record(self, operation: Operation) -> dict[str, Any]:
        if not self.terminal_code or not self.secret_key:
            raise LedgerError("Captura el código y la llave de la terminal para sincronizar.")
        key = decode_secret(self.secret_key)
        body = operation.to_api()
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        nonce = str(uuid.uuid4())
        canonical = canonical_payment_request(
            self.terminal_code,
            timestamp,
            nonce,
            OPERATION_PATH,
            body,
        )
        signature = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        value = self.request(
            "POST",
            OPERATION_PATH,
            body,
            {
                "x-terminal-code": self.terminal_code,
                "x-payment-timestamp": timestamp,
                "x-payment-nonce": nonce,
                "x-payment-hmac": signature,
            },
        )
        if value.get("status") != "RECORDED" or value.get("simulation") is not True:
            raise LedgerError("Swift Ledger no confirmó el registro sandbox.")
        return value
