from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from card_studio.interfaces.base import CardInterface
from card_studio.tlv import TlvNode, parse_tlv

from .apdu import ApduContext, CommandApdu, ResponseApdu, StatusAction, exchange_with_followups
from .cryptogram import Cid, CryptogramError, CryptogramResult
from .data import EmvDataStore, OwnershipDomain
from .dol import DolDocument, DolKind
from .taa import TerminalAction


class GacError(ValueError):
    def __init__(self, code: str, message: str, command_sent: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.command_sent = command_sent


@dataclass(frozen=True)
class GenerateAcResponse:
    response_format: int
    result: CryptogramResult
    icc_dynamic_number: bytes | None = None
    signed_dynamic_application_data: bytes | None = None
    command_count: int = 1

    def to_evidence(self) -> dict[str, Any]:
        return {
            "response_format": self.response_format,
            "result": self.result.to_evidence(),
            "icc_dynamic_number_length": len(self.icc_dynamic_number or b""),
            "signed_dynamic_application_data_length": len(self.signed_dynamic_application_data or b""),
            "command_count": self.command_count,
        }


def _p1(action: TerminalAction, cda_requested: bool) -> int:
    selected = TerminalAction(action)
    base = {
        TerminalAction.AAC: 0x00,
        TerminalAction.TC: 0x40,
        TerminalAction.ARQC: 0x80,
    }[selected]
    if cda_requested:
        if selected == TerminalAction.AAC:
            raise GacError("INVALID_CDA_REQUEST", "CDA no se solicita con AAC en LAB_ONLY.")
        base |= 0x10
    return base


def _validate_ownership(document: DolDocument, store: EmvDataStore, final: bool) -> None:
    allowed = {OwnershipDomain.CARD, OwnershipDomain.TERMINAL, OwnershipDomain.PROFILE, OwnershipDomain.KERNEL}
    if final:
        allowed.add(OwnershipDomain.HOST)
    for entry in document.entries:
        record = store.require(entry.tag)
        if record.ownership not in allowed:
            raise GacError("CDOL_OWNERSHIP_CONFLICT", f"Dato CDOL fuera de dominio: {entry.tag}.")


def build_generate_ac_command(
    action: TerminalAction,
    cdol: DolDocument,
    store: EmvDataStore,
    cda_requested: bool = False,
) -> CommandApdu:
    if cdol.kind not in {DolKind.CDOL1, DolKind.CDOL2}:
        raise GacError("INVALID_CDOL_KIND", "GENERATE AC requiere CDOL1 o CDOL2.")
    final = cdol.kind == DolKind.CDOL2
    _validate_ownership(cdol, store, final)
    values = cdol.build(store)
    return CommandApdu(0x80, 0xAE, _p1(action, cda_requested), 0x00, values, 0x00)


def build_final_generate_ac_command(
    action: TerminalAction,
    cdol2: DolDocument,
    store: EmvDataStore,
    cda_requested: bool = False,
) -> CommandApdu:
    if cdol2.kind != DolKind.CDOL2:
        raise GacError("INVALID_CDOL_KIND", "Final GENERATE AC requiere CDOL2.")
    return build_generate_ac_command(action, cdol2, store, cda_requested)


def _unique(children, tag: str, required: bool = True) -> TlvNode | None:
    matches = [node for node in children if node.tag == tag]
    if len(matches) > 1:
        raise GacError("DUPLICATE_GAC_OBJECT", f"Objeto GENERATE AC duplicado: {tag}.", command_sent=True)
    if not matches:
        if required:
            raise GacError("MISSING_GAC_OBJECT", f"Objeto GENERATE AC obligatorio ausente: {tag}.", command_sent=True)
        return None
    node = matches[0]
    if node.constructed:
        raise GacError("INVALID_GAC_OBJECT", f"Objeto GENERATE AC debe ser primitivo: {tag}.", command_sent=True)
    return node


def _fixed(children, tag: str, length: int) -> bytes:
    value = _unique(children, tag).value
    if len(value) != length:
        raise GacError("INVALID_GAC_LENGTH", f"Objeto {tag} debe ocupar {length} bytes.", command_sent=True)
    return value


def parse_generate_ac_response(response: ResponseApdu) -> GenerateAcResponse:
    if not response.is_success:
        raise GacError("GAC_STATUS_ERROR", f"GENERATE AC terminó con {response.sw1sw2}.", command_sent=True)
    try:
        roots = parse_tlv(response.data)
    except ValueError as exc:
        raise GacError("MALFORMED_GAC_RESPONSE", "Respuesta GENERATE AC truncada o mal formada.", command_sent=True) from exc
    if len(roots) != 1:
        raise GacError("AMBIGUOUS_GAC_RESPONSE", "Se esperaba una única respuesta GENERATE AC.", command_sent=True)
    root = roots[0]
    dynamic_number = None
    signed_data = None
    if root.tag == "80" and not root.constructed:
        if not 11 <= len(root.value) <= 43:
            raise GacError("INVALID_GAC_LENGTH", "Formato 1 requiere CID/ATC/AC e IAD de hasta 32 bytes.", command_sent=True)
        cid_raw = root.value[0]
        atc = root.value[1:3]
        cryptogram = root.value[3:11]
        iad = root.value[11:]
        response_format = 80
    elif root.tag == "77" and root.constructed:
        allowed = {"9F27", "9F26", "9F36", "9F10", "9F4B", "9F4C"}
        seen: set[str] = set()
        for child in root.children:
            if child.constructed:
                raise GacError("NESTED_GAC_OBJECT", "Template 77 no admite objetos anidados.", command_sent=True)
            if child.tag in seen:
                raise GacError("DUPLICATE_GAC_OBJECT", f"Objeto GENERATE AC duplicado: {child.tag}.", command_sent=True)
            seen.add(child.tag)
            if child.tag not in allowed:
                raise GacError("UNEXPECTED_GAC_OBJECT", f"Objeto GENERATE AC desconocido: {child.tag}.", command_sent=True)
        cid_raw = _fixed(root.children, "9F27", 1)[0]
        atc = _fixed(root.children, "9F36", 2)
        cryptogram = _fixed(root.children, "9F26", 8)
        iad_node = _unique(root.children, "9F10", required=False)
        iad = iad_node.value if iad_node is not None else b""
        if len(iad) > 32:
            raise GacError("INVALID_IAD_LENGTH", "IAD supera 32 bytes.", command_sent=True)
        dynamic_node = _unique(root.children, "9F4C", required=False)
        if dynamic_node is not None:
            if not dynamic_node.value:
                raise GacError("INVALID_DYNAMIC_NUMBER", "ICC Dynamic Number vacío.", command_sent=True)
            dynamic_number = dynamic_node.value
        signed_node = _unique(root.children, "9F4B", required=False)
        if signed_node is not None:
            if not signed_node.value:
                raise GacError("INVALID_DYNAMIC_AUTH_DATA", "Signed Dynamic Application Data vacío.", command_sent=True)
            signed_data = signed_node.value
        response_format = 77
    else:
        raise GacError("UNEXPECTED_GAC_TEMPLATE", "Se esperaba template 77 u 80.", command_sent=True)
    try:
        result = CryptogramResult(Cid(cid_raw), cryptogram, atc, iad)
    except CryptogramError:
        raise
    return GenerateAcResponse(response_format, result, dynamic_number, signed_data)


def generate_ac(
    card_interface: CardInterface,
    command: CommandApdu,
    max_followups: int = 4,
) -> GenerateAcResponse:
    exchange = exchange_with_followups(card_interface, command, context=ApduContext.GENERIC, max_followups=max_followups)
    if exchange.action != StatusAction.SUCCESS:
        raise GacError("GAC_STATUS_ERROR", f"GENERATE AC terminó en {exchange.action.value}.", command_sent=True)
    parsed = parse_generate_ac_response(exchange.response)
    return GenerateAcResponse(
        parsed.response_format,
        parsed.result,
        parsed.icc_dynamic_number,
        parsed.signed_dynamic_application_data,
        exchange.command_count,
    )
