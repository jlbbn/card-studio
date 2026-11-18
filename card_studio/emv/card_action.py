from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from card_studio.interfaces.base import CardInterface
from card_studio.readers.base import TransportResult
from card_studio.tlv import TlvNode, encode_tlv
from card_studio.transactions.model import FunctionalResult

from .aip import ApplicationInterchangeProfile
from .apdu import CommandApdu
from .cryptogram import CryptogramResult
from .decision_engine import DecisionResult
from .dol import DolDocument
from .gac import GenerateAcResponse, build_final_generate_ac_command, build_generate_ac_command, generate_ac
from .oda import OdaBoundaryResult, OdaMethod, OdaProfile, OdaProvider, OdaStatus, evaluate_oda
from .taa import TerminalAction


EMV_CARD_ACTION_FLOW = "EMV_CARD_ACTION_FLOW"


class CardActionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CardActionInput:
    decision: DecisionResult
    aip: ApplicationInterchangeProfile
    cdol1: DolDocument
    operation_id: str
    profile_ref: str
    oda_profile: OdaProfile = OdaProfile()
    final_cdol2: DolDocument | None = None
    final_action: TerminalAction | None = None


@dataclass(frozen=True)
class CardActionResult:
    decision: DecisionResult
    oda: OdaBoundaryResult
    first_gac: GenerateAcResponse
    final_gac: GenerateAcResponse | None

    @property
    def cryptogram_result(self) -> CryptogramResult:
        return self.final_gac.result if self.final_gac is not None else self.first_gac.result

    @property
    def functional_result(self) -> FunctionalResult:
        return self.cryptogram_result.functional_result

    @property
    def command_count(self) -> int:
        return self.first_gac.command_count + (self.final_gac.command_count if self.final_gac else 0)

    @property
    def gate_status(self) -> str:
        return "PASS"

    def to_evidence(self) -> dict[str, Any]:
        history = [
            {"stage": "DECISION_ENGINE", "proposed_action": self.decision.proposed_action.value},
            {"stage": "ODA_BOUNDARY", "status": self.oda.status.value},
            {"stage": "FIRST_GENERATE_AC", "cryptogram_type": self.first_gac.result.cryptogram_type.value},
        ]
        if self.final_gac is not None:
            history.append({"stage": "FINAL_GENERATE_AC", "cryptogram_type": self.final_gac.result.cryptogram_type.value})
        return {
            "mode": "LAB_ONLY",
            "gate": EMV_CARD_ACTION_FLOW,
            "status": self.gate_status,
            "history": history,
            "functional_result": self.functional_result.value,
            "command_count": self.command_count,
            "oda": self.oda.to_evidence(),
            "first_gac": self.first_gac.to_evidence(),
            "final_gac": self.final_gac.to_evidence() if self.final_gac else None,
        }


def run_card_action_flow(
    card_interface: CardInterface,
    input: CardActionInput,
    oda_provider: OdaProvider,
    max_followups: int = 4,
    stage_handler: Any = None,
) -> CardActionResult:
    store = input.decision.store
    if stage_handler is not None:
        stage_handler("OFFLINE_DATA_AUTHENTICATION")
    oda = evaluate_oda(
        store,
        input.aip,
        input.oda_profile,
        oda_provider,
        input.operation_id,
        input.profile_ref,
        tvr=input.decision.risk.tvr,
        tsi=input.decision.risk.tsi,
    )
    if oda.technical_failure:
        raise CardActionError(oda.reason or oda.selection.blocked_reason or "ODA_BLOCKED", "ODA boundary está bloqueada.")
    cda_requested = oda.selection.method == OdaMethod.CDA and oda.status == OdaStatus.SUCCESS
    if stage_handler is not None:
        stage_handler("CARD_ACTION_ANALYSIS")
    first_command = build_generate_ac_command(
        input.decision.proposed_action,
        input.cdol1,
        store,
        cda_requested=cda_requested,
    )
    first = generate_ac(card_interface, first_command, max_followups=max_followups)
    final = None
    if input.final_cdol2 is not None:
        if stage_handler is not None:
            stage_handler("FINAL_CARD_ACTION_ANALYSIS")
        final_command = build_final_generate_ac_command(
            input.final_action or input.decision.proposed_action,
            input.final_cdol2,
            store,
            cda_requested=cda_requested,
        )
        final = generate_ac(card_interface, final_command, max_followups=max_followups)
    return CardActionResult(input.decision, oda, first, final)


