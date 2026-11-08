from __future__ import annotations

from typing import Any

from card_studio.application import laboratory_profile
from card_studio.emv import (
    DataRecord,
    DataSource,
    DirectoryEntry,
    EmvDataStore,
    OwnershipDomain,
    TerminalAction,
    build_final_generate_ac_command,
    build_generate_ac_command,
    build_gpo_command,
    install_synthetic_decision_data,
    run_decision_engine,
    synthetic_data_foundation_responses,
    synthetic_generate_ac_response,
)
from card_studio.emv.online import OnlineFlowConfig
from card_studio.emv.runtime import EmvFlowConfig
from card_studio.kernels.base import KernelAdapter
from card_studio.profiles.model import CardProfile
from card_studio.readers.base import TransportResult
from card_studio.readers.mock import MockReaderAdapter
from card_studio.tlv import TlvNode, encode_tlv
from card_studio.transactions.model import TransactionRequest


def _runtime(interface: str, action: str) -> dict[str, Any]:
    pdol = [
        {"tag": "9F02", "length": 6, "data_type": "NUMERIC"},
        {"tag": "5F2A", "length": 2, "data_type": "NUMERIC"},
    ]
    if interface == "CONTACTLESS":
        pdol.append({"tag": "9F66", "length": 4, "data_type": "BINARY"})
    online_capable = action != "AAC"
    value = {
        "pdol": pdol,
        "cdol1": [
            {"tag": "9F02", "length": 6, "data_type": "NUMERIC"},
            {"tag": "95", "length": 5, "data_type": "BINARY"},
            {"tag": "9A", "length": 3, "data_type": "NUMERIC"},
            {"tag": "9C", "length": 1, "data_type": "BINARY"},
            {"tag": "9F34", "length": 3, "data_type": "BINARY"},
        ],
        "terminal_date": "260101",
        "supported_kernel_ids": ["02"] if interface == "CONTACTLESS" else [],
        "c8": {"require_ctq": True, "ctq_allowed_mask": "FFFF"},
        "decision": {
            "terminal_online_capable": online_capable,
            "taa": {
                "no_match_default_when_online_capable": action if online_capable else "ARQC",
                "no_match_default_when_offline_only": action if action in {"TC", "AAC"} else "AAC",
            },
            "risk": {"floor_limit_minor": 0, "random_selection_percent": 0},
        },
    }
    if interface == "CONTACTLESS":
        value["ttq"] = "26000000"
    return value


def internal_profile(kernel: KernelAdapter, case_id: str, interface: str, scenario: str) -> CardProfile:
    profile = laboratory_profile(kernel.kernel_ref)
    value = profile.to_dict()
    if case_id != "multiple-application":
        value["applications"] = value["applications"][:1]
    value["interfaces"] = [interface]
    action = "ARQC" if scenario == "online_required" else ("AAC" if scenario == "declined" else "TC")
    value["scheme_parameters"] = {
        **value["scheme_parameters"],
        "emv_execution": _runtime(interface, action),
        "online_completion": {
            "cdol2": [{"tag": "8A", "length": 2, "data_type": "BINARY"}],
            "final_gac_policy": "PROFILE",
            "issuer_authentication": "DISABLED",
            "scripts_enabled": True,
        },
    }
    value["profile_digest"] = ""
    return CardProfile.from_dict(value)


def prepare_internal_reader(
    reader: MockReaderAdapter,
    profile: CardProfile,
    request: TransactionRequest,
    malformed: bool = False,
    unpredictable_number: bytes | None = None,
) -> None:
    if not isinstance(reader, MockReaderAdapter) or reader.simulated is not True:
        raise ValueError("La matriz Internal requiere MockReaderAdapter SIMULATED.")
    config = EmvFlowConfig.from_profile(profile)
    if config is None:
        raise ValueError("El perfil Internal L3 requiere emv_execution.")
    store = config.new_store(request, unpredictable_number=unpredictable_number)
    card = EmvDataStore()
    install_synthetic_decision_data(card)
    card_records = tuple(card.snapshot().values())
    entries = tuple(
        DirectoryEntry(
            application.aid,
            application.priority,
            application.application_label,
            "02" if request.interface == "CONTACTLESS" else None,
        )
        for application in profile.applications
    )
    responses = synthetic_data_foundation_responses(
        request.interface,
        entries,
        config.pdol,
        store,
        records={(1, 1): card_records},
    )
    taa = config.decision_profile.taa
    action = (
        taa.no_match_default_when_online_capable
        if config.decision_profile.terminal_online_capable
        else taa.no_match_default_when_offline_only
    )
    cid = {"AAC": 0x00, "TC": 0x40, "ARQC": 0x80}[action.value]
    if request.interface == "CONTACTLESS":
        responses[build_gpo_command(config.pdol, store).to_bytes().hex().upper()] = TransportResult(
            encode_tlv(TlvNode("77", children=(
                TlvNode("82", b"\x00\x00"),
                TlvNode("94", bytes.fromhex("08010100")),
                TlvNode("9F27", bytes((cid,))),
                TlvNode("9F36", b"\x00\x01"),
                TlvNode("9F26", b"L3GPOAC1"),
                TlvNode("9F6C", b"\x00\x00"),
            ))),
            "9000",
        )
    store.merge(card_records)
    decision = run_decision_engine(store, config.decision_profile, config.decision_input(request, profile))
    store.merge((
        DataRecord("95", decision.risk.tvr.to_bytes(), DataSource.KERNEL, OwnershipDomain.KERNEL),
        DataRecord("9F34", decision.cvm.cvm_results, DataSource.KERNEL, OwnershipDomain.KERNEL),
    ))
    if request.interface == "CONTACT":
        first = build_generate_ac_command(decision.proposed_action, config.cdol1, store)
        responses[first.to_bytes().hex().upper()] = synthetic_generate_ac_response(cid, 77, "l3-first")
    online = OnlineFlowConfig.from_profile(profile)
    if online is None or online.cdol2 is None:
        raise ValueError("El perfil Internal L3 requiere online_completion/CDOL2.")
    store.put(DataRecord("8A", b"00", DataSource.HOST, OwnershipDomain.HOST))
    final = build_final_generate_ac_command(TerminalAction.TC, online.cdol2, store)
    responses[final.to_bytes().hex().upper()] = synthetic_generate_ac_response(0x40, 80, "l3-final")
    if malformed:
        environment = min(
            key for key, value in responses.items()
            if key.startswith("00A40400") and isinstance(value, TransportResult)
        )
        responses[environment] = TransportResult(b"\x6f\x05\x84\x01", "9000")
    reader.responses.update(responses)
