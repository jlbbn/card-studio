from __future__ import annotations

import hashlib
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from card_studio.production.contract import canonical_json

from .crypto import SessionKeys, derive_session_keys, open_envelope, sign_value, signed_ack, verify_signature
from .schema import (
    ACK_SCHEMA,
    EVENTS_PATH,
    HELLO_PATH,
    PAIRING_SCHEMA,
    PairingError,
    decode_base64,
    encode_base64,
    expect_fields,
    format_timestamp,
    identifier,
    json_loads,
    parse_timestamp,
    validate_envelope,
    validate_event,
)
from .store import AtomicEnvelopeStore


@dataclass
class _Session:
    reader_id: str
    server_id: str
    expires_at: datetime
    keys: SessionKeys
    next_sequence: int = 1
    received: dict[str, tuple[str, str, int]] = field(default_factory=dict)


class PairingService:
    def __init__(
        self,
        pairing_key: bytes,
        reader_id: str,
        server_id: str,
        store: AtomicEnvelopeStore,
        *,
        session_ttl_seconds: int = 300,
        max_hello_age_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
    ):
        if len(pairing_key) != 32:
            raise ValueError("La clave de pairing debe contener 32 bytes.")
        if not 30 <= session_ttl_seconds <= 3600:
            raise ValueError("La duración de sesión debe estar entre 30 y 3600 segundos.")
        self.pairing_key = pairing_key
        self.reader_id = identifier(reader_id, "reader_id")
        self.server_id = identifier(server_id, "server_id")
        self.store = store
        self.session_ttl_seconds = session_ttl_seconds
        self.max_hello_age_seconds = max_hello_age_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sessions: dict[str, _Session] = {}
        self.hello_nonces: set[str] = set()
        self.lock = threading.RLock()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("El reloj debe incluir zona horaria.")
        return value.astimezone(timezone.utc)

    def _ack(
        self,
        session_id: str,
        session: _Session,
        envelope: Mapping[str, Any],
        event_id: str,
        status: str,
    ) -> dict[str, Any]:
        return signed_ack(session.keys, {
            "schema": ACK_SCHEMA,
            "session_id": session_id,
            "message_id": envelope["message_id"],
            "event_id": event_id,
            "sequence": envelope["sequence"],
            "status": status,
            "server_time": format_timestamp(self._now()),
        })

    def hello(self, value: Mapping[str, Any]) -> dict[str, Any]:
        fields = frozenset({"schema", "reader_id", "reader_nonce", "requested_at", "signature"})
        request = expect_fields(value, fields, "INVALID_HELLO_SCHEMA")
        if request["schema"] != PAIRING_SCHEMA or request["reader_id"] != self.reader_id:
            raise PairingError("PAIRING_REJECTED")
        verify_signature(self.pairing_key, "CARD-STUDIO-HELLO-V1", request)
        decode_base64(request["reader_nonce"], 32, "reader_nonce")
        requested_at = parse_timestamp(request["requested_at"], "requested_at")
        age = (self._now() - requested_at).total_seconds()
        if age < -30 or age > self.max_hello_age_seconds:
            raise PairingError("HELLO_EXPIRED")
        with self.lock:
            if request["reader_nonce"] in self.hello_nonces:
                raise PairingError("DUPLICATE_HELLO_NONCE")
            self.hello_nonces.add(request["reader_nonce"])
            now = self._now()
            self.sessions = {
                name: session for name, session in self.sessions.items()
                if session.expires_at > now
            }
            session_id = uuid.uuid4().hex
            server_nonce = encode_base64(os.urandom(32))
            expires_at = now + timedelta(seconds=self.session_ttl_seconds)
            keys = derive_session_keys(
                self.pairing_key,
                session_id=session_id,
                reader_id=self.reader_id,
                server_id=self.server_id,
                reader_nonce=request["reader_nonce"],
                server_nonce=server_nonce,
            )
            self.sessions[session_id] = _Session(
                self.reader_id, self.server_id, expires_at, keys,
            )
            response = {
                "schema": PAIRING_SCHEMA,
                "session_id": session_id,
                "reader_id": self.reader_id,
                "server_id": self.server_id,
                "reader_nonce": request["reader_nonce"],
                "server_nonce": server_nonce,
                "expires_at": format_timestamp(expires_at),
            }
            return {
                **response,
                "signature": sign_value(self.pairing_key, "CARD-STUDIO-HELLO-RESPONSE-V1", response),
            }

    def event(self, value: Mapping[str, Any]) -> dict[str, Any]:
        envelope = validate_envelope(value)
        session_id = envelope["session_id"]
        with self.lock:
            try:
                session = self.sessions[session_id]
            except KeyError as error:
                raise PairingError("PAIRING_REQUIRED") from error
            if session.expires_at <= self._now():
                self.sessions.pop(session_id, None)
                raise PairingError("PAIRING_EXPIRED")
            if envelope["sender_id"] != self.reader_id:
                raise PairingError("SENDER_MISMATCH")
            digest = hashlib.sha256(canonical_json(envelope)).hexdigest()
            previous = session.received.get(envelope["message_id"])
            if previous is not None:
                if previous[0] != digest:
                    raise PairingError("MESSAGE_ID_CONFLICT")
                return self._ack(session_id, session, envelope, previous[1], "DUPLICATE")
            if envelope["sequence"] != session.next_sequence:
                raise PairingError("SEQUENCE_MISMATCH")
            event = open_envelope(session.keys, envelope)
            validate_event(event)
            if event["reader_id"] != self.reader_id:
                raise PairingError("EVENT_READER_MISMATCH")
            created = self.store.record(envelope, event)
            session.received[envelope["message_id"]] = (
                digest, event["event_id"], envelope["sequence"],
            )
            session.next_sequence += 1
            return self._ack(
                session_id, session, envelope, event["event_id"],
                "STORED" if created else "DUPLICATE",
            )

    def post(self, path: str, value: Mapping[str, Any]) -> dict[str, Any]:
        if path == HELLO_PATH:
            return self.hello(value)
        if path == EVENTS_PATH:
            return self.event(value)
        raise PairingError("NOT_FOUND")


def make_server(address: tuple[str, int], service: PairingService) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CardStudioPairing"
        sys_version = ""

        def _send(self, status: int, value: Mapping[str, Any]) -> None:
            payload = canonical_json(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/livez":
                self._send(200, {"ok": True})
            elif self.path == "/readyz":
                ready = service.store.ping()
                self._send(200 if ready else 503, {"ok": ready})
            else:
                self._send(404, {"ok": False, "error": "NOT_FOUND"})

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64 * 1024:
                    raise PairingError("INVALID_PAYLOAD_SIZE")
                value = json_loads(self.rfile.read(length))
                self._send(200, service.post(self.path, value))
            except PairingError as error:
                if error.code in {"INVALID_SIGNATURE", "PAIRING_REJECTED", "PAIRING_REQUIRED", "PAIRING_EXPIRED"}:
                    status = 401
                elif "CONFLICT" in error.code or error.code in {"SEQUENCE_MISMATCH", "DUPLICATE_HELLO_NONCE"}:
                    status = 409
                elif error.code == "NOT_FOUND":
                    status = 404
                else:
                    status = 400
                self._send(status, {"ok": False, "error": error.code})
            except Exception:
                self._send(500, {"ok": False, "error": "INTERNAL_ERROR"})

        def log_message(self, *_args: Any) -> None:
            return

    return ThreadingHTTPServer(address, Handler)
