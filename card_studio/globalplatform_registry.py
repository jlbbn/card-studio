from __future__ import annotations

from typing import Any

from .globalplatform_transport import GlobalPlatformTransport
from .model import CardError
from .tlv.codec import parse_tlv


SUPPORTED_P1 = {
    0x80: "ISD",
    0x40: "APPLET",
    0x20: "PACKAGE",
    0x10: "PACKAGE",
}


def build_get_status_apdu(
    p1: int,
    *,
    next_occurrence: bool = False,
) -> str:
    if p1 not in SUPPORTED_P1:
        raise ValueError(f"GET STATUS P1 no soportado: {p1:02X}")

    # b2=1 -> BER-TLV response structure.
    # b1=0 -> first/all occurrences.
    # b1=1 -> next occurrence(s).
    p2 = 0x03 if next_occurrence else 0x02

    # Search criterion mandatory: 4F 00 = all AIDs.
    # Le = 00.
    apdu = bytes([
        0x80,
        0xF2,
        p1,
        p2,
        0x02,
        0x4F,
        0x00,
        0x00,
    ])

    return apdu.hex().upper()


def _children(node: Any, tag: str) -> list[Any]:
    selected = tag.upper()
    return [
        child
        for child in node.children
        if child.tag == selected
    ]


def _first_child(node: Any, tag: str) -> Any | None:
    matches = _children(node, tag)
    return matches[0] if matches else None


def parse_registry_tlv(
    data: bytes | bytearray | memoryview | str,
    *,
    entry_type: str,
) -> list[dict[str, Any]]:
    if not data:
        return []

    try:
        nodes = parse_tlv(
            data,
            context="GP_GET_STATUS",
        )
    except Exception as exc:
        raise CardError(
            "GP_REGISTRY_TLV_INVALID",
            f"La respuesta GET STATUS contiene TLV inválido: {exc}",
        ) from exc

    entries: list[dict[str, Any]] = []

    for node in nodes:
        if node.tag != "E3":
            raise CardError(
                "GP_REGISTRY_TEMPLATE_INVALID",
                f"Se esperaba template E3 y se recibió {node.tag}.",
            )

        aid_node = _first_child(node, "4F")

        if aid_node is None:
            raise CardError(
                "GP_REGISTRY_AID_MISSING",
                "Una entrada E3 no contiene AID 4F.",
            )

        aid = bytes(aid_node.value)

        if not 5 <= len(aid) <= 16:
            raise CardError(
                "GP_REGISTRY_AID_INVALID",
                "El AID GlobalPlatform tiene longitud inválida.",
            )

        lifecycle_node = _first_child(node, "9F70")
        privileges_node = _first_child(node, "C5")
        module_nodes = _children(node, "84")
        associated_sd_node = _first_child(node, "CC")

        entry: dict[str, Any] = {
            "type": entry_type,
            "aid": aid.hex().upper(),
        }

        if lifecycle_node is not None:
            entry["life_cycle"] = bytes(
                lifecycle_node.value
            ).hex().upper()

        if privileges_node is not None:
            entry["privileges"] = bytes(
                privileges_node.value
            ).hex().upper()

        if associated_sd_node is not None:
            entry["associated_sd"] = bytes(
                associated_sd_node.value
            ).hex().upper()

        if module_nodes:
            entry["modules"] = [
                bytes(module.value).hex().upper()
                for module in module_nodes
            ]

        entries.append(entry)

    return entries


class GlobalPlatformRegistryReader:
    """
    Lectura read-only del GlobalPlatform Registry.

    Nunca abre ni cierra PC/SC.
    Usa exclusivamente el GlobalPlatformTransport recibido.
    """

    def __init__(
        self,
        transport: GlobalPlatformTransport,
        *,
        max_pages: int = 64,
    ) -> None:
        self._transport = transport
        self._max_pages = max_pages

    def read_entries(
        self,
        p1: int,
        *,
        entry_type: str | None = None,
    ) -> list[dict[str, Any]]:
        selected_type = (
            entry_type
            or SUPPORTED_P1.get(p1)
        )

        if selected_type is None:
            raise ValueError(
                f"GET STATUS P1 no soportado: {p1:02X}"
            )

        entries: list[dict[str, Any]] = []
        next_occurrence = False

        for _ in range(self._max_pages):
            command = build_get_status_apdu(
                p1,
                next_occurrence=next_occurrence,
            )

            response = self._transport.transmit_hex(
                command
            )

            sw = str(response.sw1sw2).upper()

            if sw == "6A88":
                return entries

            if sw not in {"9000", "6310"}:
                raise CardError(
                    "GP_GET_STATUS_FAILED",
                    f"GET STATUS falló con SW={sw}.",
                )

            if response.data:
                entries.extend(
                    parse_registry_tlv(
                        response.data,
                        entry_type=selected_type,
                    )
                )

            if sw == "9000":
                return entries

            next_occurrence = True

        raise CardError(
            "GP_GET_STATUS_LIMIT",
            "GET STATUS excedió el límite de páginas.",
        )

    def read_registry(self) -> list[dict[str, Any]]:
        entries = []

        entries.extend(
            self.read_entries(
                0x40,
                entry_type="APPLET",
            )
        )

        entries.extend(
            self.read_entries(
                0x20,
                entry_type="PACKAGE",
            )
        )

        unique: dict[
            tuple[str, str],
            dict[str, Any],
        ] = {}

        for entry in entries:
            unique[
                (
                    entry["type"],
                    entry["aid"],
                )
            ] = entry

        return list(unique.values())
