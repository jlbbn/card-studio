from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from card_studio.production.contract import canonical_json

from .schema import (
    ENVELOPE_SCHEMA,
    PairingError,
    decode_base64,
    encode_base64,
    utc_now,
    validate_envelope,
    validate_event,
)


@dataclass(frozen=True)
class SessionKeys:
    encryption: bytes
    acknowledgement: bytes


def generate_pairing_key() -> str:
    return encode_base64(os.urandom(32))


def decode_pairing_key(value: str) -> bytes:
    return decode_base64(value, 32, "CARD_STUDIO_PAIRING_KEY")


def sign_value(key: bytes, domain: str, value: Mapping[str, Any]) -> str:
    payload = domain.encode("ascii") + b"\n" + canonical_json(value)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_signature(key: bytes, domain: str, value: Mapping[str, Any]) -> None:
    fields = {name: item for name, item in value.items() if name != "signature"}
    signature = value.get("signature")
    expected = sign_value(key, domain, fields)
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise PairingError("INVALID_SIGNATURE")


def derive_session_keys(
    pairing_key: bytes,
    *,
    session_id: str,
    reader_id: str,
    server_id: str,
    reader_nonce: str,
    server_nonce: str,
) -> SessionKeys:
    reader_raw = decode_base64(reader_nonce, 32, "reader_nonce")
    server_raw = decode_base64(server_nonce, 32, "server_nonce")
    info = canonical_json({
        "protocol": "CARD-STUDIO-LIVE-PAIRING-V1",
        "session_id": session_id,
        "reader_id": reader_id,
        "server_id": server_id,
    })
    derived = HKDF(
        algorithm=SHA256(), length=64, salt=reader_raw + server_raw, info=info,
    ).derive(pairing_key)
    return SessionKeys(derived[:32], derived[32:])


def seal_event(
    keys: SessionKeys,
    event: Mapping[str, Any],
    *,
    session_id: str,
    sender_id: str,
    sequence: int,
    sent_at: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    validate_event(event)
    header = {
        "schema": ENVELOPE_SCHEMA,
        "session_id": session_id,
        "message_id": message_id or str(uuid.uuid4()),
        "sequence": sequence,
        "sent_at": sent_at or utc_now(),
        "sender_id": sender_id,
    }
    nonce = os.urandom(12)
    ciphertext = AESGCM(keys.encryption).encrypt(
        nonce, canonical_json(event), canonical_json(header),
    )
    envelope = {
        **header,
        "nonce": encode_base64(nonce),
        "ciphertext": encode_base64(ciphertext),
    }
    validate_envelope(envelope)
    return envelope


def open_envelope(keys: SessionKeys, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
    validate_envelope(envelope)
    header = {
        name: envelope[name]
        for name in ("schema", "session_id", "message_id", "sequence", "sent_at", "sender_id")
    }
    nonce = decode_base64(envelope["nonce"], 12, "nonce")
    import base64
    try:
        raw_ciphertext = base64.b64decode(
            envelope["ciphertext"] + "=" * (-len(envelope["ciphertext"]) % 4),
            altchars=b"-_", validate=True,
        )
        plaintext = AESGCM(keys.encryption).decrypt(
            nonce, raw_ciphertext, canonical_json(header),
        )
    except InvalidTag as error:
        raise PairingError("ENVELOPE_AUTHENTICATION_FAILED") from error
    except (ValueError, UnicodeEncodeError) as error:
        raise PairingError("INVALID_CIPHERTEXT") from error
    try:
        event = json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise PairingError("INVALID_DECRYPTED_EVENT") from error
    return validate_event(event)


def signed_ack(keys: SessionKeys, value: Mapping[str, Any]) -> dict[str, Any]:
    return {**value, "signature": sign_value(keys.acknowledgement, "CARD-STUDIO-ACK-V1", value)}
