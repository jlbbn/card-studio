from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Sequence

from .base import HostError


SUPPORTED_ARC = frozenset({"00", "05", "Y3", "Z3"})
RESPONSE_TAGS = frozenset({"8A", "91", "71", "72"})
SCRIPT_TAGS = frozenset({"86", "9F18"})


@dataclass(frozen=True)
class _Node:
    tag: str
    constructed: bool
    value: bytes
    children: tuple["_Node", ...]


def _fail(code: str, message: str) -> None:
    raise HostError(code, message)


def _node(data: bytes, offset: int) -> tuple[_Node, int]:
    if offset >= len(data):
        _fail("MALFORMED_RESPONSE_TEMPLATE", "TLV truncado.")
    first = data[offset]
    if first in {0x00, 0xFF}:
        _fail("MALFORMED_RESPONSE_TEMPLATE", "Tag BER inválido.")
    constructed = bool(first & 0x20)
    tag_bytes = bytearray([first])
    cursor = offset + 1
    if first & 0x1F == 0x1F:
        for _ in range(3):
            if cursor >= len(data):
                _fail("MALFORMED_RESPONSE_TEMPLATE", "Tag multibyte truncado.")
            value = data[cursor]
            tag_bytes.append(value)
            cursor += 1
            if not value & 0x80:
                break
        else:
            _fail("MALFORMED_RESPONSE_TEMPLATE", "Tag demasiado largo.")
        if tag_bytes[-1] & 0x80:
            _fail("MALFORMED_RESPONSE_TEMPLATE", "Tag multibyte incompleto.")
    if cursor >= len(data):
        _fail("MALFORMED_RESPONSE_TEMPLATE", "Longitud TLV ausente.")
    initial = data[cursor]
    cursor += 1
    if initial < 0x80:
        length = initial
    elif initial == 0x80:
        _fail("MALFORMED_RESPONSE_TEMPLATE", "Longitud indefinida no permitida.")
    elif initial in {0x81, 0x82}:
        count = initial & 0x0F
        if cursor + count > len(data):
            _fail("MALFORMED_RESPONSE_TEMPLATE", "Longitud extensa truncada.")
        length = int.from_bytes(data[cursor:cursor + count], "big")
        cursor += count
        if (initial == 0x81 and length < 0x80) or (initial == 0x82 and length < 0x100):
            _fail("MALFORMED_RESPONSE_TEMPLATE", "Longitud BER no canónica.")
    else:
        _fail("MALFORMED_RESPONSE_TEMPLATE", "Forma de longitud no soportada.")
    end = cursor + length
    if end > len(data):
        _fail("MALFORMED_RESPONSE_TEMPLATE", "Valor TLV truncado.")
    value = data[cursor:end]
    children = _children(value) if constructed else ()
    return _Node(bytes(tag_bytes).hex().upper(), constructed, value, children), end


def _children(data: bytes) -> tuple[_Node, ...]:
    values = []
    offset = 0
    while offset < len(data):
        child, offset = _node(data, offset)
        values.append(child)
    return tuple(values)


def _arc(value: bytes) -> str:
    if len(value) != 2:
        _fail("INVALID_AUTHORIZATION_RESPONSE_CODE", "ARC debe medir dos bytes.")
    try:
        arc = value.decode("ascii")
    except UnicodeDecodeError as error:
        raise HostError("INVALID_AUTHORIZATION_RESPONSE_CODE", "ARC no es ASCII.") from error
    if arc not in SUPPORTED_ARC:
        _fail("INVALID_AUTHORIZATION_RESPONSE_CODE", "ARC fuera de la política LAB.")
    return arc


