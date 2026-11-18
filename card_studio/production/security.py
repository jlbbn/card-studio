from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from .contract import canonical_json


class AuthenticationError(ValueError):
    def __init__(self, code: str, message: str = "Autenticación de servicio rechazada."):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ServiceCredentials:
    secret: bytes
    operations: frozenset[str] = frozenset({"purchase", "refund", "reversal", "query"})

    def __post_init__(self) -> None:
        if len(self.secret) != 32:
            raise ValueError("La credencial de servicio debe contener 32 bytes.")
        object.__setattr__(self, "operations", frozenset(self.operations))


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as error:
        raise AuthenticationError("INVALID_TIMESTAMP") from error
    if parsed.tzinfo is None:
        raise AuthenticationError("INVALID_TIMESTAMP")
    return parsed.astimezone(timezone.utc)


def authentication_payload(
    method: str,
    path: str,
    terminal_id: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join((
        "CARD-P3-v1", method.upper(), path, terminal_id, timestamp, nonce, body_hash,
    )).encode("utf-8")


class ServiceAuthenticator:
    def __init__(
        self,
        credentials: Mapping[str, ServiceCredentials],
        nonce_store: object,
        max_age_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
    ):
        self._credentials = dict(credentials)
        self._nonces = nonce_store
        self.max_age_seconds = max_age_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def headers(
        self,
        terminal_id: str,
        method: str,
        path: str,
        body: Mapping[str, object] | bytes | None = None,
        *,
        timestamp: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        try:
            credentials = self._credentials[terminal_id]
        except KeyError as error:
            raise AuthenticationError("INVALID_CREDENTIALS") from error
        raw = canonical_json(body) if isinstance(body, Mapping) else body or b""
        timestamp = timestamp or self._clock().isoformat(timespec="microseconds").replace("+00:00", "Z")
        nonce = nonce or str(uuid.uuid4())
        signature = hmac.new(
            credentials.secret,
            authentication_payload(method, path, terminal_id, timestamp, nonce, raw),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-Card-Terminal": terminal_id,
            "X-Card-Timestamp": timestamp,
            "X-Card-Nonce": nonce,
            "X-Card-Signature": signature,
            "Content-Type": "application/json",
        }

    def authenticate(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        operation_type: str,
    ) -> str:
        normalized = {str(key).lower(): value for key, value in headers.items()}
        terminal_id = normalized.get("x-card-terminal", "")
        timestamp = normalized.get("x-card-timestamp", "")
        nonce = normalized.get("x-card-nonce", "")
        signature = normalized.get("x-card-signature", "")
        credentials = self._credentials.get(terminal_id)
        if credentials is None:
            raise AuthenticationError("INVALID_CREDENTIALS")
        if operation_type not in credentials.operations:
            raise AuthenticationError("OPERATION_NOT_AUTHORIZED")
        if not re.fullmatch(r"[0-9a-f]{64}", signature) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{8,127}", nonce
        ):
            raise AuthenticationError("INVALID_AUTHENTICATION_FORMAT")
        parsed = _timestamp(timestamp)
        age = (self._clock() - parsed).total_seconds()
        if age < -30 or age > self.max_age_seconds:
            raise AuthenticationError("TIMESTAMP_EXPIRED")
        expected = hmac.new(
            credentials.secret,
            authentication_payload(method, path, terminal_id, timestamp, nonce, body),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise AuthenticationError("INVALID_SIGNATURE")
        if not self._nonces.reserve(terminal_id, nonce, timestamp):
            raise AuthenticationError("DUPLICATE_NONCE")
        return terminal_id
