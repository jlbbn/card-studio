from __future__ import annotations

from collections.abc import Iterable

from .errors import (
    TlvLimitError,
    TlvMalformedError,
    TlvNonCanonicalError,
    TlvTruncatedError,
)
from .model import TlvLimits, TlvNode


def encode_length(length: int) -> bytes:
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        raise ValueError("La longitud TLV debe ser un entero no negativo.")
    if length < 0x80:
        return bytes((length,))
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(encoded),)) + encoded


def encode_tlv(nodes: TlvNode | Iterable[TlvNode]) -> bytes:
    selected = (nodes,) if isinstance(nodes, TlvNode) else tuple(nodes)
    output = bytearray()
    for node in selected:
        if not isinstance(node, TlvNode):
            raise TypeError("encode_tlv solo acepta TlvNode.")
        payload = encode_tlv(node.children) if node.children else node.value
        output.extend(bytes.fromhex(node.tag))
        output.extend(encode_length(len(payload)))
        output.extend(payload)
    return bytes(output)


def _coerce_bytes(data: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(data, str):
        try:
            return bytes.fromhex(data)
        except ValueError as exc:
            raise TlvMalformedError("TLV contiene hexadecimal inválido.") from exc
    try:
        return bytes(data)
    except (TypeError, ValueError) as exc:
        raise TlvMalformedError("TLV debe ser bytes o hexadecimal.") from exc


def parse_tlv(
    data: bytes | bytearray | memoryview | str,
    limits: TlvLimits | None = None,
    context: str | None = None,
) -> tuple[TlvNode, ...]:
    if context is not None and (
        not isinstance(context, str)
        or not context
        or len(context) > 64
        or any(ord(char) < 0x20 for char in context)
    ):
        raise ValueError("El contexto TLV debe ser texto visible de hasta 64 caracteres.")
    raw = _coerce_bytes(data)
    selected = limits or TlvLimits()
    if len(raw) > selected.max_bytes:
        raise TlvLimitError("Se superó max_bytes.", offset=selected.max_bytes)
    nodes_seen = 0

    def parse_range(start: int, end: int, depth: int, path: str) -> tuple[tuple[TlvNode, ...], int]:
        nonlocal nodes_seen
        if depth > selected.max_depth:
            raise TlvLimitError("Se superó max_depth.", offset=start)
        result: list[TlvNode] = []
        position = start
        while position < end:
            node_offset = position
            if position + 1 > end:
                raise TlvTruncatedError("Tag TLV truncado.", offset=position)
            first = raw[position]
            position += 1
            if first in {0x00, 0xFF}:
                raise TlvMalformedError("Tag TLV contiene un byte inicial inválido.", offset=node_offset)
            if first & 0x1F == 0x1F:
                if position >= end:
                    raise TlvTruncatedError("Tag multibyte truncado.", offset=node_offset)
                following = raw[position]
                if following & 0x7F == 0:
                    raise TlvNonCanonicalError("Tag multibyte no canónico.", offset=node_offset)
                tag_bytes = bytearray((first,))
                tag_length = 1
                while True:
                    if position >= end:
                        raise TlvTruncatedError("Tag multibyte truncado.", offset=node_offset)
                    tag_bytes.append(raw[position])
                    tag_length += 1
                    if tag_length > 4:
                        raise TlvLimitError("Tag TLV demasiado largo.", offset=node_offset)
                    last = raw[position] & 0x80 == 0
                    position += 1
                    if last:
                        break
                tag = tag_bytes.hex().upper()
            else:
                tag = bytes((first,)).hex().upper()
            if position >= end:
                raise TlvTruncatedError("Longitud TLV ausente.", offset=node_offset)
            first_length = raw[position]
            position += 1
            if first_length == 0x80:
                raise TlvMalformedError("Longitud indefinida prohibida.", offset=node_offset)
            if first_length < 0x80:
                length = first_length
            else:
                count = first_length & 0x7F
                if count == 0 or count > 4:
                    raise TlvLimitError("Longitud TLV no admitida.", offset=node_offset)
                if position + count > end:
                    raise TlvTruncatedError("Longitud larga truncada.", offset=node_offset)
                encoded = raw[position:position + count]
                if encoded[0] == 0:
                    raise TlvNonCanonicalError("Longitud larga con cero inicial.", offset=node_offset)
                length = int.from_bytes(encoded, "big")
                if length < 0x80:
                    raise TlvNonCanonicalError("Longitud larga no canónica.", offset=node_offset)
                position += count
            if length > end - position:
                raise TlvTruncatedError("Valor TLV truncado.", offset=node_offset)
            value_end = position + length
            nodes_seen += 1
            if nodes_seen > selected.max_nodes:
                raise TlvLimitError("Se superó max_nodes.", offset=node_offset)
            constructed = bool(first & 0x20)
            node_context = f"{path}/{tag}"
            if constructed:
                children, consumed = parse_range(position, value_end, depth + 1, node_context)
                if consumed != value_end:
                    raise TlvMalformedError("Template TLV mal formado.", offset=node_offset)
                node = TlvNode(tag, children=children, offset=node_offset, depth=depth, context=node_context)
            else:
                node = TlvNode(tag, raw[position:value_end], offset=node_offset, depth=depth, context=node_context)
            result.append(node)
            position = value_end
        return tuple(result), position

    parsed, final = parse_range(0, len(raw), 1, context or "BER")
    if final != len(raw):
        raise TlvMalformedError("Datos TLV sobrantes.")
    return parsed


def find_tag(nodes: Iterable[TlvNode], tag: str) -> TlvNode | None:
    selected = tag.upper()
    for node in nodes:
        if node.tag == selected:
            return node
    return None