def validate_response_template(template: str) -> dict[str, object]:
    if not isinstance(template, str) or not template or len(template) % 2:
        _fail("MALFORMED_RESPONSE_TEMPLATE", "response_template debe ser hexadecimal BER-TLV completo.")
    try:
        data = bytes.fromhex(template)
    except ValueError as error:
        raise HostError("MALFORMED_RESPONSE_TEMPLATE", "response_template no es hexadecimal.") from error
    try:
        nodes = _children(data)
    except HostError:
        raise
    except Exception as error:
        raise HostError("MALFORMED_RESPONSE_TEMPLATE", "No se pudo parsear response_template.") from error
    if not nodes:
        _fail("MALFORMED_RESPONSE_TEMPLATE", "response_template vacío.")
    seen: set[str] = set()
    arc: str | None = None
    script_count = 0
    script_identifier_count = 0

    def reject_nested(node: _Node, parent: str | None) -> None:
        if parent is not None and node.tag in RESPONSE_TAGS:
            _fail("NESTED_RESPONSE_OBJECT", f"Objeto {node.tag} anidado no permitido.")
        for child in node.children:
            reject_nested(child, node.tag)

    for node in nodes:
        reject_nested(node, None)

    def visit(node: _Node, parent: str | None) -> None:
        nonlocal arc, script_count, script_identifier_count
        if parent is not None and node.tag in RESPONSE_TAGS:
            _fail("NESTED_RESPONSE_OBJECT", f"Objeto {node.tag} anidado no permitido.")
        allowed = RESPONSE_TAGS if parent is None else SCRIPT_TAGS
        if node.tag not in allowed:
            _fail("UNSUPPORTED_RESPONSE_OBJECT", f"Objeto no soportado: {node.tag}.")
        if parent is None:
            if node.tag in seen:
                _fail("DUPLICATE_RESPONSE_OBJECT", f"Objeto duplicado: {node.tag}.")
            seen.add(node.tag)
            if node.tag == "8A":
                if node.constructed:
                    _fail("INVALID_AUTHORIZATION_RESPONSE_CODE", "8A debe ser primitivo.")
                arc = _arc(node.value)
            elif node.tag == "91":
                if node.constructed or not 8 <= len(node.value) <= 16:
                    _fail("INVALID_ISSUER_AUTHENTICATION_DATA", "91 debe ser primitivo de 8 a 16 bytes.")
            elif not node.constructed:
                _fail("INVALID_ISSUER_SCRIPT", f"{node.tag} debe ser construido.")
        else:
            if node.constructed:
                _fail("INVALID_ISSUER_SCRIPT", f"Objeto {node.tag} debe ser primitivo.")
            if node.tag == "86":
                if len(node.value) < 4:
                    _fail("INVALID_ISSUER_SCRIPT", "Comando 86 demasiado corto.")
                script_count += 1
            else:
                if len(node.value) != 4:
                    _fail("INVALID_ISSUER_SCRIPT", "9F18 debe medir cuatro bytes.")
                script_identifier_count += 1
        for child in node.children:
            visit(child, node.tag)

    for node in nodes:
        visit(node, None)
    if arc is None:
        _fail("MISSING_AUTHORIZATION_RESPONSE_CODE", "response_template requiere 8A.")
    for tag in ("71", "72"):
        matching = [node for node in nodes if node.tag == tag]
        identifiers = sum(child.tag == "9F18" for child in matching[0].children) if matching else 0
        if identifiers > 1:
            _fail("DUPLICATE_RESPONSE_OBJECT", f"9F18 duplicado en {tag}.")
        if matching and not any(child.tag == "86" for child in matching[0].children):
            _fail("INVALID_ISSUER_SCRIPT", f"{tag} requiere al menos un comando 86.")
    return {
        "authorization_response_code": arc,
        "response_template_sha256": hashlib.sha256(data).hexdigest(),
        "response_template_length": len(data),
        "response_tag_count": len(nodes),
        "script_count": script_count,
        "script_identifier_count": script_identifier_count,
        "response_tags": [node.tag for node in nodes],
    }


def _length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    if length <= 0xFF:
        return bytes([0x81, length])
    if length <= 0xFFFF:
        return bytes([0x82]) + length.to_bytes(2, "big")
    raise HostError("INVALID_ISSUER_SCRIPT", "Objeto TLV demasiado grande.")


def _tlv(tag: str, value: bytes) -> bytes:
    return bytes.fromhex(tag) + _length(len(value)) + value


def build_response_template(
    arc: str,
    issuer_authentication_data: str | None = None,
    issuer_script_commands: Mapping[str, Sequence[str]] | None = None,
    issuer_script_identifiers: Mapping[str, str] | None = None,
) -> str:
    try:
        arc_bytes = arc.encode("ascii")
    except (AttributeError, UnicodeEncodeError) as error:
        raise HostError("INVALID_AUTHORIZATION_RESPONSE_CODE", "ARC inválido.") from error
    _arc(arc_bytes)
    result = bytearray(_tlv("8A", arc_bytes))
    if issuer_authentication_data is not None:
        try:
            issuer = bytes.fromhex(issuer_authentication_data)
        except ValueError as error:
            raise HostError("INVALID_ISSUER_AUTHENTICATION_DATA", "91 no es hexadecimal.") from error
        result.extend(_tlv("91", issuer))
    commands = issuer_script_commands or {}
    identifiers = issuer_script_identifiers or {}
    if (set(commands) | set(identifiers)) - {"71", "72"}:
        _fail("UNSUPPORTED_RESPONSE_OBJECT", "Solo se admiten scripts 71/72.")
    for tag in ("71", "72"):
        children = bytearray()
        if tag in identifiers:
            try:
                children.extend(_tlv("9F18", bytes.fromhex(identifiers[tag])))
            except ValueError as error:
                raise HostError("INVALID_ISSUER_SCRIPT", "9F18 no es hexadecimal.") from error
        for command in commands.get(tag, ()):
            try:
                children.extend(_tlv("86", bytes.fromhex(command)))
            except ValueError as error:
                raise HostError("INVALID_ISSUER_SCRIPT", "Comando 86 no es hexadecimal.") from error
        if children:
            result.extend(_tlv(tag, bytes(children)))
    template = bytes(result).hex().upper()
    validate_response_template(template)
    return template