def _synthetic_ac(seed: str) -> bytes:
    return hashlib.sha256(f"LAB_ONLY_SYNTHETIC_AC|{seed}".encode("utf-8")).digest()[:8]


def synthetic_generate_ac_response(
    cid: int = 0x80,
    response_format: int = 77,
    seed: str = "default",
    atc: bytes = b"\x00\x01",
    iad: bytes = b"",
    malformed: str | None = None,
) -> TransportResult:
    """Respuesta GENERATE AC local SYNTHETIC; AC no es criptografía productiva."""
    ac = _synthetic_ac(seed)
    if response_format == 77:
        children = [TlvNode("9F27", bytes((cid,))), TlvNode("9F36", atc), TlvNode("9F26", ac)]
        if iad:
            children.append(TlvNode("9F10", iad))
        data = encode_tlv(TlvNode("77", children=tuple(children)))
    elif response_format == 80:
        data = encode_tlv(TlvNode("80", bytes((cid,)) + atc + ac + iad))
    else:
        raise ValueError("Formato GAC debe ser 77 u 80.")
    if malformed is not None:
        name = malformed.upper()
        if name == "GAC_TRUNCATED":
            data = b"\x77\x05\x9f\x27"
        elif name == "GAC_WRONG_TEMPLATE":
            data = encode_tlv(TlvNode("70", children=()))
        elif name == "GAC_DUPLICATE_CID":
            data = encode_tlv(TlvNode("77", children=(
                TlvNode("9F27", bytes((cid,))),
                TlvNode("9F27", bytes((cid,))),
                TlvNode("9F36", atc),
                TlvNode("9F26", ac),
            )))
        elif name == "GAC_MISSING_AC":
            data = encode_tlv(TlvNode("77", children=(TlvNode("9F27", bytes((cid,))), TlvNode("9F36", atc))))
        elif name == "GAC_SHORT_AC":
            data = encode_tlv(TlvNode("77", children=(
                TlvNode("9F27", bytes((cid,))),
                TlvNode("9F36", atc),
                TlvNode("9F26", ac[:7]),
            )))
        elif name == "GAC_MISSING_ATC":
            data = encode_tlv(TlvNode("77", children=(TlvNode("9F27", bytes((cid,))), TlvNode("9F26", ac))))
        elif name == "GAC_UNSUPPORTED_CID":
            data = encode_tlv(TlvNode("77", children=(
                TlvNode("9F27", b"\xc0"),
                TlvNode("9F36", atc),
                TlvNode("9F26", ac),
            )))
        elif name == "GAC_IAD_TOO_LONG":
            data = encode_tlv(TlvNode("77", children=(
                TlvNode("9F27", bytes((cid,))),
                TlvNode("9F36", atc),
                TlvNode("9F26", ac),
                TlvNode("9F10", bytes(33)),
            )))
        elif name == "GAC_UNKNOWN_TAG":
            data = encode_tlv(TlvNode("77", children=(
                TlvNode("9F27", bytes((cid,))),
                TlvNode("9F36", atc),
                TlvNode("9F26", ac),
                TlvNode("DF77", b"\x01"),
            )))
        else:
            raise ValueError("Caso malformed GAC desconocido.")
    return TransportResult(data, "9000")


def synthetic_gac_responses(
    first_command: CommandApdu,
    first_cid: int = 0x80,
    first_format: int = 77,
    final_command: CommandApdu | None = None,
    final_cid: int = 0x40,
    final_format: int = 80,
    malformed: str | None = None,
) -> dict[str, TransportResult]:
    responses = {
        first_command.to_bytes().hex().upper(): synthetic_generate_ac_response(first_cid, first_format, "first", malformed=malformed),
    }
    if final_command is not None:
        responses[final_command.to_bytes().hex().upper()] = synthetic_generate_ac_response(final_cid, final_format, "final")
    return responses
