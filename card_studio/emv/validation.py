from __future__ import annotations


MAX_TAG_BYTES = 4


def normalize_tag(tag: str) -> str:
    if not isinstance(tag, str):
        raise ValueError("Tag debe ser hexadecimal.")
    normalized = tag.strip().upper()
    if not normalized or len(normalized) % 2 or len(normalized) > MAX_TAG_BYTES * 2:
        raise ValueError("Tag debe medir entre uno y cuatro bytes.")
    try:
        raw = bytes.fromhex(normalized)
    except ValueError as exc:
        raise ValueError("Tag debe ser hexadecimal.") from exc
    if raw[0] in {0x00, 0xFF}:
        raise ValueError("Tag contiene un byte inicial inválido.")
    if raw[0] & 0x1F == 0x1F:
        if len(raw) == 1 or raw[1] & 0x7F == 0 or raw[-1] & 0x80:
            raise ValueError("Tag multibyte incompleto o no canónico.")
        for byte in raw[1:-1]:
            if not byte & 0x80:
                raise ValueError("Tag multibyte con continuación inválida.")
    elif len(raw) != 1:
        raise ValueError("Tag de un byte con bytes sobrantes.")
    return normalized


def normalize_aid(aid: str) -> str:
    if not isinstance(aid, str):
        raise ValueError("AID debe ser hexadecimal.")
    normalized = aid.strip().upper()
    if not 10 <= len(normalized) <= 32 or len(normalized) % 2:
        raise ValueError("AID debe medir entre 5 y 16 bytes.")
    try:
        bytes.fromhex(normalized)
    except ValueError as exc:
        raise ValueError("AID debe ser hexadecimal.") from exc
    return normalized
