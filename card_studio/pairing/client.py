from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from card_studio.ledger import LedgerError, normalize_base_url
from card_studio.production.contract import canonical_json

from .crypto import SessionKeys, derive_session_keys, seal_event, sign_value, verify_signature
from .schema import (
    ACK_SCHEMA,
    EVENTS_PATH,
    HELLO_PATH,
    PAIRING_SCHEMA,
    PairingError,
    encode_base64,
    expect_fields,
    identifier,
    json_loads,
    parse_timestamp,
    utc_now,
    validate_event,
)


@dataclass
class _ClientSession:
    session_id: str
    server_id: str
    keys: SessionKeys
    sequence: int = 1
    pending: dict[str, Any] | None = None
    pending_event_id: str | None = None


class PairingClient:
    def __init__(
        self,
        base_url: str,
        reader_id: str,
        pairing_key: bytes,
        *,
        timeout: float = 5.0,
    ):
        try:
            self.base_url = normalize_base_url(base_url)
        except LedgerError as error:
            raise PairingError("INVALID_SERVER_URL", str(error)) from error
        self.reader_id = identifier(reader_id, "reader_id")
        if len(pairing_key) != 32:
            raise ValueError("La clave de pairing debe contener 32 bytes.")
        self.pairing_key = pairing_key
        self.timeout = timeout
        self.session: _ClientSession | None = None

    def _request(self, path: str, value: Mapping[str, Any]) -> Mapping[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=canonical_json(value),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(64 * 1024 + 1)
        except urllib.error.HTTPError as error:
            try:
                with error:
                    body = json_loads(error.read(64 * 1024))
                code = body.get("error", "SERVER_REJECTED") if isinstance(body, dict) else "SERVER_REJECTED"
            except (PairingError, ValueError):
                code = "MALFORMED_SERVER_RESPONSE"
            raise PairingError(code) from error
        except (socket.timeout, TimeoutError) as error:
            raise PairingError("SERVER_TIMEOUT_AFTER_SEND") from error
        except (urllib.error.URLError, OSError) as error:
            raise PairingError("SERVER_UNAVAILABLE_UNCERTAIN") from error
        if len(payload) > 64 * 1024:
            raise PairingError("MALFORMED_SERVER_RESPONSE")
        body = json_loads(payload)
        if not isinstance(body, Mapping):
            raise PairingError("MALFORMED_SERVER_RESPONSE")
        return body

    def pair(self) -> dict[str, Any]:
        request = {
            "schema": PAIRING_SCHEMA,
            "reader_id": self.reader_id,
            "reader_nonce": encode_base64(os.urandom(32)),
            "requested_at": utc_now(),
        }
        request["signature"] = sign_value(
            self.pairing_key, "CARD-STUDIO-HELLO-V1", request,
        )
        response = self._request(HELLO_PATH, request)
        fields = frozenset({
            "schema", "session_id", "reader_id", "server_id", "reader_nonce",
            "server_nonce", "expires_at", "signature",
        })
        response = expect_fields(response, fields, "INVALID_HELLO_RESPONSE")
        if (
            response["schema"] != PAIRING_SCHEMA
            or response["reader_id"] != self.reader_id
            or response["reader_nonce"] != request["reader_nonce"]
        ):
            raise PairingError("HELLO_RESPONSE_MISMATCH")
        identifier(response["session_id"], "session_id")
        identifier(response["server_id"], "server_id")
        if parse_timestamp(response["expires_at"], "expires_at") <= datetime.now(timezone.utc):
            raise PairingError("PAIRING_EXPIRED")
        verify_signature(
            self.pairing_key, "CARD-STUDIO-HELLO-RESPONSE-V1", response,
        )
        keys = derive_session_keys(
            self.pairing_key,
            session_id=response["session_id"],
            reader_id=self.reader_id,
            server_id=response["server_id"],
            reader_nonce=response["reader_nonce"],
            server_nonce=response["server_nonce"],
        )
        self.session = _ClientSession(response["session_id"], response["server_id"], keys)
        return {
            "session_id": response["session_id"],
            "reader_id": self.reader_id,
            "server_id": response["server_id"],
            "expires_at": response["expires_at"],
        }

    def _send_pending(self) -> dict[str, Any]:
        if self.session is None or self.session.pending is None:
            raise PairingError("PAIRING_REQUIRED")
        envelope = self.session.pending
        response = self._request(EVENTS_PATH, envelope)
        fields = frozenset({
            "schema", "session_id", "message_id", "event_id", "sequence",
            "status", "server_time", "signature",
        })
        response = expect_fields(response, fields, "INVALID_ACK_SCHEMA")
        if (
            response["schema"] != ACK_SCHEMA
            or response["session_id"] != self.session.session_id
            or response["message_id"] != envelope["message_id"]
            or response["event_id"] != self.session.pending_event_id
            or response["sequence"] != envelope["sequence"]
            or response["status"] not in {"STORED", "DUPLICATE"}
        ):
            raise PairingError("ACK_MISMATCH")
        parse_timestamp(response["server_time"], "server_time")
        verify_signature(self.session.keys.acknowledgement, "CARD-STUDIO-ACK-V1", response)
        self.session.sequence += 1
        self.session.pending = None
        self.session.pending_event_id = None
        return dict(response)

    def publish(self, event: Mapping[str, Any]) -> dict[str, Any]:
        validate_event(event)
        if self.session is None:
            raise PairingError("PAIRING_REQUIRED")
        if event["reader_id"] != self.reader_id:
            raise PairingError("EVENT_READER_MISMATCH")
        if self.session.pending is not None:
            raise PairingError("UNCONFIRMED_EVENT_REQUIRES_RETRY")
        self.session.pending_event_id = event["event_id"]
        self.session.pending = seal_event(
            self.session.keys,
            event,
            session_id=self.session.session_id,
            sender_id=self.reader_id,
            sequence=self.session.sequence,
        )
        return self._send_pending()

    def retry(self) -> dict[str, Any]:
        return self._send_pending()
