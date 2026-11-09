from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from card_studio.production.contract import canonical_json

PAIRING_SCHEMA = "card-studio.live-pairing.v1"
ENVELOPE_SCHEMA = "card-studio.encrypted-envelope.v1"
EVENT_SCHEMA = "card-studio.chip-metadata.v1"
ACK_SCHEMA = "card-studio.pairing-ack.v1"
STORE_SCHEMA = "card-studio.encrypted-event-store.v1"
HELLO_PATH = "/v1/pairing/hello"
EVENTS_PATH = "/v1/pairing/events"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,127}")
_LONG_NUMBER = re.compile(r"(?<!\d)\d{12,19}(?!\d)")
_METADATA_FIELDS = frozenset({
    "atr", "selected_aid", "reader_name", "application_label",
    "interface", "protocol", "standard",
})


class PairingError(ValueError):
    def __init__(self, code: str, message: str = "Operación de pairing rechazada."):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value: Any, field: str = "timestamp") -> datetime:
    if not isinstance(value, str):
        raise PairingError("INVALID_TIMESTAMP", f"{field} debe ser UTC ISO 8601.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PairingError("INVALID_TIMESTAMP", f"{field} debe ser UTC ISO 8601.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PairingError("INVALID_TIMESTAMP", f"{field} debe ser UTC ISO 8601.")
    return parsed.astimezone(timezone.utc)


def identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PairingError("INVALID_IDENTIFIER", f"{field} no tiene un formato válido.")
    return value


def expect_fields(value: Any, fields: frozenset[str] | set[str], code: str = "INVALID_SCHEMA") -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise PairingError(code)
    return value


def encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_base64(value: Any, expected_length: int, field: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise PairingError("INVALID_BASE64", f"{field} debe ser base64url.")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True,
        )
    except (ValueError, UnicodeEncodeError) as error:
        raise PairingError("INVALID_BASE64", f"{field} debe ser base64url.") from error
    if len(decoded) != expected_length or encode_base64(decoded) != value.rstrip("="):
        raise PairingError("INVALID_BASE64", f"{field} debe ser base64url canónico.")
    return decoded


def _safe_text(value: Any, field: str, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise PairingError("INVALID_METADATA", f"{field} debe ser texto.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or not normalized.isprintable() or _LONG_NUMBER.search(normalized):
        raise PairingError("UNSAFE_METADATA", f"{field} contiene datos no permitidos.")
    return normalized


def _hex(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise PairingError("INVALID_METADATA", f"{field} debe ser hexadecimal.")
    normalized = "".join(value.split()).upper()
    if not re.fullmatch(r"[0-9A-F]+", normalized) or len(normalized) % 2:
        raise PairingError("INVALID_METADATA", f"{field} debe ser hexadecimal.")
    return normalized


def validate_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - _METADATA_FIELDS:
        raise PairingError("INVALID_METADATA_FIELDS")
    metadata: dict[str, Any] = {}
    for name in ("atr", "selected_aid", "reader_name", "application_label", "interface", "protocol", "standard"):
        if name in value and value[name] is not None:
            metadata[name] = value[name]
    if "atr" in metadata:
        atr = _hex(metadata["atr"], "atr")
        if not 4 <= len(atr) <= 66 or atr[:2] not in {"3B", "3F"}:
            raise PairingError("INVALID_ATR")
        metadata["atr"] = atr
    if "selected_aid" in metadata:
        aid = _hex(metadata["selected_aid"], "selected_aid")
        if not 10 <= len(aid) <= 32:
            raise PairingError("INVALID_AID")
        metadata["selected_aid"] = aid
    if not {"atr", "selected_aid"} & set(metadata):
        raise PairingError("NO_CHIP_METADATA")
    for name in ("reader_name", "application_label", "standard"):
        if name in metadata:
            metadata[name] = _safe_text(metadata[name], name)
    if "interface" in metadata:
        if metadata["interface"] not in {"CONTACT", "CONTACTLESS", "VIRTUAL"}:
            raise PairingError("INVALID_INTERFACE")
    if "protocol" in metadata:
        if metadata["protocol"] not in {"T=0", "T=1", "VIRTUAL", "UNKNOWN"}:
            raise PairingError("INVALID_PROTOCOL")
    return metadata


def validate_event(value: Any) -> Mapping[str, Any]:
    fields = frozenset({
        "schema", "kind", "event_id", "reader_id", "captured_at",
        "evidence_class", "source_result", "metadata",
    })
    event = expect_fields(value, fields, "INVALID_EVENT_SCHEMA")
    if event["schema"] != EVENT_SCHEMA or event["kind"] != "chip.metadata":
        raise PairingError("INVALID_EVENT_SCHEMA")
    identifier(event["event_id"], "event_id")
    identifier(event["reader_id"], "reader_id")
    parse_timestamp(event["captured_at"], "captured_at")
    if event["evidence_class"] not in {"SYNTHETIC", "LIVE_READ_ONLY"}:
        raise PairingError("INVALID_EVIDENCE_CLASS")
    identifier(event["source_result"], "source_result")
    validate_metadata(event["metadata"])
    return event


def build_metadata_event(
    reader_id: str,
    metadata: Mapping[str, Any],
    *,
    evidence_class: str = "SYNTHETIC",
    source_result: str = "OPERATOR_SUPPLIED",
    captured_at: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    captured_at = captured_at or utc_now()
    normalized = {
        "reader_id": identifier(reader_id, "reader_id"),
        "captured_at": captured_at,
        "evidence_class": evidence_class,
        "source_result": source_result,
        "metadata": validate_metadata(metadata),
    }
    parse_timestamp(captured_at, "captured_at")
    if evidence_class not in {"SYNTHETIC", "LIVE_READ_ONLY"}:
        raise PairingError("INVALID_EVIDENCE_CLASS")
    identifier(source_result, "source_result")
    if event_id is None:
        digest = hashlib.sha256(canonical_json(normalized)).hexdigest()[:32]
        event_id = f"evt-{digest}"
    event = {"schema": EVENT_SCHEMA, "kind": "chip.metadata", "event_id": event_id, **normalized}
    validate_event(event)
    return event


def metadata_event_from_evidence(
    report: Mapping[str, Any],
    reader_id: str,
    *,
    interface: str = "CONTACT",
    protocol: str = "UNKNOWN",
    application_label: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(report, Mapping) or report.get("mode") not in {"SIMULATED", "PHYSICAL"}:
        raise PairingError("INVALID_EVIDENCE_MODE")
    metadata = {
        "atr": report.get("atr"),
        "selected_aid": report.get("selected_aid") or report.get("simulated_selected_aid"),
        "reader_name": report.get("reader_name"),
        "interface": interface,
        "protocol": protocol,
        "standard": "ISO/IEC 7816-4",
        "application_label": application_label,
    }
    return build_metadata_event(
        reader_id,
        metadata,
        evidence_class="SYNTHETIC" if report["mode"] == "SIMULATED" else "LIVE_READ_ONLY",
        source_result=str(report.get("result") or "UNSPECIFIED"),
        captured_at=report.get("timestamp"),
        event_id=event_id,
    )


def validate_envelope(value: Any) -> Mapping[str, Any]:
    fields = frozenset({
        "schema", "session_id", "message_id", "sequence", "sent_at",
        "sender_id", "nonce", "ciphertext",
    })
    envelope = expect_fields(value, fields, "INVALID_ENVELOPE_SCHEMA")
    if envelope["schema"] != ENVELOPE_SCHEMA:
        raise PairingError("INVALID_ENVELOPE_SCHEMA")
    identifier(envelope["session_id"], "session_id")
    identifier(envelope["message_id"], "message_id")
    identifier(envelope["sender_id"], "sender_id")
    if isinstance(envelope["sequence"], bool) or not isinstance(envelope["sequence"], int) or envelope["sequence"] < 1:
        raise PairingError("INVALID_SEQUENCE")
    parse_timestamp(envelope["sent_at"], "sent_at")
    decode_base64(envelope["nonce"], 12, "nonce")
    if not isinstance(envelope["ciphertext"], str):
        raise PairingError("INVALID_CIPHERTEXT")
    try:
        raw = base64.b64decode(
            envelope["ciphertext"] + "=" * (-len(envelope["ciphertext"]) % 4),
            altchars=b"-_", validate=True,
        )
    except (ValueError, UnicodeEncodeError) as error:
        raise PairingError("INVALID_CIPHERTEXT") from error
    if len(raw) < 17 or encode_base64(raw) != envelope["ciphertext"].rstrip("="):
        raise PairingError("INVALID_CIPHERTEXT")
    return envelope


def event_digest(event: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(event)).hexdigest()


def json_loads(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise PairingError("MALFORMED_JSON") from error
