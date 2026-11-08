from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from card_studio.interfaces.base import CardInterface
from card_studio.tlv import TlvNode, encode_tlv, parse_tlv

from .afl import ApplicationFileLocator
from .aip import ApplicationInterchangeProfile
from .apdu import ApduContext, CommandApdu, ResponseApdu, StatusAction, exchange_with_followups
from .c8_recovery import enforce_relay_time_limit
from .data import EmvDataStore, OwnershipDomain
from .cryptogram import Cid, CryptogramResult
from .dol import DolDocument, DolKind


class GpoError(ValueError):
    def __init__(self, code: str, message: str, command_sent: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.command_sent = command_sent


class GpoResponseFormat(str, Enum):
    TEMPLATE_77 = "TEMPLATE_77"
    TEMPLATE_80 = "TEMPLATE_80"


@dataclass(frozen=True)
class GpoResult:
    response_format: GpoResponseFormat
    aip: ApplicationInterchangeProfile
    afl: ApplicationFileLocator
    objects: tuple[dict[str, Any], ...]
    command_count: int = 1
    cryptogram_result: CryptogramResult | None = None
    ctq: bytes | None = None

    def to_evidence(self) -> dict[str, Any]:
        return {
            "response_format": self.response_format.value,
            "aip": self.aip.to_evidence(),
            "afl": self.afl.to_evidence(),
            "objects": list(self.objects),
            "command_count": self.command_count,
            "cryptogram_result": self.cryptogram_result.to_evidence() if self.cryptogram_result else None,
            "ctq": self.ctq.hex().upper() if self.ctq is not None else None,
            "mode": "LAB_ONLY",
        }


def build_gpo_command(pdol: DolDocument, store: EmvDataStore) -> CommandApdu:
    if pdol.kind != DolKind.PDOL:
        raise GpoError("INVALID_PDOL_KIND", "GET PROCESSING OPTIONS requiere un documento PDOL.")
    for entry in pdol.entries:
        source = store.require(entry.tag)
        if source.ownership not in {OwnershipDomain.TERMINAL, OwnershipDomain.PROFILE}:
            raise GpoError("PDOL_OWNERSHIP_CONFLICT", f"PDOL contiene un dato no terminal/profile: {entry.tag}.")
    pdol_values = pdol.build(store)
    command_template = encode_tlv(TlvNode("83", pdol_values))
    return CommandApdu(0x80, 0xA8, 0x00, 0x00, command_template, 0x00)


def _metadata(nodes) -> tuple[dict[str, Any], ...]:
    return tuple({"tag": node.tag, "constructed": node.constructed, "value_length": len(node.value)} for node in nodes)


def _required_primitive(children, tag: str) -> TlvNode:
    matches = [node for node in children if node.tag == tag]
    if len(matches) != 1:
        code = "DUPLICATE_GPO_OBJECT" if matches else "MISSING_GPO_OBJECT"
        raise GpoError(code, f"Se esperaba un único objeto {tag}.", command_sent=True)
    node = matches[0]
    if node.constructed:
        raise GpoError("INVALID_GPO_OBJECT", f"El objeto {tag} debe ser primitivo.", command_sent=True)
    return node


def parse_gpo_response(response: ResponseApdu) -> GpoResult:
    if not response.is_success:
        raise GpoError("GPO_STATUS_ERROR", f"GPO terminó con {response.sw1sw2}.", command_sent=True)
    try:
        roots = parse_tlv(response.data, context="GPO")
    except ValueError as exc:
        raise GpoError("MALFORMED_GPO_RESPONSE", "Respuesta GPO truncada o mal formada.", command_sent=True) from exc
    if len(roots) != 1:
        raise GpoError("AMBIGUOUS_GPO_RESPONSE", "Se esperaba un único template GPO.", command_sent=True)
    root = roots[0]
    cryptogram = None
    ctq = None
    if root.tag == "77":
        if not root.constructed:
            raise GpoError("INVALID_GPO_TEMPLATE", "Template 77 debe ser constructed.", command_sent=True)
        seen: set[str] = set()
        for child in root.children:
            if child.constructed:
                raise GpoError("NESTED_GPO_OBJECT", "Template 77 no admite objetos construidos anidados.", command_sent=True)
            if child.tag in seen:
                raise GpoError("DUPLICATE_GPO_OBJECT", f"Objeto GPO duplicado: {child.tag}.", command_sent=True)
            seen.add(child.tag)
        aip_value = _required_primitive(root.children, "82").value
        afl_value = _required_primitive(root.children, "94").value
        response_format = GpoResponseFormat.TEMPLATE_77
        objects = _metadata(root.children)
        cryptogram_tags = {"9F27", "9F36", "9F26", "9F10"}
        if seen & cryptogram_tags:
            cid = _required_primitive(root.children, "9F27").value
            atc = _required_primitive(root.children, "9F36").value
            ac = _required_primitive(root.children, "9F26").value
            iad_nodes = [node for node in root.children if node.tag == "9F10"]
            iad = iad_nodes[0].value if iad_nodes else b""
            if len(cid) != 1:
                raise GpoError("INVALID_GPO_LENGTH", "CID debe ocupar un byte.", command_sent=True)
            cryptogram = CryptogramResult(Cid(cid[0]), ac, atc, iad)
        ctq_nodes = [node for node in root.children if node.tag == "9F6C"]
        if ctq_nodes:
            ctq = ctq_nodes[0].value
            if len(ctq) != 2:
                raise GpoError("INVALID_GPO_LENGTH", "CTQ debe ocupar dos bytes.", command_sent=True)
    elif root.tag == "80":
        if root.constructed:
            raise GpoError("INVALID_GPO_TEMPLATE", "Template 80 debe ser primitivo.", command_sent=True)
        if len(root.value) <= 2:
            raise GpoError("MISSING_GPO_OBJECT", "Template 80 requiere AIP y AFL.", command_sent=True)
        aip_value = root.value[:2]
        afl_value = root.value[2:]
        response_format = GpoResponseFormat.TEMPLATE_80
        objects = ({"tag": "80", "constructed": False, "value_length": len(root.value)},)
    else:
        raise GpoError("UNEXPECTED_GPO_TEMPLATE", "Se esperaba template 77 u 80.", command_sent=True)
    try:
        aip = ApplicationInterchangeProfile.parse(aip_value)
        afl = ApplicationFileLocator.parse(afl_value)
    except ValueError:
        raise
    return GpoResult(response_format, aip, afl, objects, 1, cryptogram, ctq)


def get_processing_options(
    card_interface: CardInterface,
    pdol: DolDocument,
    store: EmvDataStore,
    max_followups: int = 4,
    relay_time_limit_ms: int = 0,
    clock: Any = time.monotonic,
) -> GpoResult:
    started_at = clock()
    exchange = exchange_with_followups(
        card_interface,
        build_gpo_command(pdol, store),
        context=ApduContext.GENERIC,
        max_followups=max_followups,
    )
    enforce_relay_time_limit(started_at, relay_time_limit_ms, clock)
    if exchange.action != StatusAction.SUCCESS:
        raise GpoError("GPO_STATUS_ERROR", f"GPO terminó en {exchange.action.value}.", command_sent=True)
    result = parse_gpo_response(exchange.response)
    return GpoResult(
        result.response_format,
        result.aip,
        result.afl,
        result.objects,
        exchange.command_count,
        result.cryptogram_result,
        result.ctq,
    )
